# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""UserStore — load, save, authenticate, and manage user accounts.

Storage layout: ``users.json`` in the data dir, replaced crash-safely on
every mutation (``durable.atomic_write_bytes``: temp file, fsync, rename,
directory fsync), with ``users.json.bak`` holding a second copy of the last
write.  A main file that can't be read falls back to the backup; when neither
holds an account but the install was set up before (``_install_evidence``),
the store is in **recovery** (``recovery_required``): ``has_any()`` stays True
so every gate keeps asking for a sign-in, and no request can succeed until an
admin restores ``users.json`` or creates an account with ``soniqboom-setadm``.
A fresh install (no users file ever) is not affected.

Sessions: the server's store (``init_user_store``) keeps them in
``sessions.json`` (0600) — the SHA-256 of each token, never the token — so a
restart keeps everyone signed in.  New sessions and sliding-expiry advances
are written in the background (coalesced); a revocation (sign-out, password
change, user delete, demotion / disable) is written before the call returns.
A session is bound to the password hash its sign-in verified, so a password
changed by another process (``soniqboom-setadm``), or while the sign-in ran,
ends it too.  An account keeps at most ``_MAX_SESSIONS_PER_USER`` sessions
(the least recently used goes first).

Saves never undo a change another process made: ``_save`` compares
users.json with what this store last read or wrote (``disk_signature`` of the
file) and, when it changed, merges this store's own changes into it — field
by field, the file winning where both changed the same field, a deletion on
either side winning.  A users.json that changed and can't be read is never
overwritten (``UsersFileUnreadable``); one this store could never read is moved
aside (``users.json.corrupt-*``) before the first account list replaces it.

Password hashing: stdlib ``hashlib.scrypt`` with per-user random salt.
The stored hash is ``scrypt$N$r$p$<salt_hex>$<key_hex>`` so the
verification path is self-contained — no Python crypto deps required.

Thread safety: all writes happen on the event loop's single writer
context; the in-memory dicts are GIL-atomic for read access from other
threads, matching the rest of the store.
"""
from __future__ import annotations

import fcntl
import functools
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

from soniqboom.core.durable import atomic_write_bytes
from soniqboom.models.user import User, ROLES, Role

log = logging.getLogger(__name__)

# Shown (log, /api/auth/status, the sign-in overlay, soniqboom-setadm) while
# the store is in recovery — see the module docstring.
RECOVERY_MESSAGE = (
    "The user accounts file (users.json) could not be read and no usable backup "
    "was found, so sign-in is locked. Restore users.json from a backup into the "
    "server's data directory, or create an admin account on the server with: "
    "soniqboom-setadm -user <name> -passwd '<password>' -role admin"
)


class SignInsClosed(RuntimeError):
    """No new sign-in session: the server is restarting
    (``UserStore.close_sign_ins``)."""


class SessionRefused(RuntimeError):
    """No session for this sign-in: the account is gone or disabled, or its
    password changed after the sign-in verified it (``issue_session``)."""


class PasswordChangedMeanwhile(RuntimeError):
    """``set_password(expected_hash=...)`` refused: the account's password
    changed after the caller verified the old one (an administrator's reset,
    or another change of it), so the change would undo that one."""


class UsersFileUnreadable(OSError):
    """users.json changed on disk since this store read it and can't be read
    now: nothing is saved (saving would replace whatever it holds)."""


class _UnreadableUsers(ValueError):
    """users.json (or its backup) holds something that isn't a users document."""


def _parse_users_doc(raw: bytes) -> dict:
    """``raw`` as a users document, or ``_UnreadableUsers``: a JSON object
    whose ``users`` is a list of objects.  ``{}``, ``[]``, ``null`` and other
    valid-JSON garbage are refused here — read as "no users" they would turn a
    damaged file into an install with no accounts."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _UnreadableUsers(f"not JSON ({exc})") from None
    if not isinstance(data, dict) or not isinstance(data.get("users"), list):
        raise _UnreadableUsers("no 'users' list")
    if not all(isinstance(u, dict) for u in data["users"]):
        raise _UnreadableUsers("a user record is not an object")
    return data

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

# Longest display name accepted (the API, the CLI).
DISPLAY_NAME_MAX_LEN = 100

# Sign-in sessions one account keeps; a new one beyond this ends the least
# recently used.
_MAX_SESSIONS_PER_USER = 50

# How often a session's cookie is sent again (``session_cookie_due``): the
# browser's copy follows the sliding expiry, about once an hour.
_COOKIE_REFRESH_SEC = 3600.0

# Temp files ``durable.atomic_write_bytes`` leaves behind when a write is cut
# off: ``<name>.<pid>.<8 hex>.tmp``.
_USERS_TMP_RE = re.compile(r"^users\.json(?:\.bak)?\.\d+\.[0-9a-f]{8}\.tmp$")
_SESSIONS_TMP_RE = re.compile(r"^sessions\.json\.(\d+)\.[0-9a-f]{8}\.tmp$")

_ROLE_RANK = {"readonly": 0, "edit": 1, "admin": 2}

# Hard ceiling on password length — scrypt over a multi-MB blob will block
# the event loop and is never legitimate.
_PASSWORD_MAX_LEN = 1024

# Session token TTL — 7 days, refreshed on every authed request.  Slightly
# longer than the old admin token (1 h) because users expect "stay signed
# in" behaviour from a media player.
_SESSION_TTL_SEC = 7 * 24 * 3600

# sessions.json: new sessions and expiry advances are written this long after
# the first change (one write for a burst of sign-ins); a sliding-expiry
# advance is only written once it is this far past the stored expiry (so an
# active session is rewritten about once an hour, not on every request).
_SESSION_WRITE_DELAY_SEC = 1.0
_SESSION_EXPIRY_PERSIST_SEC = 3600.0
_SESSION_WRITE_RETRY_SEC = 30.0


def _token_hash(token: str) -> str:
    """What identifies a session in memory and on disk: SHA-256 of the token
    (256-bit random, so an unsalted hash is not invertible)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@functools.lru_cache(maxsize=4096)
def _pw_binding(password_hash: str) -> str:
    """A session's binding to the password hash it was issued under — a
    password changed anywhere (another process included) ends it."""
    return hashlib.sha256(b"sb-session\0" + (password_hash or "").encode("utf-8")).hexdigest()[:32]

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


def validate_display_name(display_name: str | None) -> None:
    """Raise ValueError for a display name longer than DISPLAY_NAME_MAX_LEN."""
    if display_name is not None and len(display_name) > DISPLAY_NAME_MAX_LEN:
        raise ValueError(
            f"Display name must be at most {DISPLAY_NAME_MAX_LEN} characters.",
        )


def role_rank(role: str | None) -> int:
    """readonly < edit < admin (an unknown role ranks lowest)."""
    return _ROLE_RANK.get(role or "", -1)


STRONG_PASSWORD_MIN_LEN = 10
STRONG_PASSWORD_RULE = (
    f"Password must be at least {STRONG_PASSWORD_MIN_LEN} characters and include "
    "an uppercase letter, a lowercase letter, a number and a symbol."
)


def is_strong_password(password: str) -> bool:
    """The "Require strong passwords" rule (Settings → Users)."""
    return (len(password) >= STRONG_PASSWORD_MIN_LEN
            and any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
            and any(not c.isalnum() and not c.isspace() for c in password))


def validate_password(password: str) -> None:
    """Reject an empty or over-long password, or — while "Require strong
    passwords" is on (Settings → Users) — one that doesn't meet that rule.
    With the rule off, any length up to the maximum is accepted."""
    if not password:
        raise ValueError("Password can't be empty.")
    if len(password) > _PASSWORD_MAX_LEN:
        raise ValueError(
            f"Password must be at most {_PASSWORD_MAX_LEN} characters.",
        )
    from soniqboom.config import is_strong_passwords_required
    if is_strong_passwords_required() and not is_strong_password(password):
        raise ValueError(STRONG_PASSWORD_RULE)


# ── Store ────────────────────────────────────────────────────────────────────

class UserStore:
    """In-memory user/session manager, persisted to ``users.json``."""

    def __init__(self, data_dir: Path, persist_sessions: bool = False) -> None:
        self._data_dir = data_dir
        self._path = data_dir / "users.json"
        self._bak_path = data_dir / "users.json.bak"
        self._lock_path = data_dir / "users.json.lock"
        # No account could be loaded although the install was set up before
        # (see the module docstring and ``_load``).
        self._recovery_required = False
        # Sessions survive a restart only for the server's store
        # (``init_user_store``): the CLI (``soniqboom-setadm``) builds its own
        # store and must never write the server's sessions.
        self._persist_sessions = persist_sessions
        self._sessions_path = data_dir / "sessions.json"
        self._sess_lock = threading.Lock()      # session-map inserts / removals + snapshots
        self._sess_io_lock = threading.Lock()   # one sessions.json write at a time
        self._sess_dirty = False
        self._sess_timer: threading.Timer | None = None
        self._sign_ins_closed = False           # ``close_sign_ins``
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
        # Sessions: sha256(token) → {user_id, expiry, pw (``_pw_binding``),
        # persisted (the expiry last written to sessions.json)}.
        self._sessions: dict[str, dict] = {}
        # Lockout state: username_lower → {fails: [(ts, ...)], locked_until}.
        # In-memory only; a restart clears all counters which is acceptable
        # (an attacker who can restart the server has bigger leverage).
        self._lockout: dict[str, dict] = {}
        # Guards ``_lockout``: a guess is counted (``_reserve_attempt``)
        # before its scrypt check, so parallel guesses can't all pass a check
        # made before any of them failed.
        self._lockout_lock = threading.Lock()
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
        # What this store last read from / wrote to users.json (``_save``
        # merges a file that changed since): the file's signature, and the
        # account records (plaintext ``User.to_storage()``), API key ids and
        # custom flags as they were then.  ``_main_unknown``: users.json
        # exists but could not be read at the last load — its content is
        # unknown.  ``_bak_sig``: users.json.bak as last written / read.
        self._synced_sig = None
        self._bak_sig = None
        self._base_users: dict[str, dict] = {}
        self._base_keys: dict[str, set[str]] = {}
        self._base_custom: dict[str, bool] = {}
        self._main_unknown = False
        # The last read of users.json / .bak failed with a read error (EIO,
        # EMFILE, permissions): ``check_restored`` tries again on every poll,
        # not only when the file changes.  ``_adopt_logged``: that error, as
        # last logged (one line per distinct error).
        self._read_error = False
        self._adopt_logged = None
        self._load()
        # What users.json / .bak looked like when last read while no account
        # was loaded (``check_restored``): a changed file is read again.
        self._adopt_sig = self.disk_signature()
        if self._persist_sessions:
            self._load_sessions()
        self._remove_stale_temp_files()

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

    def _build_maps(self, data: dict) -> dict:
        """The in-memory maps for a parsed users document (``_parse_users_doc``),
        built aside — ``_swap_maps`` installs them.  Raises on a bad record.

        Scrobble tokens are stored encrypted at rest (Fernet, same key as
        share passwords).  We decrypt here so the in-memory User objects hold
        plaintext for the scrobble path — but the on-disk file never reveals
        them."""
        new_users: dict[str, User] = {}
        new_by_username: dict[str, str] = {}
        new_raw: dict[str, str] = {}
        fer = self._ss_fernet()
        for u in data["users"]:
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
        return {"users": new_users, "by_username": new_by_username, "ss_raw": new_raw,
                "api_keys": new_keys, "by_api_key_hash": new_by_hash, "custom": new_custom}

    def _swap_maps(self, m: dict) -> None:
        # Swap fresh maps in ATOMICALLY rather than clear()+repopulate.
        # ``lookup_session`` reads ``_users`` / ``_by_username`` WITHOUT the
        # lock (it's the hot auth path on every request); the old
        # clear-then-fill exposed an empty-map window during which a
        # concurrent lookup saw "user not found", POPPED the still-valid
        # session, and then returned a *sticky* 401 on every authenticated
        # route until the user signed in again.  A dict rebind is atomic under
        # the GIL, so a lock-free reader always sees either the complete old
        # map or the complete new one — never a partial state.  ``reload()``
        # still holds ``self._lock`` so writers remain serialised.
        if not self._users and m["users"]:
            # Accounts arrive in a store that had none (recovery ended, a
            # restore was adopted): the failed guesses counted meanwhile —
            # every sign-in failed then — start over (Subsonic apps that kept
            # polling would otherwise stay locked out for the cooldown).
            with self._lockout_lock:
                self._lockout.clear()
        self._ss_raw = m["ss_raw"]
        self._users = m["users"]
        self._by_username = m["by_username"]
        self._api_keys = m["api_keys"]
        self._by_api_key_hash = m["by_api_key_hash"]
        self._ss_custom = m["custom"]
        self._login_persisted = {uid: float(u.last_login_at or 0)
                                 for uid, u in m["users"].items()}

    @staticmethod
    def _read_users_file(path: Path) -> tuple[str, dict | None, Exception | None]:
        """``("missing" | "ok" | "bad" | "error", document, error)`` for one
        users file: "bad" = read, but not a users document; "error" = could
        not be read (permissions, EMFILE, EIO) — the file may be fine."""
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return "missing", None, None
        except OSError as exc:
            return "error", None, exc
        try:
            return "ok", _parse_users_doc(raw), None
        except _UnreadableUsers as exc:
            return "bad", None, exc

    def _quarantine(self, path: Path, exc, fatal: bool) -> None:
        """Move an unreadable users file aside (``<name>.corrupt-<ts>``) so an
        admin can investigate and the next save can't silently overwrite it.
        ``fatal``: a main file that can't be moved aside stops the start."""
        ts = int(time.time())
        quarantine = path.with_name(f"{path.name}.corrupt-{ts}")
        if quarantine.exists():
            quarantine = path.with_name(f"{path.name}.corrupt-{ts}-{time.time_ns()}")
        try:
            shutil.move(str(path), str(quarantine))
            log.error("%s could not be read (%s); moved aside to %s",
                      path.name, exc, quarantine.name)
        except OSError:
            if not fatal:
                log.exception("%s could not be read (%s) nor moved aside", path.name, exc)
                return
            log.exception("users.json corrupt and could not be quarantined; aborting")
            raise RuntimeError(
                f"users.json at {path} is corrupt and cannot be moved aside. "
                f"Fix or remove the file before restarting."
            )

    def _install_evidence(self) -> bool:
        """Whether this install was set up before: a users file in any form
        (a quarantined copy ``users.json*.corrupt-*``, a temp file a crash
        left behind) or ``sessions.json`` (only written once accounts exist).
        ``users.json.lock`` doesn't count — the sign-in overlay's "re-check"
        creates it on a fresh install.  A data dir that can't be listed counts
        (fail closed)."""
        try:
            return any((p.name.startswith("users.json") and p.name != "users.json.lock")
                       or p.name == "sessions.json"
                       for p in self._data_dir.iterdir())
        except FileNotFoundError:
            return False
        except OSError:
            return True

    _BAD_RECORD = (KeyError, TypeError, ValueError, AttributeError)

    def _load(self) -> bool:
        """Read the accounts into memory: ``users.json``, else its backup
        ``users.json.bak``.  An unreadable file (bad JSON, valid JSON that
        isn't a users document, a bad record, a read error) is moved aside
        (``<name>.corrupt-<ts>``) — without this the next save would replace
        it and silently wipe whatever it held.

        Neither file holding an account: on a fresh install (no users file
        ever — ``_install_evidence``) nothing changes, the open first-run
        setup as before; otherwise the store is in recovery
        (``recovery_required``, see the module docstring) — never an install
        without accounts.  Accounts already in memory (a ``reload``) are kept
        when nothing could be read.

        Returns False when a file could not be read (and was moved aside);
        a missing file is nothing to load, not a failure."""
        unreadable = False
        empty_main = None
        sig0 = self._file_sig(self._path)       # (before the read: a later change is seen)
        status, data, exc = self._read_users_file(self._path)
        if status == "error":
            # Not readable right now — not proof of damage: nothing is moved
            # aside and the backup (possibly as stale as a file another user
            # just wrote) is not used.  Accounts in memory stay; none → recovery.
            log.error("users.json could not be read (%s)", exc)
            self._read_error = True
            self._main_unknown = True
            self._synced_sig = sig0
            return self._no_accounts_loaded(evidence=True, unreadable=True)
        self._read_error = False
        if status == "ok":
            try:
                maps = self._build_maps(data)
            except self._BAD_RECORD as e:
                status, exc = "bad", e
            else:
                if maps["users"]:
                    self._swap_maps(maps)
                    self._mark_synced(sig0, bak_sig=None)
                    self._recovery_required = False
                    log.info("Loaded %d user(s) from %s", len(self._users), self._path)
                    return True
                empty_main = maps               # a valid document without an account
        if status == "bad":
            unreadable = True
            self._quarantine(self._path, exc, fatal=True)
        bsig0 = self._file_sig(self._bak_path)
        bstatus, bdata, bexc = self._read_users_file(self._bak_path)
        if bstatus == "ok":
            try:
                maps = self._build_maps(bdata)
            except self._BAD_RECORD as e:
                bstatus, bexc = "bad", e
            else:
                if maps["users"]:
                    self._swap_maps(maps)
                    self._mark_synced(self._file_sig(self._path), bak_sig=bsig0)
                    self._recovery_required = False
                    log.error("users.json %s — loaded the %d account(s) of the backup %s",
                              "is missing" if status == "missing" else "could not be read",
                              len(self._users), self._bak_path.name)
                    return True
        if bstatus == "bad":
            unreadable = True
            self._quarantine(self._bak_path, bexc, fatal=False)
        elif bstatus == "error":
            unreadable = True
            self._read_error = True
            log.error("%s could not be read (%s)", self._bak_path.name, bexc)
        if empty_main is not None:
            self._swap_maps(empty_main)
            self._mark_synced(sig0, bak_sig=None)
        elif not self._users:
            self._synced_sig = self._file_sig(self._path)
            self._main_unknown = False
        return self._no_accounts_loaded(
            evidence=status != "missing" or bstatus != "missing" or self._install_evidence(),
            unreadable=unreadable)

    def _no_accounts_loaded(self, evidence: bool, unreadable: bool) -> bool:
        """The end of a ``_load`` that found no account to load: keep the
        accounts in memory if any, else recovery when the install was set up
        before.  The recovery message is logged once per entry into it."""
        if self._users:
            self._recovery_required = False
        else:
            was = self._recovery_required
            self._recovery_required = evidence
            if evidence and not was:
                log.error("No user account could be loaded from %s. %s",
                          self._data_dir, RECOVERY_MESSAGE)
        return not unreadable

    @property
    def recovery_required(self) -> bool:
        """No account is loaded although this install was set up before (its
        users file is unreadable or gone): sign-in stays required and cannot
        succeed until an admin restores users.json or creates an account."""
        return self._recovery_required and not self._users

    def reload(self) -> bool:
        """Re-read users.json from disk.  Used by ``POST /auth/reload``
        after the CLI bootstraps a new admin so the server's in-memory
        singleton sees the new user without a process restart.  False when
        the file could not be read — the in-memory accounts are unchanged."""
        with self._flock(), self._lock:
            return self._load()

    def disk_signature(self) -> tuple:
        """``(inode, size, mtime_ns, ctime_ns)`` of users.json and of
        users.json.bak (None for a file that can't be stat'ed): changes when
        either file is written, replaced, chmod'ed or chown'ed."""
        out = []
        for path in (self._path, self._bak_path):
            try:
                st = path.stat()
            except OSError:
                out.append(None)
                continue
            out.append((st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
        return tuple(out)

    def adopt_from_disk(self, sessions: bool = True) -> bool:
        """Load accounts from disk only when that cannot hurt: users.json
        holding a valid account list (or, with no users.json at all, the
        backup holding one).  Unlike ``reload`` nothing is ever moved aside
        and nothing in memory changes otherwise — a file that is still being
        copied in is left alone and read again later.  True when accounts
        were loaded.  ``sessions``: the sign-ins in sessions.json are read
        again too (as a restart would) — only while no account was loaded."""
        with self._flock(), self._lock:
            sig0 = self._file_sig(self._path)
            status, data, exc = self._read_users_file(self._path)
            bstatus = bdata = bexc = None
            if status == "missing":
                bsig0 = self._file_sig(self._bak_path)
                bstatus, bdata, bexc = self._read_users_file(self._bak_path)
            # A read error (EIO, EMFILE, permissions) may pass: read again at
            # the next poll even if the file doesn't change (``check_restored``).
            self._read_error = status == "error" or bstatus == "error"
            if status in ("error", "bad"):
                # One line per distinct error, not one per poll.
                key = (status, str(exc))
                if key != self._adopt_logged:
                    self._adopt_logged = key
                    log.warning("users.json could not be loaded (%s) — it is read "
                                "again %s", exc,
                                "until it can be" if status == "error" else "when it changes")
            candidates = []
            if status == "ok":
                candidates.append((self._path, data))
            elif status == "missing" and bstatus == "ok":
                candidates.append((self._bak_path, bdata))
            for path, doc in candidates:
                try:
                    maps = self._build_maps(doc)
                except self._BAD_RECORD:
                    continue
                if maps["users"]:
                    had_accounts = bool(self._users)
                    self._swap_maps(maps)
                    if path == self._path:
                        self._mark_synced(sig0, bak_sig=None)
                    else:
                        self._mark_synced(None, bak_sig=bsig0)
                    self._recovery_required = False
                    self._adopt_logged = None
                    log.warning("Loaded %d user account(s) from %s%s", len(self._users), path,
                                "" if had_accounts else
                                " (restored while no account was loaded)")
                    if sessions and self._persist_sessions and not had_accounts:
                        # As a restart would: the sign-ins in sessions.json of
                        # these accounts are valid again (none existed meanwhile).
                        self._load_sessions()
                    return True
            return False

    def check_restored(self) -> bool:
        """While no account is loaded (recovery, or a fresh install): when
        users.json / .bak changed on disk since the last look — or the last
        read of it failed with a read error, which may have passed — try
        ``adopt_from_disk``.  The server calls this every few seconds, so a
        users.json put back from a backup (or one that was unreadable for a
        moment) is picked up without a restart and without any request
        asking for it.  True when accounts were loaded."""
        if self._users:
            return False
        sig = self.disk_signature()
        if sig == self._adopt_sig and not self._read_error:
            return False
        self._adopt_sig = sig
        return self.adopt_from_disk()

    # ── Sync with users.json (``_save`` never undoes another writer) ─────

    @staticmethod
    def _file_sig(path: Path):
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    _UNSET = object()

    def _mark_synced(self, main_sig, bak_sig=_UNSET) -> None:
        """users.json (signature ``main_sig``) now holds exactly the accounts
        in memory: they become the base the next ``_save`` merges against."""
        self._synced_sig = main_sig
        if bak_sig is not UserStore._UNSET:
            self._bak_sig = bak_sig
        self._base_users = {uid: u.to_storage() for uid, u in self._users.items()}
        self._base_keys = {uid: {k["id"] for k in lst} for uid, lst in self._api_keys.items()}
        self._base_custom = dict(self._ss_custom)
        self._main_unknown = False

    def _move_aside(self, path: Path, exc) -> bool:
        """``<name>.corrupt-<ts>`` for a users file that can't be read; False
        when it couldn't be moved (logged)."""
        ts = int(time.time())
        aside = path.with_name(f"{path.name}.corrupt-{ts}")
        if aside.exists():
            aside = path.with_name(f"{path.name}.corrupt-{ts}-{time.time_ns()}")
        try:
            os.replace(path, aside)
        except FileNotFoundError:
            return True
        except OSError:
            log.exception("%s could not be read (%s) nor moved aside", path.name, exc)
            return False
        log.error("%s could not be read (%s); moved aside to %s — a new account "
                  "list is written in its place", path.name, exc, aside.name)
        return True

    def _reconcile_with_disk(self) -> None:
        """Before a save (under the flock and the lock): when users.json is
        not what this store last read or wrote, take in what changed there
        (``_merge_disk``).  A changed users.json that can't be read raises
        ``UsersFileUnreadable`` (it is never overwritten); one this store
        never read an account from is moved aside first — the account list
        written now starts afresh, and the old file is kept."""
        sig = self._file_sig(self._path)
        if not self._main_unknown and sig == self._synced_sig:
            return
        status, doc, exc = self._read_users_file(self._path)
        if status == "ok":
            try:
                disk = self._build_maps(doc)
            except self._BAD_RECORD as e:
                status, exc = "bad", e
            else:
                self._merge_disk(disk)
                return
        if status == "missing":
            return
        if not self._base_users:
            if self._move_aside(self._path, exc):
                self._main_unknown = False
                return
        raise UsersFileUnreadable(
            getattr(exc, "errno", None) or 0,
            f"users.json in {self._data_dir} changed on disk and can't be read ({exc}) "
            "— nothing was saved; fix the file (or its owner / permissions) first")

    def _merge_disk(self, disk: dict) -> None:
        """Merge users.json as it is on disk (``disk``: ``_build_maps``) with
        this store's own changes since the base (``_mark_synced``), in place.
        Per account: one created on either side is kept, one deleted on either
        side is gone; for an account both have, a field changed on disk takes
        the disk's value (a password reset, a disable by ``soniqboom-setadm``
        is never undone), else this store's stays.  API keys and the custom
        flags merge the same way.  A username created on both sides keeps
        the disk's account."""
        base = self._base_users
        dusers: dict[str, User] = disk["users"]
        d_raw: dict[str, str] = disk["ss_raw"]
        out: dict[str, User] = {}
        took_disk: set[str] = set()
        for uid in set(base) | set(self._users) | set(dusers):
            b, m, d = base.get(uid), self._users.get(uid), dusers.get(uid)
            if b is None:
                if d is not None:
                    out[uid] = d                       # created on disk
                    took_disk.add(uid)
                elif m is not None:
                    out[uid] = m                       # created here
                continue
            if d is None or m is None:
                continue                               # deleted on either side
            for f, dv in d.to_storage().items():
                if f == "id":
                    continue
                if f == _SS_FIELD and uid in d_raw:
                    continue                           # (not readable here: keep ours)
                if dv != b.get(f):
                    if f == "last_login_at":
                        mv = getattr(m, f)
                        dv = max(v for v in (dv, mv) if v is not None) if (dv or mv) else dv
                    setattr(m, f, dv)
                    if f == _SS_FIELD:
                        self._ss_raw.pop(uid, None)
            out[uid] = m
        # Usernames stay unique: a name created here that the disk also
        # created meanwhile (another account) gives way.
        by_username: dict[str, str] = {}
        for uid in sorted(out, key=lambda i: (i not in took_disk and i not in base, i)):
            low = out[uid].username.lower()
            if low in by_username:
                log.error("User '%s' was created by another process meanwhile — "
                          "this store's account of that name is dropped", out[uid].username)
                out.pop(uid)
                continue
            by_username[low] = uid
        # API keys: per key id, the same rule.
        keys: dict[str, list[dict]] = {}
        for uid in out:
            bids = self._base_keys.get(uid, set())
            mine = self._api_keys.get(uid) or []
            dlst = disk["api_keys"].get(uid) or []
            dids = {k["id"] for k in dlst}
            mids = {k["id"] for k in mine}
            lst = [k for k in mine if not (k["id"] in bids and k["id"] not in dids)]
            lst += [k for k in dlst if k["id"] not in bids and k["id"] not in mids]
            if lst:
                keys[uid] = lst
        by_hash = {k["sha256"]: (uid, k["id"]) for uid, lst in keys.items() for k in lst}
        custom: dict[str, bool] = {}
        for uid in out:
            bv = self._base_custom.get(uid, UserStore._UNSET)
            dv = disk["custom"].get(uid, UserStore._UNSET)
            v = dv if dv != bv else self._ss_custom.get(uid, UserStore._UNSET)
            if v is not UserStore._UNSET:
                custom[uid] = v
        for uid in took_disk:
            if uid in d_raw:
                self._ss_raw[uid] = d_raw[uid]
        for uid in list(self._ss_raw):
            if uid not in out:
                self._ss_raw.pop(uid, None)
        for uid in took_disk:
            self._login_persisted[uid] = float(out[uid].last_login_at or 0) if uid in out else 0.0
        self._users = out
        self._by_username = by_username
        self._api_keys = keys
        self._by_api_key_hash = by_hash
        self._ss_custom = custom
        log.info("users.json changed on disk since it was read — merged (%d account(s))",
                 len(out))

    def _remove_stale_temp_files(self) -> None:
        """Temp copies a cut-off write left (``durable.atomic_write_bytes``:
        ``users.json[.bak].<pid>.<hex>.tmp``, ``sessions.json.<pid>.<hex>.tmp``)
        hold password hashes / session hashes: remove them — only once accounts
        are loaded (in recovery such a file may be the only copy left) and,
        for users files, under the flock every writer holds while its temp file
        exists."""
        if not self._users:
            return
        try:
            names = list(self._data_dir.iterdir())
        except OSError:
            return
        users_tmp = [q for q in names if _USERS_TMP_RE.match(q.name)]
        sess_tmp = []
        if self._persist_sessions:
            for q in names:
                m = _SESSIONS_TMP_RE.match(q.name)
                if m and int(m.group(1)) != os.getpid():
                    sess_tmp.append(q)
        removed = 0
        if users_tmp:
            try:
                with self._flock():
                    for q in users_tmp:
                        try:
                            q.unlink()
                            removed += 1
                        except OSError:
                            pass
            except OSError:
                pass
        for q in sess_tmp:
            try:
                q.unlink()
                removed += 1
            except OSError:
                pass
        if removed:
            log.info("Removed %d leftover temp file(s) of users.json / sessions.json", removed)

    def _save(self) -> None:
        """Crash-safe write of users.json (``durable.atomic_write_bytes``)
        and then of the same bytes to ``users.json.bak``, under the
        cross-process flock.  A users.json another process changed since this
        store read it is merged first (``_reconcile_with_disk``; raises
        ``UsersFileUnreadable`` for one that changed and can't be read), and a
        backup that can't be read is moved aside before it is replaced.
        Sensitive token fields (last.fm session, ListenBrainz token) are
        encrypted with the machine-bound credentials key before write."""
        with self._flock(), self._lock:
            self._reconcile_with_disk()
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
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Lock down the data dir + lock file too — the QA pass flagged
            # them as relying on umask, which on multi-user UNIX hosts
            # could leak users.json's existence (or contents under a bad
            # umask) to other local users.
            for p, mode in [
                (self._path.parent, 0o700),
                (self._lock_path, 0o600),
            ]:
                try: os.chmod(p, mode)
                except OSError: pass
            payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
            atomic_write_bytes(self._path, payload, 0o600)
            self._mark_synced(self._file_sig(self._path))
            # The backup is written only once the main file is durable, so at
            # every instant at least one of the two holds a complete copy.
            # One that is not the copy this store last wrote and can't be read
            # (another OS user's file, a damaged one) is moved aside first.
            bsig = self._file_sig(self._bak_path)
            write_bak = True
            if bsig is not None and bsig != self._bak_sig:
                bstatus, _bdoc, bexc = self._read_users_file(self._bak_path)
                if bstatus in ("error", "bad"):
                    write_bak = self._move_aside(self._bak_path, bexc)
            if write_bak:
                try:
                    atomic_write_bytes(self._bak_path, payload, 0o600)
                    self._bak_sig = self._file_sig(self._bak_path)
                except OSError:
                    log.warning("%s could not be written", self._bak_path.name, exc_info=True)
            if self._users:
                self._recovery_required = False

    # ── Queries ──────────────────────────────────────────────────────────

    def count(self) -> int:
        return len(self._users)

    def has_any(self) -> bool:
        """Whether sign-in is required: an account exists, or the store is in
        recovery (``recovery_required``).  Every "no users yet → open
        first-run setup" gate keys on this, so recovery keeps them closed.
        ``count()`` is the number of accounts actually loaded."""
        return len(self._users) > 0 or self._recovery_required

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
        self_registered: bool = False,
    ) -> User:
        validate_username(username)
        validate_password(password)
        validate_display_name(display_name)
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
            self_registered=bool(self_registered),
        )
        with self._lock:
            # (checked again under the lock: two creates of one name in two
            # threads — the API runs them off the event loop — both passed
            # the check above)
            if user.username.lower() in self._by_username:
                raise ValueError(f"Username already taken: {username}")
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
        validate_display_name(display_name)
        with self._lock:
            user = self._users.get(user_id)
            if not user:
                raise KeyError(user_id)
            prev_role = user.role
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
            # Demoting (any lower role) or disabling a user kicks out their
            # open sessions — without this, a user demoted from admin to
            # readonly keeps admin-level cookies until their session naturally
            # expires.  A promotion, or the same role again, keeps them.
            should_purge = (
                (role is not None and role_rank(role) < role_rank(prev_role)) or
                (enabled is False)
            )
            if should_purge:
                self._purge_sessions_for(user_id)
        # _save() acquires its own flock; call outside the in-process lock
        # to avoid holding it across IO.
        self._save()
        if should_purge:
            self._write_sessions()          # the revocation is on disk before we return
        return user

    def set_password(self, user_id: str, new_password: str,
                     keep_token: str | None = None, *,
                     expected_hash: str | None = None) -> None:
        """Set ``user_id``'s login password: every session of the account
        ends — except ``keep_token``'s (the caller's own sign-in, when the
        account changes its own password), which stays, bound to the new
        password.

        ``expected_hash`` (an account changing its own password): the password
        hash the caller's old password was verified against
        (``authenticate_with_hash``).  When the account's hash is no longer
        that one — an administrator's reset (or another change) landed after
        the verification — nothing changes and ``PasswordChangedMeanwhile``
        is raised; the check is made under the in-process lock, in the same
        hold as the write, after users.json was taken in (a reset made by
        another process — ``soniqboom-setadm`` — counts too; one that lands on
        disk while this change is saved wins the merge, and is reported the
        same way)."""
        validate_password(new_password)
        if expected_hash is not None:
            # Another process's change of the file first (a setadm reset).
            with self._flock(), self._lock:
                self._reconcile_with_disk()
        new_hash = hash_password(new_password)
        keep_h = _token_hash(keep_token) if keep_token else None
        # The token-mode secret follows the login password only when it IS a
        # copy of it (seeded, never customised): a generated Subsonic app
        # password — or an explicit removal ("") — survives a login-password
        # change.  The custom flag answers that in O(1); only a record whose
        # flag is still unknown pays one scrypt check, on this rare path —
        # outside the in-process lock (other writers, some on the event loop,
        # take it), then the record is re-checked under it and the change
        # applied there (callers run this in worker threads: two changes of
        # one account, or one racing an ``update``, must not interleave).
        for attempt in range(3):
            last = attempt == 2                 # (then checked under the lock)
            user = self._users.get(user_id)
            if not user:
                raise KeyError(user_id)
            seen = (user.subsonic_password, user.password_hash, self._ss_custom.get(user_id),
                    user_id in self._ss_raw)
            sp, cur_hash, known, held = seen
            copy_of_login = (not last and known is None and bool(sp)
                             and verify_password(sp, cur_hash))
            with self._lock:
                user = self._users.get(user_id)
                if not user:
                    raise KeyError(user_id)
                if expected_hash is not None and not secrets.compare_digest(
                        user.password_hash.encode("utf-8"), expected_hash.encode("utf-8")):
                    raise PasswordChangedMeanwhile(user_id)
                now = (user.subsonic_password, user.password_hash, self._ss_custom.get(user_id),
                       user_id in self._ss_raw)
                if now != seen:
                    if not last:
                        continue                # changed meanwhile: look again
                    seen = now
                sp, cur_hash, known, held = seen
                if last:
                    copy_of_login = (known is None and bool(sp)
                                     and verify_password(sp, cur_hash))
                follows = ((sp is None and not held)
                           or (held and known is False)
                           or (bool(sp) and (known is False or copy_of_login)))
                keep_rec = self._sessions.get(keep_h) if keep_h else None
                if keep_rec is not None and keep_rec.get("user_id") != user_id:
                    keep_rec = None
                new_bind = _pw_binding(new_hash)
                if keep_rec is not None:
                    # (a lookup between the next two lines accepts either
                    # binding — the kept session never sees a mismatch)
                    keep_rec["pw_next"] = new_bind
                user.password_hash = new_hash
                if follows:
                    user.subsonic_password = new_password
                    self._ss_custom[user_id] = False
                elif sp and known is None:
                    self._ss_custom[user_id] = True
                self._pw_verified.pop(user_id, None)
                # Force re-login everywhere when password rotates (but on the
                # device that changed it).
                self._purge_sessions_for(user_id, keep=keep_h if keep_rec is not None else None)
                if keep_rec is not None:
                    keep_rec["pw"] = new_bind
                    keep_rec.pop("pw_next", None)
                    self._sess_dirty = True
                break
        self._save()
        self._write_sessions()
        if expected_hash is not None:
            # The save merged users.json: a reset another process wrote
            # meanwhile took the field (``_merge_disk``) — this change lost.
            user = self._users.get(user_id)
            if user is not None and user.password_hash != new_hash:
                raise PasswordChangedMeanwhile(user_id)

    def delete(self, user_id: str) -> None:
        # The check and the removal under one hold of the in-process lock:
        # two deletes in two threads (the API runs them off the event loop)
        # must not both see the other admin and remove the last two.
        with self._lock:
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
            self._users.pop(user_id, None)
            self._by_username.pop(user.username.lower(), None)
            for k in self._api_keys.pop(user_id, None) or ():
                self._by_api_key_hash.pop(k["sha256"], None)
                self._api_key_last_used.pop(k["id"], None)
            self._pw_verified.pop(user_id, None)
            self._ss_custom.pop(user_id, None)
        self._purge_sessions_for(user_id)
        self._save()
        self._write_sessions()
        log.info("Deleted user '%s'", user.username)

    # ── OpenSubsonic API keys ────────────────────────────────────────────

    def create_api_key(self, user_id: str, name: str = "") -> tuple[str, dict]:
        """Mint a key for ``user_id``: ``(plaintext, public entry)``.  The
        plaintext exists only in this return value — users.json keeps its
        sha256.  Raises ``KeyError`` for an unknown user, ``ValueError`` at
        the per-user cap."""
        key = secrets.token_urlsafe(32)          # 256 bits of entropy
        ent = {"id": secrets.token_hex(8),
               "name": " ".join((name or "").split())[:_API_KEY_NAME_MAX],
               "sha256": _api_key_hash(key), "created": time.time()}
        with self._lock:
            # (under the lock: a delete — another thread — can't leave a key
            # behind for an account that is gone)
            if user_id not in self._users:
                raise KeyError(user_id)
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
        with self._lockout_lock:
            self._count_attempt(username_lower, time.time())

    def _count_attempt(self, username_lower: str, now: float) -> None:
        """One guess against ``username_lower`` (``_lockout_lock`` held)."""
        rec = self._lockout.setdefault(username_lower, {"fails": [], "locked_until": 0.0})
        # Drop fails outside the rolling window.
        rec["fails"] = [t for t in rec["fails"] if now - t < _LOCKOUT_WINDOW_SEC]
        rec["fails"].append(now)
        if len(rec["fails"]) >= _LOCKOUT_MAX_ATTEMPTS and rec["locked_until"] <= now:
            rec["locked_until"] = now + _LOCKOUT_COOLDOWN_SEC
            log.warning(
                "Account '%s' locked for %ds after %d failed login attempts",
                username_lower.split("\0", 1)[0], _LOCKOUT_COOLDOWN_SEC, len(rec["fails"]),
            )

    def _reserve_attempt(self, username_lower: str, also_locked: str | None = None) -> bool:
        """Count a guess BEFORE it is checked — under one lock with the
        lockout check, so a parallel burst gets exactly the guesses a serial
        one would: False (refused, not counted) while ``username_lower`` (or
        ``also_locked``) is locked.  A guess that then succeeds clears the
        count (``_clear_failed_logins``); one that fails stays counted."""
        with self._lockout_lock:
            now = time.time()
            for key in (username_lower, also_locked):
                if key is not None:
                    rec = self._lockout.get(key)
                    if rec and rec.get("locked_until", 0.0) > now:
                        return False
            self._count_attempt(username_lower, now)
            return True

    def _clear_failed_logins(self, username_lower: str) -> None:
        with self._lockout_lock:
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
        hit = self.authenticate_with_hash(username, password)
        return hit[0] if hit else None

    def authenticate_with_hash(self, username: str, password: str) -> tuple[User, str] | None:
        """``authenticate``, also answering the password hash the password was
        verified against — what a sign-in session is bound to
        (``issue_session``), so a password reset racing the sign-in ends it."""
        u_lower = (username or "").lower()
        if not self._reserve_attempt(u_lower):
            # Burn the same scrypt time as a real check so locked accounts
            # don't have an obvious timing fingerprint.
            verify_password(password, UserStore._dummy_hash())
            return None
        user = self.get_by_username(username)
        if not user or not user.enabled:
            verify_password(password, UserStore._dummy_hash())
            return None
        verified = user.password_hash
        if not verify_password(password, verified):
            return None
        # Successful auth — clear the counter and bump last_login.
        self._clear_failed_logins(u_lower)
        return self._login_ok(user, password, verified), verified

    def authenticate_subsonic_password(self, username: str, password: str) -> User | None:
        """``authenticate`` for a Subsonic ``p=`` password (plain / enc:)
        that missed the O(1) checks — the login password, verified with
        scrypt.  Failures count in their OWN lockout scope ("p"), never the
        main one: a device that keeps sending a retired app password (or an
        old login password) can't lock the owner out of the web UI.  Refused
        while either scope is locked (a locked web login stays locked here
        too).  Success does the same bookkeeping as ``authenticate``."""
        p_key = self._lock_key(username, "p")
        if not self._reserve_attempt(p_key, also_locked=(username or "").lower()):
            verify_password(password, UserStore._dummy_hash())
            return None
        user = self.get_by_username(username)
        if not user or not user.enabled:
            verify_password(password, UserStore._dummy_hash())
            return None
        verified = user.password_hash
        if not verify_password(password, verified):
            return None
        self._clear_failed_logins(p_key)
        return self._login_ok(user, password, verified)

    def _login_ok(self, user: User, password: str, verified: str | None = None) -> User:
        """The success tail shared by ``authenticate`` and
        ``authenticate_subsonic_password`` (``password`` = the verified LOGIN
        password): last-login stamp, the O(1) re-check cache, the Subsonic
        token-mode seed, and a users.json write only when something that
        matters changed."""
        now = time.time()
        user.last_login_at = now
        self._pw_verified[user.id] = (verified or user.password_hash, self._pw_digest(password))
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
            try:
                self._save()
            except OSError:
                # (users.json unreadable / unwritable right now: the sign-in
                # itself stands — the stamp is written by a later save)
                log.error("users.json not written after a sign-in", exc_info=True)
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
    #
    # Keyed by ``_token_hash(token)`` — the raw token exists only in the
    # client's cookie.  Inserts and removals of ``_sessions`` take
    # ``_sess_lock`` (the background writer snapshots the map under it); the
    # hot read path (``lookup_session``) reads without it.

    def issue_session(self, user_id: str, pw_hash: str | None = None) -> tuple[str, float]:
        """A new sign-in session, bound to ``pw_hash`` — the password hash the
        sign-in verified (``authenticate_with_hash``; default: the account's
        current one).  Checked under the session lock: ``SessionRefused`` when
        the account is gone or disabled or its password is no longer
        ``pw_hash`` (a reset that raced the sign-in — the reset's purge then
        has nothing to miss); ``SignInsClosed`` once ``close_sign_ins`` ran (a
        session is either issued before the close — and in the final sessions
        write — or refused).  The account's least recently used sessions go
        beyond ``_MAX_SESSIONS_PER_USER``."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        expiry = now + _SESSION_TTL_SEC
        with self._sess_lock:
            if self._sign_ins_closed:
                raise SignInsClosed("the server is restarting")
            user = self._users.get(user_id)
            if user is None or not user.enabled:
                raise SessionRefused("the account is gone or disabled")
            cur = user.password_hash
            if pw_hash is not None and pw_hash != cur:
                raise SessionRefused("the password changed during the sign-in")
            mine = [(s["expiry"], h) for h, s in self._sessions.items()
                    if s["user_id"] == user_id]
            if len(mine) >= _MAX_SESSIONS_PER_USER:
                mine.sort()
                for _exp, h in mine[:len(mine) - _MAX_SESSIONS_PER_USER + 1]:
                    self._sessions.pop(h, None)
            self._sessions[_token_hash(token)] = {
                "user_id": user_id, "expiry": expiry, "pw": _pw_binding(cur),
                "persisted": 0.0, "cookie_at": now}
        self._schedule_session_write()
        return token, expiry

    def session_cookie_due(self, token: str) -> bool:
        """Whether the browser's cookie for this live session should be sent
        again now (its Max-Age follows the sliding expiry): True at most once
        per ``_COOKIE_REFRESH_SEC`` per session — the first call after a
        restart included."""
        if not token or not isinstance(token, str):
            return False
        s = self._sessions.get(_token_hash(token))
        if not s:
            return False
        now = time.time()
        if now > s["expiry"] or now - s.get("cookie_at", 0.0) < _COOKIE_REFRESH_SEC:
            return False
        s["cookie_at"] = now
        return True

    def close_sign_ins(self) -> None:
        """No new session from now on (``issue_session`` refuses): the source
        install's restart, before its final ``flush_sessions``."""
        with self._sess_lock:
            self._sign_ins_closed = True

    def lookup_session(self, token: str) -> User | None:
        """Return the active user for a session token, or None.  Refreshes the
        expiry on every successful lookup so active users stay signed in."""
        if not token or not isinstance(token, str):
            return None
        h = _token_hash(token)
        s = self._sessions.get(h)
        if not s:
            return None
        now = time.time()
        if now > s["expiry"]:
            self._drop_session(h)
            return None
        user = self._users.get(s["user_id"])
        if not user or not user.enabled:
            self._drop_session(h)
            return None
        pw = s.get("pw")
        if pw is not None:
            cur = _pw_binding(user.password_hash)
            if pw != cur and s.get("pw_next") != cur:
                # The password changed since this session began (here, the
                # in-process paths already purged it — this catches a change
                # made by another process, e.g. soniqboom-setadm + reload, and
                # a sign-in that raced a reset).
                self._drop_session(h)
                return None
        # Sliding expiry — written back only once it has moved far enough.
        s["expiry"] = now + _SESSION_TTL_SEC
        if (self._persist_sessions and not self._sess_dirty
                and s["expiry"] - s.get("persisted", 0.0) >= _SESSION_EXPIRY_PERSIST_SEC):
            self._schedule_session_write()
        return user

    def revoke_session(self, token: str) -> None:
        """Sign-out: the session is gone from memory and from sessions.json
        when this returns (it does file I/O — call it off the event loop)."""
        if not token or not isinstance(token, str):
            return
        with self._sess_lock:
            gone = self._sessions.pop(_token_hash(token), None) is not None
        if gone:
            self._write_sessions()

    def _drop_session(self, h: str) -> None:
        """Forget a session found dead at lookup (expired, user gone or
        disabled, password changed).  Written lazily: the same checks run on
        every session restored from disk."""
        with self._sess_lock:
            gone = self._sessions.pop(h, None) is not None
        if gone:
            self._schedule_session_write()

    def _purge_sessions_for(self, user_id: str, keep: str | None = None) -> None:
        """Drop every session of ``user_id`` (but ``keep``, a token hash) from
        memory.  The caller writes sessions.json (``_write_sessions``) once
        its own save is done."""
        with self._sess_lock:
            dead = [h for h, s in self._sessions.items()
                    if s["user_id"] == user_id and h != keep]
            for h in dead:
                self._sessions.pop(h, None)
            if dead:
                self._sess_dirty = True

    def _schedule_session_write(self, delay: float | None = None) -> None:
        """Write sessions.json ``delay`` seconds (default
        ``_SESSION_WRITE_DELAY_SEC``) from now in a background thread,
        coalescing every change made meanwhile into that one write."""
        if not self._persist_sessions:
            return
        if delay is None:
            delay = _SESSION_WRITE_DELAY_SEC
        with self._sess_lock:
            self._sess_dirty = True
            if self._sess_timer is not None:
                return
            t = threading.Timer(delay, self._timer_write)
            t.daemon = True
            self._sess_timer = t
        t.start()

    def _timer_write(self) -> None:
        with self._sess_lock:
            self._sess_timer = None
        if self._sess_dirty:
            self._write_sessions()           # (re-schedules itself on failure)

    def flush_sessions(self) -> bool:
        """Write sessions.json now if anything changed since the last write
        (the server's stop / restart).  Blocking file I/O.  A coalesced write
        already under way is waited for first: it cleared the change flag
        before writing, and the source install's restart replaces the process
        right after this returns."""
        with self._sess_io_lock:
            if not self._sess_dirty:
                return True
        return self._write_sessions()

    def _write_sessions(self) -> bool:
        """Durably write the current sessions to sessions.json (0600): one
        row per live session — the token's SHA-256, user id, expiry and
        password binding.  Expired sessions are left out.  Nothing is written
        while no account is loaded (recovery), so a restart after the accounts
        are restored still finds the sessions (a ``reload`` doesn't re-read
        them).  False when the write failed (logged, retried in the
        background)."""
        if not self._persist_sessions:
            return True
        with self._sess_io_lock:
            if not self._users:
                return True
            now = time.time()
            with self._sess_lock:
                self._sess_dirty = False
                for h in [h for h, s in self._sessions.items() if s["expiry"] <= now]:
                    self._sessions.pop(h, None)
                rows = []
                for h, s in self._sessions.items():
                    exp = s["expiry"]
                    rows.append({"h": h, "uid": s["user_id"], "exp": exp, "pw": s.get("pw")})
                    s["persisted"] = exp
            payload = json.dumps({"version": 1, "sessions": rows},
                                 separators=(",", ":")).encode("utf-8")
            try:
                atomic_write_bytes(self._sessions_path, payload, 0o600)
            except OSError:
                log.error("Could not write %s — retried in %.0f s", self._sessions_path.name,
                          _SESSION_WRITE_RETRY_SEC, exc_info=True)
            else:
                return True
        # Retried in the background (a revocation included: until it is
        # written, a crash would bring that session back).
        self._schedule_session_write(_SESSION_WRITE_RETRY_SEC)
        return False

    def _load_sessions(self) -> None:
        """Restore sessions.json: only sessions still valid now — not
        expired, user present and enabled, password unchanged.  An unreadable
        file means nobody stays signed in (logged), never a failed start."""
        try:
            raw = self._sessions_path.read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("Could not read %s (%s) — everyone signs in again",
                        self._sessions_path.name, exc)
            return
        try:
            data = json.loads(raw.decode("utf-8"))
            rows = data["sessions"]
            if not isinstance(rows, list):
                raise TypeError("sessions is not a list")
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("%s is unreadable (%s) — everyone signs in again",
                        self._sessions_path.name, exc)
            return
        now = time.time()
        restored: dict[str, dict] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            h, uid, exp, pw = r.get("h"), r.get("uid"), r.get("exp"), r.get("pw")
            if not (isinstance(h, str) and len(h) == 64 and isinstance(uid, str)
                    and isinstance(exp, (int, float)) and not isinstance(exp, bool)
                    and (pw is None or isinstance(pw, str))):
                continue
            if exp <= now:
                continue
            user = self._users.get(uid)
            if user is None or not user.enabled:
                continue
            if pw is not None and pw != _pw_binding(user.password_hash):
                continue
            # Never longer than a fresh session (a hand-edited or future-dated file).
            exp = min(float(exp), now + _SESSION_TTL_SEC)
            restored[h] = {"user_id": uid, "expiry": exp, "pw": pw, "persisted": exp}
        with self._sess_lock:
            self._sessions = restored
        if restored:
            log.info("Restored %d session(s) from %s", len(restored), self._sessions_path.name)


# ── Singleton accessor ───────────────────────────────────────────────────────

_instance: UserStore | None = None


def init_user_store(data_dir: Path) -> UserStore:
    """The server's store: its sessions persist in ``sessions.json``.  A
    previous store of this process (the bundled app restarts the server in
    the same process) writes its pending sessions first and then never
    writes again — a late coalesced write must not overwrite the new store's."""
    global _instance
    old = _instance
    if old is not None:
        try:
            old.flush_sessions()
        except Exception:
            log.exception("Could not write the previous run's sessions")
        old._persist_sessions = False
    _instance = UserStore(data_dir, persist_sessions=True)
    return _instance


def legacy_owner_id(users) -> str | None:
    """The account the listening data of a database without per-user data
    goes to (``core/user_data``): the oldest admin by ``created_at`` (an
    enabled one first), else the oldest account; None without accounts.
    ``users``: ``User`` objects or stored user records."""
    def _f(u, name, default=None):
        return u.get(name, default) if isinstance(u, dict) else getattr(u, name, default)

    def _created(u) -> float:
        try:
            return float(_f(u, "created_at", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    cands = [u for u in users or () if isinstance(_f(u, "id"), str) and _f(u, "id")]
    for pick in (lambda u: _f(u, "role") == "admin" and _f(u, "enabled", True) is not False,
                 lambda u: _f(u, "role") == "admin",
                 lambda u: True):
        hit = [u for u in cands if pick(u)]
        if hit:
            return _f(min(hit, key=_created), "id")
    return None


def accounts_on_disk(data_dir: Path) -> list[dict]:
    """The stored account records of ``users.json`` (else its backup
    ``users.json.bak``), read without loading a store — nothing is moved
    aside or written (the database upgrade at a start runs before the user
    store is loaded; the command-line import has no store).  Empty when
    neither file holds an account."""
    for path in (data_dir / "users.json", data_dir / "users.json.bak"):
        status, data, _exc = UserStore._read_users_file(path)
        if status == "ok" and data.get("users"):
            return list(data["users"])
    return []


def accounts_locked() -> bool:
    """The server's store is in recovery (``UserStore.recovery_required``):
    no account can be loaded on an install that had them, so nothing is
    served — not even the anonymous services an admin turned on (the DLNA
    Media Server).  False before the store exists (early boot)."""
    st = _instance
    return st is not None and st.recovery_required


def get_user_store() -> UserStore:
    if _instance is None:
        raise RuntimeError("UserStore not initialised — call init_user_store() first.")
    return _instance
