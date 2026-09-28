# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""UserStore — load, save, authenticate, and manage user accounts.

Storage layout: a single ``users.json`` file in the data dir, with atomic
replace on every mutation.  Sessions are in-memory (cleared on restart),
matching the existing admin-token pattern in ``api/admin.py``.

Password hashing: stdlib ``hashlib.scrypt`` with per-user random salt.
The stored hash is ``scrypt$N$r$p$<salt_hex>$<key_hex>`` so the
verification path is self-contained — no Python crypto deps required.

Thread safety: all writes happen on the event loop's single writer
context; the in-memory dicts are GIL-atomic for read access from other
threads, matching the rest of the store.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from pathlib import Path

from soniqboom.models.user import User, ROLES, Role

log = logging.getLogger(__name__)

# scrypt parameters — NIST SP 800-63B "memory-hard" defaults.  N=2**15
# (~32 MB), r=8, p=1 hashes in ~80 ms on an M-series Mac, plenty fast
# for a login but slow enough to make brute-force expensive.  OpenSSL
# defaults ``maxmem`` to 32 MB which is *exactly* the memory this combo
# wants, so we hand it a generous explicit ceiling — without it
# ``hashlib.scrypt`` raises "memory limit exceeded".
_SCRYPT_N      = 2 ** 15
_SCRYPT_R      = 8
_SCRYPT_P      = 1
_SCRYPT_KEY    = 64
_SCRYPT_MAXMEM = 128 * 1024 * 1024   # 128 MB — fits N=2**15, r=8, p=1

# Login lockout — bounds brute-force at K guesses per window per username.
# A determined botnet distributing across many usernames can still try,
# but the scrypt cost (~80 ms each) + this lockout makes per-account
# break-in impractical (15 guesses in 15 min = effective rate ≤ 1/min).
_LOCKOUT_MAX_ATTEMPTS = 15
_LOCKOUT_WINDOW_SEC   = 15 * 60      # rolling 15 min window
_LOCKOUT_COOLDOWN_SEC = 15 * 60      # lock duration after threshold hit

# ── Scrobble-token at-rest encryption ───────────────────────────────────────

_TOKEN_FIELDS = ("listenbrainz_token", "lastfm_session_key")
_ENC_PREFIX   = "enc:v1:"


def _encrypt_token_fields(rec: dict) -> dict:
    """Return a shallow copy of ``rec`` with token fields encrypted on
    disk.  Idempotent — already-prefixed values pass through."""
    from soniqboom.core.credentials import encrypt
    out = dict(rec)
    for k in _TOKEN_FIELDS:
        v = out.get(k)
        if v and not str(v).startswith(_ENC_PREFIX):
            try:
                out[k] = _ENC_PREFIX + encrypt(v)
            except Exception:
                # Encryption key unavailable (e.g. cryptography missing
                # during a partial install).  Fall back to plaintext so we
                # don't corrupt the file — the QA note about plaintext is
                # an explicit accepted risk in that degraded mode.
                pass
    return out


def _decrypt_token_fields(rec: dict) -> None:
    """Mutate ``rec`` in-place, decrypting any ``enc:v1:`` token fields."""
    from soniqboom.core.credentials import decrypt
    for k in _TOKEN_FIELDS:
        v = rec.get(k)
        if v and isinstance(v, str) and v.startswith(_ENC_PREFIX):
            try:
                plain = decrypt(v[len(_ENC_PREFIX):])
                rec[k] = plain or None
            except Exception:
                # Decryption failure (machine moved, key rotated) — drop
                # the value rather than crash; the user can re-paste.
                rec[k] = None

# The Subsonic token secret (``subsonic_password``) must stay recoverable as
# plaintext for ``t = md5(secret + s)`` — and by default it is a copy of the
# LOGIN password.  It is encrypted on disk with a RANDOM key kept in the data
# dir (``secret.key``, 0600).  Not the machine-bound credentials key: that one
# derives from the hostname, which changes on every Docker rebuild and would
# silently wipe every user's secret.  The key travels with a data-dir backup;
# restored WITHOUT it, the secret decrypts to None and is re-seeded at the
# next browser sign-in (see ``UserStore._login_ok``).
_SS_FIELD = "subsonic_password"
_SS_PREFIX = "enc:v2:"


def _load_or_create_data_key(key_path: Path):
    """Return a Fernet for ``key_path``, creating the key once if absent, or
    ``None`` when no usable key can be had (unreadable file).

    Creation is exclusive (``O_EXCL`` on the final name — two processes racing,
    e.g. the server and the ``soniqboom-setadm`` CLI, end up with ONE key) and
    fsynced before anyone can read it, so a power loss can't leave an empty
    key that silently disables encryption.  A corrupt key is moved aside
    (its secrets are unrecoverable anyway) and a fresh one created."""
    from cryptography.fernet import Fernet

    def _read():
        k = key_path.read_bytes().strip()
        Fernet(k)                       # validates length / base64 (ValueError)
        return k

    for attempt in range(6):
        try:
            return Fernet(_read())
        except FileNotFoundError:
            break                       # create it below
        except (ValueError, TypeError):
            # Invalid CONTENT.  An empty file may be another process's key a
            # moment before its write lands (exclusive create, then write):
            # give that a brief chance before calling the key corrupt.
            if attempt < 5:
                time.sleep(0.02)
                continue
            try:
                aside = key_path.with_name(
                    f"{key_path.name}.corrupt-{time.time_ns()}-{os.getpid()}")
                os.replace(key_path, aside)
                log.error("%s was unusable (moved to %s); a new key is created and "
                          "every user's Subsonic token secret is re-seeded at their "
                          "next sign-in.", key_path, aside.name)
            except OSError:
                return None
            break
        except OSError as exc:
            # Unreadable for another reason (permissions, EMFILE, EIO): leave
            # the file alone — a transient error must never destroy a good key.
            log.error("Cannot read %s (%s) — the Subsonic token secret is not "
                      "stored until it can be read.", key_path, exc)
            return None
    key = Fernet.generate_key()
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            return Fernet(_read())      # another process created it first
        except Exception:
            return None
    except OSError:
        return None
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        try:
            dfd = os.open(str(key_path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    except OSError:
        try:
            key_path.unlink()
        except OSError:
            pass
        return None
    return Fernet(key)


def _encrypt_ss(rec: dict, fernet) -> dict:
    """Encrypt the Subsonic secret for disk.  Never falls back to plaintext:
    without a usable key the secret is written as None (it is re-seeded from
    the login password at the next browser sign-in)."""
    v = rec.get(_SS_FIELD)
    if v and not str(v).startswith(_SS_PREFIX):
        rec = dict(rec)
        if fernet is None:
            rec[_SS_FIELD] = None
        else:
            rec[_SS_FIELD] = _SS_PREFIX + fernet.encrypt(v.encode()).decode()
    return rec


def _decrypt_ss(rec: dict, fernet) -> None:
    v = rec.get(_SS_FIELD)
    if v and isinstance(v, str) and v.startswith(_SS_PREFIX):
        try:
            rec[_SS_FIELD] = fernet.decrypt(v[len(_SS_PREFIX):].encode()).decode() or None
        except Exception:
            rec[_SS_FIELD] = None      # key missing / rotated: re-seeded at next sign-in


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._\-]{2,64}$")
_PASSWORD_MIN_LEN = 8
# Hard ceiling on password length — scrypt over a multi-MB blob will block
# the event loop and is never legitimate.
_PASSWORD_MAX_LEN = 1024

# Session token TTL — 7 days, refreshed on every authed request.  Slightly
# longer than the old admin token (1 h) because users expect "stay signed
# in" behaviour from a media player.
_SESSION_TTL_SEC = 7 * 24 * 3600

# A successful login rewrites users.json only when the persisted
# ``last_login_at`` is older than this (or a password-derived field changed):
# Subsonic clients authenticate on EVERY request, and a full users.json
# rewrite per request was pure overhead.
_LAST_LOGIN_PERSIST_SEC = 3600

# OpenSubsonic API keys (``apiKey=``): per-user cap and name length.
_MAX_API_KEYS_PER_USER = 20
_API_KEY_NAME_MAX = 64


def _api_key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _parse_api_keys(raw, users: dict) -> tuple[dict[str, list[dict]], dict[str, tuple[str, str]]]:
    """users.json ``api_keys`` → (per-user entries, hash index).  Entries of
    unknown users and malformed rows are dropped, never fatal."""
    keys: dict[str, list[dict]] = {}
    by_hash: dict[str, tuple[str, str]] = {}
    if not isinstance(raw, dict):
        return keys, by_hash
    for uid, lst in raw.items():
        if uid not in users or not isinstance(lst, list):
            continue
        for k in lst:
            if not (isinstance(k, dict) and isinstance(k.get("id"), str)
                    and isinstance(k.get("sha256"), str) and len(k["sha256"]) == 64):
                continue
            ent = {"id": k["id"], "name": str(k.get("name") or "")[:_API_KEY_NAME_MAX],
                   "sha256": k["sha256"], "created": float(k.get("created") or 0)}
            keys.setdefault(uid, []).append(ent)
            by_hash[ent["sha256"]] = (uid, ent["id"])
    return keys, by_hash


# ── Password hashing ─────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Return a self-describing scrypt hash string."""
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=_SCRYPT_KEY,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time-compare a password against a stored scrypt hash."""
    try:
        algo, n, r, p, salt_hex, key_hex = stored.split("$")
        if algo != "scrypt":
            return False
        n_i, r_i, p_i = int(n), int(r), int(p)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(key_hex)
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n_i, r=r_i, p=p_i,
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(expected),
        )
        return secrets.compare_digest(candidate, expected)
    except (ValueError, TypeError):
        return False


# ── Validation ───────────────────────────────────────────────────────────────

def validate_username(username: str) -> None:
    """Raise ValueError if the username doesn't meet the rules."""
    if not _USERNAME_RE.match(username or ""):
        raise ValueError(
            "Username must be 2-64 chars, alphanumerics + ._- only.",
        )


def validate_password(password: str) -> None:
    if not password or len(password) < _PASSWORD_MIN_LEN:
        raise ValueError(
            f"Password must be at least {_PASSWORD_MIN_LEN} characters.",
        )
    if len(password) > _PASSWORD_MAX_LEN:
        raise ValueError(
            f"Password must be at most {_PASSWORD_MAX_LEN} characters.",
        )


# ── Store ────────────────────────────────────────────────────────────────────

class UserStore:
    """In-memory user/session manager, persisted to ``users.json``."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._path = data_dir / "users.json"
        self._lock_path = data_dir / "users.json.lock"
        self._secret_key_path = data_dir / "secret.key"   # at-rest key for the Subsonic secret
        self._ss_key = None                               # Fernet, loaded once (``_ss_fernet``)
        # Ciphertext of secrets that could not be decrypted because the key was
        # UNREADABLE at load: written back verbatim until the key is readable
        # again (then decrypted in ``_ss_fernet``) — never dropped, never plain.
        self._ss_raw: dict[str, str] = {}
        self._ss_key_retry_at = 0.0       # no key: don't re-try (and re-log) before this
        # Users keyed by id (UUID), with a username→id index for fast lookup.
        self._users: dict[str, User] = {}
        self._by_username: dict[str, str] = {}
        # Sessions: token → {user_id, expiry}.  In-memory only.
        self._sessions: dict[str, dict] = {}
        # Lockout state: username_lower → {fails: [(ts, ...)], locked_until}.
        # In-memory only; a restart clears all counters which is acceptable
        # (an attacker who can restart the server has bigger leverage).
        self._lockout: dict[str, dict] = {}
        # OpenSubsonic API keys: user id → [{id, name, sha256, created}], and
        # sha256(key) → (user id, key id) for the O(1) per-request lookup.
        # Persisted in users.json under ``api_keys`` (hashes only — the
        # plaintext is shown once, at creation).  ``last_used`` is tracked in
        # memory only: writing users.json on every Subsonic request is exactly
        # the cost the auth fast path exists to avoid.
        self._api_keys: dict[str, list[dict]] = {}
        self._by_api_key_hash: dict[str, tuple[str, str]] = {}
        self._api_key_last_used: dict[str, float] = {}
        # user id → the ``last_login_at`` value last written to disk, so a
        # successful login only rewrites users.json when that stamp is stale
        # (see ``authenticate``).
        self._login_persisted: dict[str, float] = {}
        # user id → (password_hash, HMAC of the plaintext) for the last login
        # password scrypt verified: lets a client that sends ``p=`` on every
        # request be checked in O(1) (``check_cached_password``).  Keyed by a
        # per-process random secret, memory only, and bound to the hash — a
        # password change invalidates it by construction.
        self._pw_verified: dict[str, tuple[str, bytes]] = {}
        self._pw_key = secrets.token_bytes(32)
        # user id → whether ``subsonic_password`` is a GENERATED app password
        # (True) or the seeded copy of the login password (False); absent =
        # unknown (a record older than this flag — learned at the next
        # password login, which has the plaintext in hand).  Persisted in
        # users.json under ``subsonic_password_custom``.
        self._ss_custom: dict[str, bool] = {}
        # A single lock guards in-process IO + the username index.  Reads of
        # the dict itself are GIL-atomic so individual gets don't need it.
        # The fcntl flock on _lock_path serialises *across* processes — the
        # CLI and the server can both touch users.json safely.
        self._lock = threading.Lock()
        self._load()

    # ── Cross-process file lock ─────────────────────────────────────────

    def _flock(self):
        """Context manager: acquire an exclusive fcntl flock on the lock
        file.  Ensures CLI invocations and the running server never race
        on users.json."""
        class _Flock:
            def __init__(self, path: Path):
                self.path = path
                self.f = None

            def __enter__(self_inner):
                # ``open`` for "ab" creates the file if absent, won't truncate.
                self_inner.f = open(self_inner.path, "ab")
                fcntl.flock(self_inner.f.fileno(), fcntl.LOCK_EX)
                return self_inner

            def __exit__(self_inner, *exc):
                try:
                    fcntl.flock(self_inner.f.fileno(), fcntl.LOCK_UN)
                finally:
                    try: self_inner.f.close()
                    except Exception: pass
        return _Flock(self._lock_path)

    # ── Load / save ──────────────────────────────────────────────────────

    def _ss_fernet(self):
        """The at-rest key for the Subsonic secret, loaded (or created) once.
        An unreadable key is retried on the next use (a transient EMFILE/EIO
        must not disable the secret for the life of the process)."""
        if self._ss_key is None:
            now = time.monotonic()
            if now < self._ss_key_retry_at:
                return None
            self._ss_key = _load_or_create_data_key(self._secret_key_path)
            if self._ss_key is None:
                self._ss_key_retry_at = now + 30.0
            if self._ss_key is not None and self._ss_raw:
                pending, self._ss_raw = self._ss_raw, {}
                for uid, raw in pending.items():
                    u = self._users.get(uid)
                    if u is not None and u.subsonic_password is None:
                        rec = {_SS_FIELD: raw}
                        _decrypt_ss(rec, self._ss_key)
                        u.subsonic_password = rec[_SS_FIELD]
        return self._ss_key

    def _storage_record(self, u: User) -> dict:
        # Key FIRST: a key that became readable again decrypts the pending
        # ciphertext into ``u`` — the record must be built after that.
        fer = self._ss_fernet()
        rec = _encrypt_token_fields(u.to_storage())
        if fer is None and u.subsonic_password is None and u.id in self._ss_raw:
            rec[_SS_FIELD] = self._ss_raw[u.id]        # keep the ciphertext as it was
            return rec
        if u.subsonic_password is not None:
            self._ss_raw.pop(u.id, None)               # a new secret supersedes it
        return _encrypt_ss(rec, fer)

    def _load(self) -> bool:
        """Read users.json into memory.  On a parse error we move the
        corrupt file aside (``users.json.corrupt-<ts>``) and continue with
        an empty store.  Without this, the next save() would atomically
        replace the unreadable but on-disk file, silently wiping
        whatever legitimate users.json may have been (e.g. partial write,
        version mismatch).

        Scrobble tokens are stored encrypted at rest (Fernet, same key as
        share passwords).  We decrypt on load so the in-memory User
        objects hold plaintext for the scrobble path — but the on-disk
        file never reveals them.

        Returns False when the file could not be read (and was moved
        aside); a missing file is nothing to load, not a failure."""
        if not self._path.exists():
            return True
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            users = data.get("users", [])
            # Build fresh maps and swap them in ATOMICALLY rather than
            # clear()+repopulate.  ``lookup_session`` reads ``_users`` /
            # ``_by_username`` WITHOUT the lock (it's the hot auth path on every
            # request); the old clear-then-fill exposed an empty-map window
            # during which a concurrent lookup saw "user not found", POPPED the
            # still-valid session (see ``lookup_session`` line ~559), and then
            # returned a *sticky* 401 on every authenticated route until the
            # user signed in again.  A dict rebind is atomic under the GIL, so a
            # lock-free reader always sees either the complete old map or the
            # complete new one — never a partial state.  ``reload()`` still holds
            # ``self._lock`` so writers remain serialised.
            new_users: dict[str, User] = {}
            new_by_username: dict[str, str] = {}
            new_raw: dict[str, str] = {}
            fer = self._ss_fernet()
            for u in users:
                _decrypt_token_fields(u)
                raw_ss = u.get(_SS_FIELD)
                _decrypt_ss(u, fer)
                user = User.from_storage(u)
                if fer is None and isinstance(raw_ss, str) and raw_ss.startswith(_SS_PREFIX):
                    new_raw[user.id] = raw_ss
                new_users[user.id] = user
                new_by_username[user.username.lower()] = user.id
            new_keys, new_by_hash = _parse_api_keys(data.get("api_keys"), new_users)
            raw_custom = data.get("subsonic_password_custom")
            new_custom = ({uid: v for uid, v in raw_custom.items()
                           if uid in new_users and isinstance(v, bool)}
                          if isinstance(raw_custom, dict) else {})
            self._ss_raw = new_raw
            self._users = new_users
            self._by_username = new_by_username
            self._api_keys = new_keys
            self._by_api_key_hash = new_by_hash
            self._ss_custom = new_custom
            self._login_persisted = {uid: float(u.last_login_at or 0)
                                     for uid, u in new_users.items()}
            log.info("Loaded %d user(s) from %s", len(self._users), self._path)
            return True
        except (json.JSONDecodeError, OSError, ValueError, KeyError) as exc:
            # Move corrupt file aside so an admin can investigate; refuse
            # to overwrite their data on next save.
            try:
                ts = int(time.time())
                quarantine = self._path.with_suffix(f".json.corrupt-{ts}")
                shutil.move(str(self._path), str(quarantine))
                log.error("users.json could not be parsed (%s); moved aside to %s",
                          exc, quarantine)
            except OSError:
                log.exception("users.json corrupt and could not be quarantined; aborting")
                raise RuntimeError(
                    f"users.json at {self._path} is corrupt and cannot be moved aside. "
                    f"Fix or remove the file before restarting."
                )
            return False

    def reload(self) -> bool:
        """Re-read users.json from disk.  Used by ``POST /auth/reload``
        after the CLI bootstraps a new admin so the server's in-memory
        singleton sees the new user without a process restart.  False when
        the file could not be read — the in-memory accounts are unchanged."""
        with self._flock(), self._lock:
            return self._load()

    def _save(self) -> None:
        """Atomic write of users.json under the cross-process flock.
        Sensitive token fields (last.fm session, ListenBrainz token) are
        encrypted with the machine-bound credentials key before write."""
        with self._flock(), self._lock:
            data = {
                "version": 1,
                "users": [self._storage_record(u) for u in self._users.values()],
            }
            keys = {uid: [dict(k) for k in lst]
                    for uid, lst in self._api_keys.items() if lst and uid in self._users}
            if keys:
                data["api_keys"] = keys
            custom = {uid: v for uid, v in self._ss_custom.items() if uid in self._users}
            if custom:
                data["subsonic_password_custom"] = custom
            for u in self._users.values():
                self._login_persisted[u.id] = float(u.last_login_at or 0)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            # Lock down the data dir + lock file too — the QA pass flagged
            # them as relying on umask, which on multi-user UNIX hosts
            # could leak users.json's existence (or contents under a bad
            # umask) to other local users.
            for p, mode in [
                (tmp.parent,   0o700),
                (self._lock_path, 0o600),
            ]:
                try: os.chmod(p, mode)
                except OSError: pass
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            try: os.chmod(tmp, 0o600)
            except OSError: pass
            os.replace(tmp, self._path)

    # ── Queries ──────────────────────────────────────────────────────────

    def count(self) -> int:
        return len(self._users)

    def has_any(self) -> bool:
        return len(self._users) > 0

    def has_any_admin(self) -> bool:
        return any(u.role == "admin" and u.enabled for u in self._users.values())

    def get(self, user_id: str) -> User | None:
        u = self._users.get(user_id)
        if u is not None and self._ss_raw and u.id in self._ss_raw:
            self._ss_fernet()      # key readable again? recover held-back secrets now
        return u

    def get_by_id(self, user_id: str) -> User | None:
        """Alias for :meth:`get` — look up a user by their immutable id.

        ``cast_stream``'s token re-auth check probes for a ``get_by_id``
        method (the cast token's ``uid`` claim is the user *id*, via
        ``cast._user_field``).  Without this method that probe silently
        no-ops and the fallback ``get_by_username(uid)`` can't resolve a
        UUID, so the re-auth check rejected EVERY cast token with a 404
        ("Stream link no longer valid") — i.e. no cast ever streamed.
        """
        return self.get(user_id)

    def get_by_username(self, username: str) -> User | None:
        uid = self._by_username.get((username or "").lower())
        u = self._users.get(uid) if uid else None
        # Every sign-in path looks the user up here: a secret held back while
        # the key file was unreadable is recovered on demand (throttled to one
        # key read per 30 s), not only at the next users.json save — token
        # sign-in would otherwise stay broken on a quiet server.
        if u is not None and self._ss_raw and u.id in self._ss_raw:
            self._ss_fernet()
        return u

    def list_users(self) -> list[User]:
        return sorted(self._users.values(), key=lambda u: u.created_at)

    # ── Mutations ────────────────────────────────────────────────────────

    def create(
        self,
        username: str,
        password: str,
        role: Role,
        display_name: str | None = None,
    ) -> User:
        validate_username(username)
        validate_password(password)
        if role not in ROLES:
            raise ValueError(f"Invalid role: {role}")
        if self.get_by_username(username):
            raise ValueError(f"Username already taken: {username}")
        user = User(
            id=str(uuid.uuid4()),
            username=username,
            password_hash=hash_password(password),
            role=role,
            created_at=time.time(),
            display_name=display_name,
            # Seed the Subsonic-compat field at creation time so token
            # auth works on first connect — no separate setup step.
            subsonic_password=password,
        )
        with self._lock:
            self._users[user.id] = user
            self._by_username[user.username.lower()] = user.id
            self._ss_custom[user.id] = False            # the seeded login password
        self._save()
        log.info("Created user '%s' (role=%s)", user.username, user.role)
        return user

    def subsonic_password_custom(self, user: User) -> bool | None:
        """Whether the user's Subsonic token-mode secret is a generated app
        password (True) or the seeded copy of their login password (False);
        None when not yet known (an older record, learned at the next
        password login).  False when no secret is set at all."""
        if not user.subsonic_password:
            return False
        return self._ss_custom.get(user.id)

    def public(self, user: User) -> dict:
        """``user.to_public()`` plus ``subsonic_password_custom`` — what the
        web UI's account page shows ("token apps use your SoniqBoom password"
        vs "a generated app password is in use")."""
        d = user.to_public()
        d["subsonic_password_custom"] = self.subsonic_password_custom(user)
        return d

    def update(
        self,
        user_id: str,
        *,
        role: Role | None = None,
        enabled: bool | None = None,
        display_name: str | None = None,
        listenbrainz_token: str | None = None,
        lastfm_session_key: str | None = None,
        subsonic_password: str | None = None,
        subsonic_password_custom: bool | None = None,
    ) -> User:
        # Take the in-process lock around the entire read-check-write so
        # two concurrent admin demotions can't both pass the "other
        # admins exist" check and leave zero admins (pen-test #1 P1-7).
        # Coupled with the cross-process flock in _save() this closes the
        # window completely.
        with self._lock:
            user = self._users.get(user_id)
            if not user:
                raise KeyError(user_id)
            next_role    = role    if role    is not None else user.role
            next_enabled = enabled if enabled is not None else user.enabled
            # Invariant: there must always be at least one enabled admin.
            if user.role == "admin" and user.enabled and (
                next_role != "admin" or not next_enabled
            ):
                other_admins = [
                    u for u in self._users.values()
                    if u.id != user_id and u.role == "admin" and u.enabled
                ]
                if not other_admins:
                    raise ValueError(
                        "Refusing to remove the last enabled admin — promote "
                        "or enable another admin first.",
                    )
            if role is not None:
                if role not in ROLES:
                    raise ValueError(f"Invalid role: {role}")
                user.role = role
            if enabled is not None:
                user.enabled = bool(enabled)
            if display_name is not None:
                user.display_name = display_name or None
            if listenbrainz_token is not None:
                user.listenbrainz_token = listenbrainz_token or None
            if lastfm_session_key is not None:
                user.lastfm_session_key = lastfm_session_key or None
            # subsonic_password (the Subsonic app password token-mode clients
            # use): a non-empty string sets it; "" REMOVES it and is kept as
            # "" — an explicit "no token sign-in" that a later login never
            # re-seeds (None, "never set", is seeded with the login
            # password); ``None`` means "don't touch this field".
            if subsonic_password is not None:
                user.subsonic_password = subsonic_password
            # ``subsonic_password_custom``: True = the value just set is a
            # generated app password (PUT /api/me/subsonic-password); False =
            # a copy of the login password.  Setting a secret without saying
            # which makes the flag unknown again (learned at the next login;
            # a password change then pays its one scrypt check).
            if subsonic_password_custom is not None:
                self._ss_custom[user_id] = bool(subsonic_password_custom)
            elif subsonic_password is not None:
                self._ss_custom.pop(user_id, None)
            # Demoting or disabling a user kicks out their open sessions —
            # without this, a user demoted from admin to readonly keeps
            # admin-level cookies until their session naturally expires.
            should_purge = (
                (role is not None and role != "admin" and user.role != "admin") or
                (enabled is False)
            )
            if should_purge:
                self._purge_sessions_for(user_id)
        # _save() acquires its own flock; call outside the in-process lock
        # to avoid holding it across IO.
        self._save()
        return user

    def set_password(self, user_id: str, new_password: str) -> None:
        validate_password(new_password)
        user = self._users.get(user_id)
        if not user:
            raise KeyError(user_id)
        # The token-mode secret follows the login password only when it IS a
        # copy of it (seeded, never customised): a generated Subsonic app
        # password — or an explicit removal ("") — survives a login-password
        # change.  The custom flag answers that in O(1); only a record whose
        # flag is still unknown pays one scrypt check, on this rare path.
        sp = user.subsonic_password
        known = self._ss_custom.get(user_id)
        held = user_id in self._ss_raw     # set, but unreadable right now (key outage)
        follows = ((sp is None and not held)
                   or (held and known is False)
                   or (bool(sp) and (known is False or (
                       known is None and verify_password(sp, user.password_hash)))))
        user.password_hash = hash_password(new_password)
        if follows:
            user.subsonic_password = new_password
            self._ss_custom[user_id] = False
        elif sp and known is None:
            self._ss_custom[user_id] = True
        self._pw_verified.pop(user_id, None)
        # Force re-login everywhere when password rotates.
        self._purge_sessions_for(user_id)
        self._save()

    def delete(self, user_id: str) -> None:
        user = self._users.get(user_id)
        if not user:
            return
        # Refuse to delete the last enabled admin — would lock everyone out.
        if user.role == "admin":
            remaining = [
                u for u in self._users.values()
                if u.id != user_id and u.role == "admin" and u.enabled
            ]
            if not remaining:
                raise ValueError(
                    "Refusing to delete the last enabled admin — promote "
                    "another user to admin first.",
                )
        with self._lock:
            self._users.pop(user_id, None)
            self._by_username.pop(user.username.lower(), None)
            for k in self._api_keys.pop(user_id, None) or ():
                self._by_api_key_hash.pop(k["sha256"], None)
                self._api_key_last_used.pop(k["id"], None)
            self._pw_verified.pop(user_id, None)
            self._ss_custom.pop(user_id, None)
        self._purge_sessions_for(user_id)
        self._save()
        log.info("Deleted user '%s'", user.username)

    # ── OpenSubsonic API keys ────────────────────────────────────────────

    def create_api_key(self, user_id: str, name: str = "") -> tuple[str, dict]:
        """Mint a key for ``user_id``: ``(plaintext, public entry)``.  The
        plaintext exists only in this return value — users.json keeps its
        sha256.  Raises ``KeyError`` for an unknown user, ``ValueError`` at
        the per-user cap."""
        if user_id not in self._users:
            raise KeyError(user_id)
        key = secrets.token_urlsafe(32)          # 256 bits of entropy
        ent = {"id": secrets.token_hex(8),
               "name": " ".join((name or "").split())[:_API_KEY_NAME_MAX],
               "sha256": _api_key_hash(key), "created": time.time()}
        with self._lock:
            lst = self._api_keys.setdefault(user_id, [])
            if len(lst) >= _MAX_API_KEYS_PER_USER:
                raise ValueError(f"At most {_MAX_API_KEYS_PER_USER} API keys per account — "
                                 "revoke one first.")
            lst.append(ent)
            self._by_api_key_hash[ent["sha256"]] = (user_id, ent["id"])
        self._save()
        return key, self._public_key(ent)

    def _public_key(self, ent: dict) -> dict:
        return {"id": ent["id"], "name": ent["name"], "created": ent["created"],
                "last_used": self._api_key_last_used.get(ent["id"])}

    def list_api_keys(self, user_id: str) -> list[dict]:
        """The user's keys, newest first — never the key or its hash."""
        return [self._public_key(k) for k in
                sorted(self._api_keys.get(user_id) or (), key=lambda k: -k["created"])]

    def revoke_api_key(self, user_id: str, key_id: str) -> bool:
        with self._lock:
            lst = self._api_keys.get(user_id) or []
            hit = next((k for k in lst if k["id"] == key_id), None)
            if hit is None:
                return False
            lst.remove(hit)
            if not lst:
                self._api_keys.pop(user_id, None)
            self._by_api_key_hash.pop(hit["sha256"], None)
            self._api_key_last_used.pop(key_id, None)
        self._save()
        return True

    def lookup_api_key(self, key: str) -> User | None:
        """The enabled user owning ``key``, else None.  One sha256 + one dict
        lookup: the key is 256-bit random, so an exact hash lookup leaks
        nothing useful through timing."""
        hit = self.lookup_api_key_entry(key)
        return hit[0] if hit else None

    def lookup_api_key_entry(self, key: str) -> tuple[User, str] | None:
        """``(owner, key id)`` for an enabled user's ``key``, else None — the
        key id lets a link minted with this key die when the key is revoked
        (``has_api_key``)."""
        if not key or len(key) > 256:
            return None
        hit = self._by_api_key_hash.get(_api_key_hash(key))
        if hit is None:
            return None
        user = self._users.get(hit[0])
        if user is None or not user.enabled:
            return None
        self._api_key_last_used[hit[1]] = time.time()
        return user, hit[1]

    def has_api_key(self, user_id: str, key_id: str) -> bool:
        """Whether ``key_id`` is still one of ``user_id``'s keys (not revoked)."""
        return any(k["id"] == key_id for k in self._api_keys.get(user_id) or ())

    # ── Authentication ───────────────────────────────────────────────────

    def _is_locked(self, username_lower: str) -> tuple[bool, float]:
        """Return (locked, seconds_remaining)."""
        rec = self._lockout.get(username_lower)
        if not rec:
            return (False, 0.0)
        locked_until = rec.get("locked_until", 0.0)
        if locked_until > time.time():
            return (True, locked_until - time.time())
        return (False, 0.0)

    def _note_failed_login(self, username_lower: str) -> None:
        now = time.time()
        rec = self._lockout.setdefault(username_lower, {"fails": [], "locked_until": 0.0})
        # Drop fails outside the rolling window.
        rec["fails"] = [t for t in rec["fails"] if now - t < _LOCKOUT_WINDOW_SEC]
        rec["fails"].append(now)
        if len(rec["fails"]) >= _LOCKOUT_MAX_ATTEMPTS:
            rec["locked_until"] = now + _LOCKOUT_COOLDOWN_SEC
            log.warning(
                "Account '%s' locked for %ds after %d failed login attempts",
                username_lower, _LOCKOUT_COOLDOWN_SEC, len(rec["fails"]),
            )

    def _clear_failed_logins(self, username_lower: str) -> None:
        self._lockout.pop(username_lower, None)

    def is_locked(self, username: str, scope: str = "") -> bool:
        """Whether ``username`` is locked out (``scope`` "" = the main login
        password; "token" = Subsonic token-mode guesses and "p" = Subsonic
        ``p=`` password guesses, each counted separately so a client stuck on
        a stale token / retired app password can't lock the web login)."""
        return self._is_locked(self._lock_key(username, scope))[0]

    def note_failed_attempt(self, username: str, scope: str = "") -> None:
        """Count one failed guess against ``username`` in ``scope``."""
        self._note_failed_login(self._lock_key(username, scope))

    @staticmethod
    def _lock_key(username: str, scope: str) -> str:
        low = (username or "").lower()
        return f"{low}\0{scope}" if scope else low

    @staticmethod
    def _dummy_hash() -> str:
        """Build the dummy-hash from *current* scrypt params so equal-time
        compares stay equal-time even when N/r/p change in a future
        deployment.  Cached on first call so the cost is amortised."""
        if not hasattr(UserStore, "_DUMMY_HASH_CACHE"):
            UserStore._DUMMY_HASH_CACHE = hash_password("__dummy__")
        return UserStore._DUMMY_HASH_CACHE

    def authenticate(self, username: str, password: str) -> User | None:
        """Verify ``(username, password)``.  Bounded by per-username
        lockout so brute-force is impractical even with the scrypt cost.
        Returns None on bad creds OR lockout."""
        u_lower = (username or "").lower()
        locked, _remaining = self._is_locked(u_lower)
        if locked:
            # Burn the same scrypt time as a real check so locked accounts
            # don't have an obvious timing fingerprint.
            verify_password(password, UserStore._dummy_hash())
            return None
        user = self.get_by_username(username)
        if not user or not user.enabled:
            verify_password(password, UserStore._dummy_hash())
            self._note_failed_login(u_lower)
            return None
        if not verify_password(password, user.password_hash):
            self._note_failed_login(u_lower)
            return None
        # Successful auth — clear the counter and bump last_login.
        self._clear_failed_logins(u_lower)
        return self._login_ok(user, password)

    def authenticate_subsonic_password(self, username: str, password: str) -> User | None:
        """``authenticate`` for a Subsonic ``p=`` password (plain / enc:)
        that missed the O(1) checks — the login password, verified with
        scrypt.  Failures count in their OWN lockout scope ("p"), never the
        main one: a device that keeps sending a retired app password (or an
        old login password) can't lock the owner out of the web UI.  Refused
        while either scope is locked (a locked web login stays locked here
        too).  Success does the same bookkeeping as ``authenticate``."""
        p_key = self._lock_key(username, "p")
        if self._is_locked((username or "").lower())[0] or self._is_locked(p_key)[0]:
            verify_password(password, UserStore._dummy_hash())
            return None
        user = self.get_by_username(username)
        if not user or not user.enabled:
            verify_password(password, UserStore._dummy_hash())
            self._note_failed_login(p_key)
            return None
        if not verify_password(password, user.password_hash):
            self._note_failed_login(p_key)
            return None
        self._clear_failed_logins(p_key)
        return self._login_ok(user, password)

    def _login_ok(self, user: User, password: str) -> User:
        """The success tail shared by ``authenticate`` and
        ``authenticate_subsonic_password`` (``password`` = the verified LOGIN
        password): last-login stamp, the O(1) re-check cache, the Subsonic
        token-mode seed, and a users.json write only when something that
        matters changed."""
        now = time.time()
        user.last_login_at = now
        self._pw_verified[user.id] = (user.password_hash, self._pw_digest(password))
        # ── Subsonic token-mode seed ───────────────────────────────────
        # An account whose token-mode secret was NEVER set (None — created
        # before Subsonic support) gets the password we just verified, so
        # token-mode apps (Amperfy / DSub / Symfonium) work with the same
        # username + password.  A generated Subsonic app password, or an explicit
        # removal (""), is never overwritten here.
        # A secret held back as ciphertext (key unreadable right now) is SET,
        # just unknown — never re-seed over it (it may be a generated app
        # password).
        changed = user.subsonic_password is None and user.id not in self._ss_raw
        if changed:
            user.subsonic_password = password
            self._ss_custom[user.id] = False
        elif user.subsonic_password and user.id not in self._ss_custom:
            # A record older than the custom flag: the plaintext is in hand,
            # so learn it now without any extra hashing.
            self._ss_custom[user.id] = not secrets.compare_digest(
                user.subsonic_password.encode("utf-8"), password.encode("utf-8"))
            changed = True
        # users.json is rewritten only when something that matters changed:
        # the seeded secret / its flag, or a persisted last-login stamp older
        # than _LAST_LOGIN_PERSIST_SEC (a per-request rewrite was measurable
        # I/O on every Subsonic call and moved the file's mtime on read-only
        # requests).
        if changed or now - self._login_persisted.get(user.id, 0.0) >= _LAST_LOGIN_PERSIST_SEC:
            self._save()
        return user

    def _pw_digest(self, password: str) -> bytes:
        import hmac as _hmac
        return _hmac.new(self._pw_key, (password or "").encode("utf-8"), "sha256").digest()

    def check_cached_password(self, username: str, password: str) -> User | None:
        """O(1) check of a login password this process already verified with
        scrypt (``authenticate``) — the user, or None (then run
        ``authenticate``).  Never succeeds for a disabled account, one locked
        out in the main or the Subsonic ``p=`` scope, or after a password
        change (the entry is bound to the password hash)."""
        user = self.get_by_username(username)
        if (user is None or not user.enabled or self.is_locked(username)
                or self.is_locked(username, "p")):
            return None
        hit = self._pw_verified.get(user.id)
        if hit is None or hit[0] != user.password_hash:
            return None
        return user if secrets.compare_digest(hit[1], self._pw_digest(password)) else None

    # ── Sessions ─────────────────────────────────────────────────────────

    def issue_session(self, user_id: str) -> tuple[str, float]:
        token = secrets.token_urlsafe(32)
        expiry = time.time() + _SESSION_TTL_SEC
        self._sessions[token] = {"user_id": user_id, "expiry": expiry}
        return token, expiry

    def lookup_session(self, token: str) -> User | None:
        """Return the active user for a session token, or None.  Refreshes the
        expiry on every successful lookup so active users stay signed in."""
        s = self._sessions.get(token)
        if not s:
            return None
        if time.time() > s["expiry"]:
            self._sessions.pop(token, None)
            return None
        user = self._users.get(s["user_id"])
        if not user or not user.enabled:
            self._sessions.pop(token, None)
            return None
        # Sliding expiry
        s["expiry"] = time.time() + _SESSION_TTL_SEC
        return user

    def revoke_session(self, token: str) -> None:
        self._sessions.pop(token, None)

    def _purge_sessions_for(self, user_id: str) -> None:
        dead = [t for t, s in self._sessions.items() if s["user_id"] == user_id]
        for t in dead:
            self._sessions.pop(t, None)


# ── Singleton accessor ───────────────────────────────────────────────────────

_instance: UserStore | None = None


def init_user_store(data_dir: Path) -> UserStore:
    global _instance
    _instance = UserStore(data_dir)
    return _instance


def get_user_store() -> UserStore:
    if _instance is None:
        raise RuntimeError("UserStore not initialised — call init_user_store() first.")
    return _instance
