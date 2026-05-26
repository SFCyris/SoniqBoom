# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""User account model.

A SoniqBoom user is identified by ``id`` (UUID) and addressed by ``username``.
Three roles gate what they can do:

* ``admin``    — full access; manages users, settings, library config.
* ``edit``     — can play, rate, build playlists, edit track metadata.
                  Cannot manage users or change global settings.
* ``readonly`` — can play, rate, and build *personal* playlists; cannot
                  edit metadata or settings.

Password storage uses stdlib ``hashlib.scrypt`` (memory-hard, NIST-recommended).
The stored hash string is self-describing: ``scrypt$N$r$p$<salt_hex>$<key_hex>``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal


Role = Literal["admin", "edit", "readonly"]
ROLES: tuple[Role, ...] = ("admin", "edit", "readonly")


@dataclass
class User:
    id: str
    username: str
    password_hash: str
    role: Role
    created_at: float
    enabled: bool = True
    display_name: str | None = None
    last_login_at: float | None = None
    # Per-user scrobble tokens (filled when the user enables scrobbling
    # under Settings → My Account in the UI).  Kept here so they migrate
    # with the user record on backup/restore.
    listenbrainz_token: str | None = None
    lastfm_session_key: str | None = None
    # Optional Subsonic-API password.  Stored in plaintext because the
    # Subsonic spec's token auth (``?u&s&t``) requires the server to
    # compute ``md5(password + salt)`` to verify — that's incompatible
    # with the scrypt hash used for browser login.  Letting users opt
    # into a separate password (the convention every Subsonic-compatible
    # server uses: Navidrome, Airsonic, Funkwhale, Gonic) means we never
    # need to keep the *main* password plaintext.  Empty/None → token
    # auth disabled for this user; they can still browser-login normally.
    subsonic_password: str | None = None

    def to_public(self) -> dict:
        """Fields safe to return over the API (no password hash)."""
        d = asdict(self)
        d.pop("password_hash", None)
        # Tokens are sensitive — only return whether they're set, not the value.
        d["listenbrainz_token"] = bool(d.get("listenbrainz_token"))
        d["lastfm_session_key"] = bool(d.get("lastfm_session_key"))
        # Don't leak the Subsonic plaintext password over the API — just
        # whether one is configured.  The user can rotate via the
        # ``PUT /api/users/{id}/subsonic-password`` endpoint if they
        # forget it.
        d["subsonic_password"] = bool(d.pop("subsonic_password", None))
        return d

    def to_storage(self) -> dict:
        """Fields persisted to users.json on disk."""
        return asdict(self)

    @classmethod
    def from_storage(cls, d: dict) -> "User":
        # Tolerate older records that pre-date some fields.
        return cls(
            id=d["id"],
            username=d["username"],
            password_hash=d["password_hash"],
            role=d.get("role", "readonly"),
            created_at=float(d.get("created_at", 0.0)),
            enabled=bool(d.get("enabled", True)),
            display_name=d.get("display_name"),
            last_login_at=d.get("last_login_at"),
            listenbrainz_token=d.get("listenbrainz_token"),
            lastfm_session_key=d.get("lastfm_session_key"),
            subsonic_password=d.get("subsonic_password"),
        )
