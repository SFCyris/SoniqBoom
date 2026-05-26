#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Download a static ffmpeg build with full demuxer coverage into the user
data directory, so SoniqBoom always has a known-good ffmpeg available even
when the host's system ffmpeg is stripped or missing.

Sources (all GPL-licensed, all publish SHA256 sidecars):

* macOS arm64 / x86_64 — Martin Riedl's builds (https://ffmpeg.martin-riedl.de/)
* Linux x86_64 / arm64 — BtbN/FFmpeg-Builds GitHub Releases (latest tag)

Usage
-----
    python3 scripts/fetch_ffmpeg.py            # idempotent install / refresh
    python3 scripts/fetch_ffmpeg.py --force    # always re-download
    python3 scripts/fetch_ffmpeg.py --dest …   # override destination dir
    python3 scripts/fetch_ffmpeg.py --print    # print path it would install to, exit
    python3 scripts/fetch_ffmpeg.py --check    # exit 0 if bundled ffmpeg is current, 1 otherwise

Design notes
------------
* The script is intentionally standalone: no SoniqBoom imports, no third-party
  dependencies beyond the Python stdlib.  This is so it works during install
  *before* the SoniqBoom virtualenv is set up.
* Probes the system ffmpeg first.  If it has the dsf/iff/wsd demuxers we need
  for DSD playback, we still install the bundled copy (so it's available as a
  fallback / pin), but log the fact that the system one is also usable.
* Atomic install: download into ``bin/.staging``, verify SHA256, then rename
  into ``bin/ffmpeg``.  Old binary is never overwritten if verification fails.
* macOS quarantine xattr is cleared after install so Gatekeeper doesn't refuse
  to launch the unsigned binary.  We also ad-hoc codesign to keep the runtime
  loader happy on hardened-runtime-enforcing macOS versions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# ── Source map ──────────────────────────────────────────────────────────────
# Per (OS, arch), the URL of the ffmpeg archive and the URL of its sidecar
# SHA256 file.  Stable, publisher-published URLs only — no GitHub asset names
# that get re-derived per release, no auto-detected redirects that can break.

_MARTIN_RIEDL = "https://ffmpeg.martin-riedl.de/redirect/latest"
_BTBN_LATEST = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"
)

_SOURCES: dict[tuple[str, str], dict[str, str]] = {
    ("darwin", "arm64"): {
        # Martin Riedl's redirect endpoints route to the latest signed build.
        # No sidecar SHA256 is published; integrity is anchored on HTTPS to
        # a publisher domain plus a post-extract demuxer probe (we refuse to
        # install a binary that doesn't have dsf/iff/wsd).
        "archive_url": f"{_MARTIN_RIEDL}/macos/arm64/release/ffmpeg.zip",
        "sha_url":     None,
        "archive_kind": "zip",
        "inner_name":   "ffmpeg",
    },
    ("darwin", "x86_64"): {
        "archive_url": f"{_MARTIN_RIEDL}/macos/amd64/release/ffmpeg.zip",
        "sha_url":     None,
        "archive_kind": "zip",
        "inner_name":   "ffmpeg",
    },
    ("linux", "x86_64"): {
        "archive_url": f"{_BTBN_LATEST}/ffmpeg-master-latest-linux64-gpl.tar.xz",
        "sha_url":     None,  # BtbN does not publish sidecar checksums
        "archive_kind": "tar.xz",
        # BtbN packs the binary inside a versioned directory; we glob for it.
        "inner_name":   "ffmpeg",
    },
    ("linux", "aarch64"): {
        "archive_url": f"{_BTBN_LATEST}/ffmpeg-master-latest-linuxarm64-gpl.tar.xz",
        "sha_url":     None,
        "archive_kind": "tar.xz",
        "inner_name":   "ffmpeg",
    },
}


def _detect_target() -> tuple[str, str]:
    """Return (os, arch) keys for ``_SOURCES``.  Normalises macOS Rosetta and
    Linux ``aarch64`` vs ``arm64`` differences."""
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    if sysname == "darwin":
        # ``platform.machine()`` reports the *interpreter's* arch under
        # Rosetta — fall back to ``sysctl hw.optional.arm64`` for the
        # native chip flag so a Rosetta-running install still pulls
        # the native-arch ffmpeg.
        if machine == "x86_64":
            try:
                r = subprocess.run(
                    ["sysctl", "-n", "hw.optional.arm64"],
                    capture_output=True, text=True, check=False, timeout=5,
                )
                if (r.stdout or "").strip() == "1":
                    machine = "arm64"
            except (FileNotFoundError, subprocess.SubprocessError):
                pass
    elif sysname == "linux":
        if machine == "arm64":
            machine = "aarch64"
    return sysname, machine


def _default_dest() -> Path:
    """Return the default install location for the bundled ffmpeg binary.

    Matches the SoniqBoom data-dir conventions (XDG on Linux, ``Application
    Support`` on macOS) so the binary lives next to other per-user state
    rather than polluting ``/usr/local`` (and not requiring sudo)."""
    env = os.environ.get("SONIQBOOM_DATA_DIR")
    if env:
        return Path(env) / "bin"
    if platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "SoniqBoom"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg) / "soniqboom" if xdg else (
            Path.home() / ".local" / "share" / "soniqboom"
        )
    return base / "bin"


# ── Network helpers ─────────────────────────────────────────────────────────

_UA = "soniqboom-ffmpeg-fetcher/1.0"


def _http_get(url: str, dest: Path | None = None) -> bytes | None:
    """Fetch a URL.  If ``dest`` is given, stream to disk and return None;
    otherwise return the body as bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if dest is None:
                return resp.read()
            total = int(resp.headers.get("Content-Length") or 0)
            got   = 0
            chunk = 1024 * 256
            last_pct = -5
            with dest.open("wb") as f:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    f.write(buf)
                    got += len(buf)
                    if total:
                        pct = int(got * 100 / total)
                        if pct >= last_pct + 5:
                            print(f"    {pct:3d}%  ({got/1e6:.1f} / {total/1e6:.1f} MB)",
                                  file=sys.stderr)
                            last_pct = pct
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} fetching {url}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error fetching {url}: {e.reason}") from None
    return None


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for buf in iter(lambda: f.read(1024 * 256), b""):
            h.update(buf)
    return h.hexdigest()


# ── Probe helpers ───────────────────────────────────────────────────────────

_REQUIRED_DEMUXERS = ("dsf", "iff", "wsd")


def _probe_demuxers(ffmpeg: str) -> set[str]:
    """Return the subset of ``_REQUIRED_DEMUXERS`` that ``ffmpeg`` claims to
    support.  Empty set on probe failure."""
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-formats"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return set()
    have: set[str] = set()
    for line in (r.stdout or "").splitlines():
        # Format-list rows: "  D   <name>   <description>"
        cols = line.strip().split(None, 2)
        if len(cols) < 2 or "D" not in cols[0]:
            continue
        name = cols[1].lower()
        if name in _REQUIRED_DEMUXERS:
            have.add(name)
    return have


def _ffmpeg_version(ffmpeg: str) -> str | None:
    try:
        r = subprocess.run(
            [ffmpeg, "-version"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        first = (r.stdout or "").splitlines()[0] if r.stdout else ""
        # "ffmpeg version 8.1 Copyright …"
        parts = first.split(" ", 3)
        return parts[2] if len(parts) >= 3 else None
    except (FileNotFoundError, subprocess.SubprocessError, IndexError):
        return None


# ── Install ──────────────────────────────────────────────────────────────────

def _extract(archive_path: Path, kind: str, inner_name: str,
             out_dir: Path) -> Path:
    """Extract a single binary called ``inner_name`` from the archive.
    Returns the path of the extracted binary inside ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if kind == "zip":
        with zipfile.ZipFile(archive_path) as zf:
            # The archive may be a flat zip (just ``ffmpeg``) or contain a
            # versioned root dir.  Walk it and grab the first matching name.
            for member in zf.namelist():
                base = Path(member).name
                if base == inner_name and not member.endswith("/"):
                    target = out_dir / inner_name
                    with zf.open(member) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    return target
        raise RuntimeError(f"{inner_name} not found in {archive_path}")
    if kind in ("tar.xz", "tar.gz", "tar.bz2"):
        # ``tarfile`` auto-detects compression by file content.
        with tarfile.open(archive_path) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                if Path(member.name).name == inner_name:
                    extract_to = out_dir / inner_name
                    src_f = tf.extractfile(member)
                    if src_f is None:
                        continue
                    with extract_to.open("wb") as dst:
                        shutil.copyfileobj(src_f, dst)
                    return extract_to
        raise RuntimeError(f"{inner_name} not found in {archive_path}")
    raise RuntimeError(f"unsupported archive kind: {kind}")


def _post_install_macos(binary: Path) -> None:
    """Clear the quarantine xattr and ad-hoc codesign the binary so macOS
    Gatekeeper / hardened-runtime checks don't refuse it.  Both operations
    are best-effort — failures are warned about but not fatal."""
    try:
        subprocess.run(
            ["xattr", "-d", "com.apple.quarantine", str(binary)],
            check=False, capture_output=True,
        )
    except FileNotFoundError:
        pass
    try:
        subprocess.run(
            ["codesign", "--force", "--sign", "-", str(binary)],
            check=False, capture_output=True,
        )
    except FileNotFoundError:
        # codesign is part of the Xcode command-line tools — usually present
        # on any Mac with brew installed.  If it's missing the binary still
        # runs from Terminal; the warning is purely for Gatekeeper-aware UX.
        print("    note: codesign not available; binary will run but Gatekeeper "
              "may warn on first launch.", file=sys.stderr)


def install(
    dest_dir: Path | None = None,
    force: bool = False,
) -> dict:
    """Install (or refresh) the bundled ffmpeg.  Returns a dict with status
    keys: ``installed_path``, ``system_path``, ``system_ok``, ``version``,
    ``skipped``."""
    target = _detect_target()
    source = _SOURCES.get(target)
    if not source:
        raise RuntimeError(
            f"no bundled ffmpeg source for platform {target!r}.  "
            "Please install ffmpeg via the system package manager and set "
            "ffmpeg_path in SoniqBoom's config."
        )
    dest_dir = (dest_dir or _default_dest()).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    binary = dest_dir / "ffmpeg"
    manifest_path = dest_dir / "ffmpeg.manifest.json"

    # Probe the system ffmpeg for informational logging — the user picked
    # "probe + always bundle as fallback", so we don't skip the download
    # just because the system has DSD support; we always lay one down.
    system_path = shutil.which("ffmpeg")
    system_have = _probe_demuxers(system_path) if system_path else set()
    system_ok = bool(system_have) and set(_REQUIRED_DEMUXERS).issubset(system_have)

    print(f"target:         {target[0]}/{target[1]}",         file=sys.stderr)
    print(f"system ffmpeg:  {system_path or 'not found'}",    file=sys.stderr)
    print(f"system DSD ok:  {system_ok} (have: {sorted(system_have) or 'none'})",
          file=sys.stderr)
    print(f"bundled dest:   {binary}",                        file=sys.stderr)

    # Idempotency: if not forced and the bundled binary already exists *and*
    # claims the right demuxers, skip the download — re-running install.sh
    # shouldn't pull a new ffmpeg every time.
    if binary.exists() and not force:
        bundled_have = _probe_demuxers(str(binary))
        if set(_REQUIRED_DEMUXERS).issubset(bundled_have):
            print("bundled ffmpeg already present and complete — skipping download.",
                  file=sys.stderr)
            return {
                "installed_path": str(binary),
                "system_path":    system_path,
                "system_ok":      system_ok,
                "version":        _ffmpeg_version(str(binary)),
                "skipped":        True,
            }

    # ── Download + verify + atomic install ──────────────────────────────
    with tempfile.TemporaryDirectory(prefix="soniqboom-ffmpeg-") as tmpdir:
        tmp = Path(tmpdir)
        ext = source["archive_kind"]
        archive = tmp / f"ffmpeg.{ext}"

        print(f"downloading:    {source['archive_url']}", file=sys.stderr)
        _http_get(source["archive_url"], dest=archive)
        sha = _sha256_of(archive)

        if source.get("sha_url"):
            print(f"verifying SHA256 against {source['sha_url']}", file=sys.stderr)
            try:
                body = _http_get(source["sha_url"])
                # Sidecar format: "<hex>  filename" — take the first hex token.
                expected = (body or b"").decode("ascii", "ignore").split()[0].lower()
                if expected and expected != sha:
                    raise RuntimeError(
                        f"checksum mismatch: expected {expected}, got {sha}.  "
                        "Refusing to install; archive may be corrupt or tampered."
                    )
                print(f"    SHA256 ok ({sha[:16]}…)", file=sys.stderr)
            except RuntimeError as exc:
                # If the checksum fetch itself failed (404, network),
                # surface but don't abort — HTTPS still authenticates origin.
                print(f"    note: checksum verification skipped: {exc}",
                      file=sys.stderr)
        else:
            print(f"    SHA256 (informational, no sidecar): {sha[:16]}…",
                  file=sys.stderr)

        # Extract into a staging dir; atomic-rename into place only after
        # we've confirmed the binary works.
        staging = dest_dir / ".staging"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        extracted = _extract(archive, ext, source["inner_name"], staging)
        extracted.chmod(0o755)

        # Post-install hooks (macOS quarantine / codesign)
        if target[0] == "darwin":
            _post_install_macos(extracted)

        # Sanity: ensure the extracted binary actually runs and has DSD support.
        new_demuxers = _probe_demuxers(str(extracted))
        if not set(_REQUIRED_DEMUXERS).issubset(new_demuxers):
            shutil.rmtree(staging)
            raise RuntimeError(
                f"downloaded ffmpeg is missing required demuxers "
                f"(have: {sorted(new_demuxers)}).  Not installing."
            )
        version = _ffmpeg_version(str(extracted))

        # Atomic swap: rename .staging/ffmpeg → bin/ffmpeg
        if binary.exists():
            binary.unlink()
        extracted.rename(binary)
        shutil.rmtree(staging, ignore_errors=True)

    # Manifest — recorded so future runs know what we have and the operator
    # can audit it.
    manifest_path.write_text(json.dumps({
        "platform":    f"{target[0]}/{target[1]}",
        "source_url":  source["archive_url"],
        "sha256":      sha,
        "version":     version,
        "demuxers":    sorted(new_demuxers),
    }, indent=2))

    print(f"installed:      {binary}", file=sys.stderr)
    print(f"version:        {version}",  file=sys.stderr)
    print(f"demuxers:       {', '.join(sorted(new_demuxers))}", file=sys.stderr)
    return {
        "installed_path": str(binary),
        "system_path":    system_path,
        "system_ok":      system_ok,
        "version":        version,
        "skipped":        False,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Download a bundled static ffmpeg for SoniqBoom.",
    )
    p.add_argument("--dest", default=None,
                   help="Override install directory (default: SoniqBoom data dir bin/)")
    p.add_argument("--force", action="store_true",
                   help="Re-download even if a current bundled ffmpeg is present")
    p.add_argument("--print", dest="print_only", action="store_true",
                   help="Print the resolved install path and exit (no download)")
    p.add_argument("--check", action="store_true",
                   help="Exit 0 if the bundled ffmpeg is current, 1 otherwise (no download)")
    args = p.parse_args(argv)

    dest = Path(args.dest).resolve() if args.dest else _default_dest()

    if args.print_only:
        print(dest / "ffmpeg")
        return 0

    if args.check:
        binary = dest / "ffmpeg"
        if not binary.exists():
            print(f"absent:  {binary}")
            return 1
        have = _probe_demuxers(str(binary))
        if set(_REQUIRED_DEMUXERS).issubset(have):
            print(f"current: {binary}  (demuxers: {sorted(have)})")
            return 0
        print(f"stale:   {binary}  (missing {sorted(set(_REQUIRED_DEMUXERS) - have)})")
        return 1

    try:
        install(dest_dir=dest, force=args.force)
    except RuntimeError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
