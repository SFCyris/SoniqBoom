# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""``soniqboom-setadm`` — create or promote an admin user from the CLI.

Designed to be safe to run on the same machine without an HTTP request,
which is intentionally the only way to bootstrap the very first admin on
a fresh install (registration via the UI is locked until at least one
admin exists, so an unauthenticated visitor can't make themselves one).

Usage::

    soniqboom-setadm -user alice -passwd 's3cret-9-things'
    soniqboom-setadm -user alice -passwd s3cret -role edit
    soniqboom-setadm -user alice -passwd s3cret -disable

If the user exists, the password and role are updated in place.  If not,
a new user is created with the requested role (defaulting to ``admin``).
"""
from __future__ import annotations

import argparse
import os
import sys

from soniqboom.config import get_data_dir
from soniqboom.core.users import (
    UserStore,
    validate_password,
    validate_username,
)
from soniqboom.models.user import ROLES


# Single-dash long-option style (-user, -passwd, ...) is intentional — the
# spec asked for it.  Argparse, however, treats a single-dash multi-char token
# as a long option *with prefix matching*, and that matching is NOT disabled
# by ``allow_abbrev=False`` (a long-standing CPython quirk).  Without the
# guard below, ``-p secret`` would silently bind to ``-passwd``, which is the
# kind of surprise that produces "I disabled the account but it set the
# password instead" bug reports.
_VALID_FLAGS = frozenset({
    "-user", "--user",
    "-passwd", "--passwd",
    "-role", "--role",
    "-display-name", "--display-name",
    "-disable", "--disable",
    "-enable", "--enable",
    "-h", "--help",
})


def _reject_prefix_matches(argv: list[str]) -> None:
    """Abort if any ``-flag`` token isn't an exact match for a defined flag.

    Runs before argparse so a prefix like ``-p`` can't silently bind to
    ``-passwd``.  Bare values (no leading dash) and ``--`` separators pass
    through untouched."""
    for tok in argv:
        if tok == "--" or not tok.startswith("-") or tok == "-":
            continue
        # Strip ``-flag=value`` form for the membership test.
        head = tok.split("=", 1)[0]
        if head not in _VALID_FLAGS:
            print(
                f"error: unknown flag {head!r}.  Valid flags: "
                "-user, -passwd, -role, -display-name, -disable, -enable.",
                file=sys.stderr,
            )
            print(
                "hint: argparse won't reject prefixes like '-p' for '-passwd' "
                "on its own — spell the flag in full.",
                file=sys.stderr,
            )
            raise SystemExit(2)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="soniqboom-setadm",
        description=(
            "Create or modify a SoniqBoom user.  Run on the server host. "
            "The first run on a fresh install bootstraps the admin account; "
            "subsequent runs can update password, role, or enabled state. "
            "\n\nExamples:"
            "\n  soniqboom-setadm -user alice -passwd 'changeme123'"
            "\n  soniqboom-setadm -user alice -passwd 'newer-pw-9' -role admin"
            "\n  soniqboom-setadm -user bob -disable"
        ),
        # Disable prefix-matching for double-dash flags; single-dash prefix
        # matching is handled out-of-band by ``_reject_prefix_matches``
        # (allow_abbrev does not cover that case).
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Single-dash style preserved (the user explicitly asked for
    # `setAdm -user <username> -passwd <password>`).  Double-dash variants
    # are accepted as aliases for users who muscle-memory GNU conventions.
    p.add_argument("-user",   "--user",   required=True, dest="user",
                   help="Username (2-64 chars, letters/digits/._-).")
    p.add_argument("-passwd", "--passwd", dest="passwd",
                   help="Password (>= 8 chars).  Omit to keep the existing "
                        "password on an existing user.")
    p.add_argument("-role",   "--role",   dest="role", choices=ROLES, default=None,
                   help="Role to assign.  Defaults to 'admin' for new users; "
                        "leaves the existing role untouched on an update.")
    p.add_argument("-display-name", "--display-name", dest="display_name", default=None,
                   help="Optional display name shown in the UI.")
    p.add_argument("-disable", "--disable", dest="disable", action="store_true",
                   help="Disable the account.  Existing sessions are revoked; "
                        "the user can't sign in until re-enabled.")
    p.add_argument("-enable",  "--enable",  dest="enable",  action="store_true",
                   help="Re-enable a previously disabled account.")
    return p


def _ensure_admin_interactive() -> int:
    """First-run bootstrap: if no admin account exists yet, prompt for a
    username + password (hidden via getpass) and create one.

    No-op (exit 0) when an admin already exists, or when stdin/stdout isn't a
    TTY — so a backgrounded or CI start never hangs waiting on input.  Invoked
    by ``run.sh`` before the server is launched.  Always returns 0: a failed
    bootstrap must never block the server from starting.
    """
    import getpass

    data_dir = get_data_dir()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        store = UserStore(data_dir)
    except (OSError, RuntimeError) as e:
        print(f"setadm: couldn't open user store at {data_dir}: {e}", file=sys.stderr)
        return 0

    if store.has_any_admin():
        return 0   # already bootstrapped — nothing to do

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "SoniqBoom has no admin account yet and this start is "
            "non-interactive.\n"
            "Create one with:  soniqboom-setadm -user <name> -passwd <password>",
            file=sys.stderr,
        )
        return 0

    print("")
    print("  ──────────────────────────────────────────────────────")
    print("  First-run setup — create the SoniqBoom admin account")
    print("  ──────────────────────────────────────────────────────")
    print("  Web-UI registration stays locked until one admin exists,")
    print("  so let's create it now.")
    print("")
    try:
        while True:
            username = input("  Admin username: ").strip()
            try:
                validate_username(username)
                break
            except ValueError as e:
                print(f"    {e}")
        while True:
            pw1 = getpass.getpass("  Admin password (min 8 chars): ")
            try:
                validate_password(pw1)
            except ValueError as e:
                print(f"    {e}")
                continue
            if getpass.getpass("  Confirm password: ") != pw1:
                print("    Passwords don't match — try again.")
                continue
            break
    except (EOFError, KeyboardInterrupt):
        print("\n  Setup cancelled — no admin created.  The server will start,")
        print("  but the web UI stays locked until you run soniqboom-setadm.")
        return 0

    try:
        user = store.create(username=username, password=pw1, role="admin")
    except (ValueError, OSError) as e:
        print(f"  setadm: couldn't create admin: {e}", file=sys.stderr)
        return 0

    print("")
    print(f"  ✓ Admin '{user.username}' created — sign in with it once the")
    print("    server is ready.")
    print("")
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = argv if argv is not None else sys.argv[1:]
    # First-run bootstrap mode (used by run.sh): create an admin interactively
    # when none exists.  Handled before the parser because the normal path
    # requires -user, whereas this mode takes no other flags.
    if "--ensure-admin" in raw or "-ensure-admin" in raw:
        return _ensure_admin_interactive()
    _reject_prefix_matches(raw)
    args = _build_parser().parse_args(raw)

    if args.disable and args.enable:
        print("error: -disable and -enable are mutually exclusive", file=sys.stderr)
        return 2

    try:
        validate_username(args.user)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    data_dir = get_data_dir()
    # Surface a friendly error if the data dir is unwriteable instead of a
    # Python traceback — the most common failure mode here is "you ran the
    # CLI as a different user than the server" or "data dir on a read-only
    # mount".  Probe both readability and writeability up front.
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / f".cli-probe-{os.getpid()}"
        probe.write_text("x")
        probe.unlink()
    except (OSError, PermissionError) as e:
        print(
            f"error: data dir {data_dir} is not writable ({e.__class__.__name__}: {e}).",
            file=sys.stderr,
        )
        print(
            "hint: run the CLI as the same OS user as the server, or set "
            "SONIQBOOM_DATA_DIR to a writable folder.",
            file=sys.stderr,
        )
        return 3

    try:
        store = UserStore(data_dir)
    except (OSError, RuntimeError) as e:
        print(f"error: couldn't open user store at {data_dir}: {e}", file=sys.stderr)
        return 3
    existing = store.get_by_username(args.user)

    # ── Create flow ──────────────────────────────────────────────────────
    if existing is None:
        if not args.passwd:
            print(
                f"error: user '{args.user}' does not exist — supply -passwd "
                "to create it.",
                file=sys.stderr,
            )
            return 2
        try:
            validate_password(args.passwd)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        role = args.role or "admin"
        try:
            user = store.create(
                username=args.user,
                password=args.passwd,
                role=role,
                display_name=args.display_name,
            )
            if args.disable:
                store.update(user.id, enabled=False)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        except OSError as e:
            print(
                f"error: couldn't write {data_dir / 'users.json'} ({e.__class__.__name__}: {e}).",
                file=sys.stderr,
            )
            return 3
        print(
            f"Created user '{user.username}' (role={user.role}, "
            f"enabled={user.enabled}, id={user.id}).",
        )
        return 0

    # ── Update flow ──────────────────────────────────────────────────────
    changes: list[str] = []
    try:
        if args.passwd:
            try:
                validate_password(args.passwd)
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            store.set_password(existing.id, args.passwd)
            changes.append("password")
        update_kwargs: dict = {}
        if args.role:
            update_kwargs["role"] = args.role
            changes.append(f"role={args.role}")
        if args.display_name is not None:
            update_kwargs["display_name"] = args.display_name
            changes.append("display_name")
        if args.disable:
            update_kwargs["enabled"] = False
            changes.append("disabled")
        if args.enable:
            update_kwargs["enabled"] = True
            changes.append("enabled")
        if update_kwargs:
            store.update(existing.id, **update_kwargs)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(
            f"error: couldn't write {data_dir / 'users.json'} ({e.__class__.__name__}: {e}).",
            file=sys.stderr,
        )
        return 3

    if not changes:
        # Hint when nothing happened — common cause: operator forgot
        # -passwd / -role and expected something to change.
        print(
            f"user '{existing.username}' unchanged "
            f"(role={existing.role}, enabled={existing.enabled}). "
            "Use -passwd to reset the password, -role to change the role, "
            "or -disable/-enable to gate sign-in.",
        )
    else:
        print(
            f"Updated user '{existing.username}': {', '.join(changes)}.",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
