# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Machine-bound credential encryption using Fernet.

The encryption key is derived deterministically from the machine's identity
(hostname + platform node) so credentials decrypt only on the same machine.
No separate key file is needed.

``SONIQBOOM_CREDENTIAL_KEY_FILE`` (set by the Docker image): the key is read
from that file instead — created with a random key on first use — because a
container's host name changes every time it is recreated.  Values encrypted
with the machine key still decrypt (and are re-encrypted with the file's key
on their next save).
"""
from __future__ import annotations

import base64
import functools
import hashlib
import logging
import os
import platform
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_SALT = b"SoniqBoom-credential-store-v1"


log = logging.getLogger(__name__)
_KEY_FILE_ENV = "SONIQBOOM_CREDENTIAL_KEY_FILE"
_key_file_lock = threading.Lock()
_key_file_cache: dict[str, bytes | None] = {}


def _machine_key() -> bytes:
    return _derive_key_for(f"{platform.node()}:{platform.machine()}")


def _valid_key(key: bytes) -> bool:
    try:
        Fernet(key)
        return True
    except Exception:
        return False


def _create_key_file(p: Path) -> bytes | None:
    """Write a new random key to ``p`` — complete or not at all: written and
    fsync'd under a temporary name, then hard-linked into place (fails if
    another process made ``p`` meanwhile; its key is used then)."""
    key = Fernet.generate_key()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, p)
        except FileExistsError:
            other = p.read_bytes().strip()
            return other if _valid_key(other) else None
        return key
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _file_key(path: str) -> bytes | None:
    """The Fernet key kept in ``path`` (created, mode 0600, when there is
    none; an empty or invalid file — a write cut short — is moved aside and
    replaced).  None when no usable key can be had: the machine key is used
    then, and that is logged once."""
    with _key_file_lock:
        if path in _key_file_cache:
            return _key_file_cache[path]
        p = Path(path)
        key: bytes | None = None
        try:
            try:
                key = p.read_bytes().strip()
            except FileNotFoundError:
                key = None
            if key is not None and not _valid_key(key):
                aside = p.with_name(f"{p.name}.invalid-{int(time.time())}")
                log.warning("credentials: %s does not hold a valid key — moved to %s, "
                            "a new key is made", p, aside.name)
                os.replace(p, aside)
                key = None
            if key is None:
                key = _create_key_file(p)
        except OSError as exc:
            log.warning("credentials: key file %s unusable (%s) — using the machine key",
                        p, exc)
            key = None
        if key is None:
            log.warning("credentials: no usable key in %s — using the machine key", p)
        _key_file_cache[path] = key
        return key


def _derive_key() -> bytes:
    """The key new values are encrypted with."""
    path = os.environ.get(_KEY_FILE_ENV, "")
    if path:
        key = _file_key(path)
        if key is not None:
            return key
    return _machine_key()


def _decrypt_keys() -> list[bytes]:
    """The keys a stored value may have been encrypted with, newest first."""
    keys = [_derive_key()]
    machine = _machine_key()
    if machine not in keys:
        keys.append(machine)
    return keys


@functools.lru_cache(maxsize=4)
def _derive_key_for(identity: str) -> bytes:
    """The key for one machine identity — memoised: the derivation is a
    100k-iteration PBKDF2 (~11 ms), and users.json encrypts / decrypts a
    few fields per user on every save / load."""
    raw = hashlib.pbkdf2_hmac("sha256", identity.encode(), _SALT, iterations=100_000)
    return base64.urlsafe_b64encode(raw)


def encrypt(plaintext: str) -> str:
    return Fernet(_derive_key()).encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str | None:
    if not ciphertext:
        return None
    try:
        return MultiFernet([Fernet(k) for k in _decrypt_keys()]).decrypt(
            ciphertext.encode()).decode()
    except InvalidToken:
        # The machine-derived key didn't authenticate this ciphertext —
        # most often means the user's machine identity (or hostname) has
        # changed since the share was saved.  Surface it so the operator
        # knows to re-enter the password instead of seeing "share fails"
        # with no clue why.
        log.warning(
            "credentials: stored password could not be decrypted "
            "(machine key mismatch?); the share will need its password re-entered",
        )
        return None
    except Exception:
        return None
