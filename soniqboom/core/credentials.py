# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Machine-bound credential encryption using Fernet.

The encryption key is derived deterministically from the machine's identity
(hostname + platform node) so credentials decrypt only on the same machine.
No separate key file is needed.
"""
from __future__ import annotations

import base64
import hashlib
import platform

from cryptography.fernet import Fernet, InvalidToken

_SALT = b"SoniqBoom-credential-store-v1"


def _derive_key() -> bytes:
    identity = f"{platform.node()}:{platform.machine()}".encode()
    raw = hashlib.pbkdf2_hmac("sha256", identity, _SALT, iterations=100_000)
    return base64.urlsafe_b64encode(raw)


def encrypt(plaintext: str) -> str:
    return Fernet(_derive_key()).encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str | None:
    if not ciphertext:
        return None
    try:
        return Fernet(_derive_key()).decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        # The machine-derived key didn't authenticate this ciphertext —
        # most often means the user's machine identity (or hostname) has
        # changed since the share was saved.  Surface it so the operator
        # knows to re-enter the password instead of seeing "share fails"
        # with no clue why.
        import logging
        logging.getLogger(__name__).warning(
            "credentials: stored password could not be decrypted "
            "(machine key mismatch?); the share will need its password re-entered",
        )
        return None
    except Exception:
        return None
