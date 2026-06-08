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

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._\-]{2,64}$")
_PASSWORD_MIN_LEN = 8
# Hard ceiling on password length — scrypt over a multi-MB blob will block
# the event loop and is never legitimate.
_PASSWORD_MAX_LEN = 1024

# Session token TTL — 7 days, refreshed on every authed request.  Slightly
# longer than the old admin token (1 h) because users expect "stay signed
# in" behaviour from a media player.
_SESSION_TTL_SEC = 7 * 24 * 3600


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
        # Users keyed by id (UUID), with a username→id index for fast lookup.
        self._users: dict[str, User] = {}
        self._by_username: dict[str, str] = {}
        # Sessions: token → {user_id, expiry}.  In-memory only.
        self._sessions: dict[str, dict] = {}
        # Lockout state: username_lower → {fails: [(ts, ...)], locked_until}.
        # In-memory only; a restart clears all counters which is acceptable
        # (an attacker who can restart the server has bigger leverage).
        self._lockout: dict[str, dict] = {}
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

    def _load(self) -> None:
        """Read users.json into memory.  On a parse error we move the
        corrupt file aside (``users.json.corrupt-<ts>``) and continue with
        an empty store.  Without this, the next save() would atomically
        replace the unreadable but on-disk file, silently wiping
        whatever legitimate users.json may have been (e.g. partial write,
        version mismatch).

        Scrobble tokens are stored encrypted at rest (Fernet, same key as
        share passwords).  We decrypt on load so the in-memory User
        objects hold plaintext for the scrobble path — but the on-disk
        file never reveals them."""
        if not self._path.exists():
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            users = data.get("users", [])
            self._users.clear()
            self._by_username.clear()
            for u in users:
                _decrypt_token_fields(u)
                user = User.from_storage(u)
                self._users[user.id] = user
                self._by_username[user.username.lower()] = user.id
            log.info("Loaded %d user(s) from %s", len(self._users), self._path)
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

    def reload(self) -> None:
        """Re-read users.json from disk.  Used by ``POST /auth/reload``
        after the CLI bootstraps a new admin so the server's in-memory
        singleton sees the new user without a process restart."""
        with self._flock(), self._lock:
            self._load()

    def _save(self) -> None:
        """Atomic write of users.json under the cross-process flock.
        Sensitive token fields (last.fm session, ListenBrainz token) are
        encrypted with the machine-bound credentials key before write."""
        with self._flock(), self._lock:
            data = {
                "version": 1,
                "users": [_encrypt_token_fields(u.to_storage())
                          for u in self._users.values()],
            }
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
        return self._users.get(user_id)

    def get_by_id(self, user_id: str) -> User | None:
        """Alias for :meth:`get` — look up a user by their immutable id.

        ``cast_stream``'s token re-auth check probes for a ``get_by_id``
        method (the cast token's ``uid`` claim is the user *id*, via
        ``cast._user_field``).  Without this method that probe silently
        no-ops and the fallback ``get_by_username(uid)`` can't resolve a
        UUID, so the re-auth check rejected EVERY cast token with a 404
        ("Stream link no longer valid") — i.e. no cast ever streamed.
        """
        return self._users.get(user_id)

    def get_by_username(self, username: str) -> User | None:
        uid = self._by_username.get((username or "").lower())
        return self._users.get(uid) if uid else None

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
        self._save()
        log.info("Created user '%s' (role=%s)", user.username, user.role)
        return user

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
            # subsonic_password: same convention — caller passes "" to
            # clear or a non-empty string to set; ``None`` means "don't
            # touch this field".
            if subsonic_password is not None:
                user.subsonic_password = subsonic_password or None
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
        user.password_hash = hash_password(new_password)
        # Keep the Subsonic-compat field in sync — see ``authenticate``
        # for rationale.  When the user rotates their main password, the
        # Subsonic clients should track the change without a separate
        # rotation step.
        user.subsonic_password = new_password
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
        self._purge_sessions_for(user_id)
        self._save()
        log.info("Deleted user '%s'", user.username)

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
        user.last_login_at = time.time()
        # ── Gold-standard Subsonic compat ──────────────────────────────
        # Capture the plaintext we just verified and stash it as
        # ``subsonic_password`` if not already set OR if it's drifted
        # from the current main password (user changed pw elsewhere).
        # This means a single login via the web UI lights up token-mode
        # auth for Amperfy / DSub / Symfonium — clients use the SAME
        # username + password they already have for browser login,
        # matching the Navidrome / Gonic single-credential UX.
        # Plain-mode auth still works for the very first attempt before
        # the user has ever logged in via the browser (it verifies via
        # scrypt directly).
        if user.subsonic_password != password:
            user.subsonic_password = password
        self._save()
        return user

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
