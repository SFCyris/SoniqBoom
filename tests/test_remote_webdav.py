# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""WebDAV shares work wherever SMB / FTP shares do (GitHub #18).

A WebDAV scan root is the share's http(s):// base URL.  Every place that
special-cased smb:// + ftp:// used to send it down the local-filesystem
branch (folder browsing answered 404), ``parse_remote_path`` refused it, the
startup connect skipped it, and the source itself returned absolute URLs where
the scanner expects root-relative paths.  These tests run the real
``WebDAVFileSource`` against a small threaded WebDAV server.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _webdav_server import DavServer  # noqa: E402

from soniqboom.core import filesource  # noqa: E402
from soniqboom.core.filesource import (  # noqa: E402
    REMOTE_SCHEMES, find_source_for_path, is_remote_path, parse_remote_path,
    register_source, remove_source, scan_root_for_share, share_endpoint,
)
from soniqboom.core.filesource_webdav import WebDAVFileSource  # noqa: E402

USER, PW = "jatsek", "s3cret pass"
ODD = "01 what's up? #2 (Ä).mp3"


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "share"
    (root / "Artist A" / "Album #1").mkdir(parents=True)
    (root / "Artist A" / "Album #1" / ODD).write_bytes(b"ID3" + bytes(range(256)) * 40)
    (root / "top.flac").write_bytes(bytes(range(256)) * 100)
    (root / "Archives").mkdir()
    with zipfile.ZipFile(root / "Archives" / "mods.zip", "w") as z:
        z.writestr("tune.mod", b"M.K." * 500)
    return root


@pytest.fixture(params=["/", "/remote.php/dav/files/jatsek/Music"],
                ids=["server-root", "nextcloud-path"])
def dav(tree, request):
    srv = DavServer(tree, prefix=request.param, user=USER, password=PW).start()
    yield srv
    srv.stop()


class _Stub:
    def close(self):
        pass


@pytest.fixture
def registered():
    roots: list[str] = []

    def _reg(root, src):
        register_source(root, src)
        roots.append(root)
        return src
    yield _reg
    for r in roots:
        remove_source(r)


# ── one scheme list ─────────────────────────────────────────────────────────

def test_one_scheme_list_covers_every_share_kind():
    for p in ("smb://nas/Music", "ftp://nas/", "http://dav.local:8080",
              "https://dav.example.com", "https://h/x:/a.zip::b.mod",
              "webdav://h/x", "webdavs://h/x"):
        assert is_remote_path(p), p
    for p in ("/Volumes/Music", "C:/Music", "", None, "file:///x"):
        assert not is_remote_path(p), p
    assert set(REMOTE_SCHEMES) >= {"smb://", "ftp://", "http://", "https://"}


def test_every_copy_of_the_scheme_list_is_gone():
    # The bug class: a module carrying its own smb/ftp-only tuple.  Every
    # remote test goes through ``is_remote_path`` / ``REMOTE_SCHEMES`` now.
    import re
    pkg = Path(filesource.__file__).resolve().parents[1]
    pat = re.compile(r"""(?:startswith\(\s*\([^)]*|=\s*\()["'](?:smb|ftp)://["']""")
    hits = [f"{p.relative_to(pkg)}:{n}" for p in pkg.rglob("*.py")
            for n, line in enumerate(p.read_text().splitlines(), 1) if pat.search(line)]
    assert hits == []


def test_scan_root_and_endpoint_for_each_protocol():
    smb = {"protocol": "smb", "host": "NAS", "share": "Music"}
    ftp = {"protocol": "ftp", "host": "10.0.0.88", "remote_path": "/mods", "port": 2121}
    dav = {"protocol": "webdavs", "host": "dav.example.com", "remote_path": "/",
           "base_url": "https://dav.example.com"}
    nc = {"protocol": "webdav", "host": "cloud:8080",
          "base_url": "http://cloud:8080/remote.php/dav/files/u/Music"}
    assert scan_root_for_share(smb) == "smb://NAS/Music"
    assert scan_root_for_share(ftp) == "ftp://10.0.0.88/mods"
    assert scan_root_for_share(dav) == "https://dav.example.com"
    assert scan_root_for_share(nc) == "http://cloud:8080/remote.php/dav/files/u/Music"
    assert share_endpoint(smb) == ("smb", "nas", 445)
    assert share_endpoint(ftp) == ("ftp", "10.0.0.88", 2121)
    assert share_endpoint(dav) == ("https", "dav.example.com", 443)
    assert share_endpoint(nc) == ("http", "cloud", 8080)
    # The admin API and the startup connect use the SAME definition (the
    # startup copy without WebDAV left WebDAV shares unconnected after every
    # restart — the reason #18's 404 lines "stopped" in 1.10.0).
    from soniqboom.api.admin import _scan_root_for_share
    assert _scan_root_for_share(dav) == "https://dav.example.com"


# ── track-path parsing ──────────────────────────────────────────────────────

def test_parse_webdav_track_paths(registered):
    # A root without a path: its ':' separator sits right after the host.
    assert parse_remote_path("https://dav.example.com:/Artist/x #1.flac") == \
        ("https://dav.example.com", "/Artist/x #1.flac")
    assert parse_remote_path("https://dav.example.com:8443:/a?b.flac") == \
        ("https://dav.example.com:8443", "/a?b.flac")
    assert parse_remote_path("https://h/remote.php/dav/files/u/Music:/A/x.flac") == \
        ("https://h/remote.php/dav/files/u/Music", "/A/x.flac")
    assert parse_remote_path("https://dav.example.com/:/x.mp3") == \
        ("https://dav.example.com/", "/x.mp3")
    assert parse_remote_path("https://dav.example.com") == ("https://dav.example.com", "")
    # A registered root is the boundary even when its own path holds a ':'.
    registered("https://h/dav/Music: Live", _Stub())
    assert parse_remote_path("https://h/dav/Music: Live:/x.flac") == \
        ("https://h/dav/Music: Live", "/x.flac")
    # SMB / FTP unchanged (including the '#' / '?' file-name round trip).
    assert parse_remote_path("ftp://nas/share:/a/sm#2 (b?).flac") == \
        ("ftp://nas/share", "/a/sm#2 (b?).flac")
    assert parse_remote_path("smb://nas/Music:/x.mp3") == ("smb://nas/Music", "/x.mp3")
    with pytest.raises(ValueError):
        parse_remote_path("/local/file.mp3")


def test_find_source_for_path_prefers_the_longest_root(registered):
    outer, inner = _Stub(), _Stub()
    registered("https://h/dav", outer)
    registered("https://h/dav/sub", inner)
    assert find_source_for_path("https://h/dav/sub/Album")[2] is inner
    assert find_source_for_path("https://h/dav/other")[1:] == ("/other", outer)
    assert find_source_for_path("https://h/davx/y") is None


# ── the source against a WebDAV server ──────────────────────────────────────

def test_listing_is_root_relative(dav):
    for base in (dav.url, dav.url + "/"):
        src = WebDAVFileSource(base, USER, PW)
        assert src.is_dir("/") and src.last_error is None
        top = {e.name: (e.path, e.is_dir) for e in src.list_dir("/")}
        assert top == {"Archives": ("/Archives", True), "Artist A": ("/Artist A", True),
                       "top.flac": ("/top.flac", False)}
        sub = src.list_dir("/Artist A/Album #1")
        assert [(e.name, e.path, e.size) for e in sub] == \
            [(ODD, f"/Artist A/Album #1/{ODD}", 10243)]
        walked = list(src.walk("/"))
        assert walked[0] == ("/", ["Archives", "Artist A"], ["top.flac"])
        assert ("/Artist A/Album #1", [], [ODD]) in walked
        st = src.stat(f"/Artist A/Album #1/{ODD}")
        assert (st.size, st.is_dir) == (10243, False) and st.mtime > 0


def test_reads_and_ranges(dav, tree):
    src = WebDAVFileSource(dav.url, USER, PW)
    data = (tree / "top.flac").read_bytes()
    assert src.read_file("/top.flac") == data
    assert src.read_at("/top.flac", 300, 40) == data[300:340]
    assert src.read_at("/top.flac", len(data) - 5, 100) == data[-5:]
    assert src.read_at("/top.flac", len(data) + 10, 5) == b""
    assert src.read_partial("/top.flac", 64) == data[:64]
    with src.open_stream("/top.flac", offset=1000) as st:
        assert st.size == len(data)
        assert st.read(1 << 20) + st.read(1 << 20) == data[1000:]
    ranges = [r[2] for r in dav.gets()]
    assert "bytes=300-339" in ranges and "bytes=1000-" in ranges
    with pytest.raises(FileNotFoundError):
        src.read_file("/missing.mp3")
    with pytest.raises(FileNotFoundError):
        src.open_stream("/missing.mp3")


def test_bad_credentials_say_why(dav):
    src = WebDAVFileSource(dav.url, USER, "wrong")
    assert not src.is_dir("/")
    assert "401" in src.last_error and "password" in src.last_error
    nope = WebDAVFileSource(dav.url + "/nope", USER, PW)
    assert not nope.is_dir("/") and "path" in nope.last_error


def test_cross_host_hrefs_are_skipped(monkeypatch):
    src = WebDAVFileSource("https://dav.example.com/Music")
    monkeypatch.setattr(src, "_propfind", lambda url, depth: [
        {"href": "/Music/", "is_dir": True, "size": 0, "mtime": 0},
        {"href": "https://evil.example/steal.mp3", "is_dir": False, "size": 1, "mtime": 0},
        {"href": "https://dav.example.com/Music/ok.mp3", "is_dir": False, "size": 2, "mtime": 0},
        {"href": "rel%20name.mp3", "is_dir": False, "size": 3, "mtime": 0},
    ])
    assert [(e.name, e.path) for e in src.list_dir("/")] == \
        [("ok.mp3", "/ok.mp3"), ("rel name.mp3", "/rel name.mp3")]


def test_listing_behind_a_proxy_that_moves_the_share(monkeypatch):
    # https://dav.example.com is proxied to a server that thinks its share
    # lives at /webdav/ — hrefs come back under that path.
    src = WebDAVFileSource("https://dav.example.com")
    pages = {
        "": ["/webdav/", "/webdav/Artist/", "/webdav/top.mp3"],
        "Artist/": ["/webdav/Artist/", "/webdav/Artist/x.mp3"],
        "Empty/": ["/webdav/Empty/"],
    }
    monkeypatch.setattr(src, "_propfind", lambda url, depth: [
        {"href": h, "is_dir": h.endswith("/"), "size": 1, "mtime": 0} for h in pages[url]])
    assert [e.path for e in src.list_dir("/")] == ["/Artist", "/top.mp3"]
    assert [e.path for e in src.list_dir("/Artist")] == ["/Artist/x.mp3"]
    assert src.list_dir("/Empty") == []


# ── the API layer routes WebDAV roots to the share ──────────────────────────

async def test_folder_browse_of_a_webdav_root(dav, registered, monkeypatch):
    from soniqboom.api import fstree
    root = dav.url
    registered(root, WebDAVFileSource(root, USER, PW))
    monkeypatch.setattr(fstree, "_get_or_build_scan_root_sorted", lambda s, h: ([], {}))
    res = await fstree.get_children(path=root, root=root)
    assert [c["name"] for c in res["children"]] == ["Archives", "Artist A"]
    assert res["children"][1]["path"] == f"{root}/Artist A"
    res = await fstree.get_children(path=f"{root}/Artist A", root=root)
    assert [c["path"] for c in res["children"]] == [f"{root}/Artist A/Album #1"]


async def test_source_bytes_and_cast_materialize_read_webdav_tracks(
        dav, tree, registered, tmp_data_dir, monkeypatch):
    from soniqboom.core import remote_cache
    from soniqboom.core.cast_render import materialize_source
    from soniqboom.core.source_bytes import read_source_bytes
    monkeypatch.setattr(remote_cache, "_cache",
                        remote_cache.RemoteCache(tmp_data_dir / "rc", 64))
    root = dav.url
    registered(root, WebDAVFileSource(root, USER, PW))
    track = f"{root}:/Artist A/Album #1/{ODD}"
    want = (tree / "Artist A" / "Album #1" / ODD).read_bytes()
    assert read_source_bytes(track) == want
    assert read_source_bytes(f"{root}:/Archives/mods.zip::tune.mod") == b"M.K." * 500
    p = await materialize_source(track, "t-webdav-1")
    assert p is not None and Path(p).read_bytes() == want


def test_webdavs_over_tls(tree, tmp_path):
    """HTTPS: a certificate the system doesn't trust fails with a clear reason;
    a share configured with ``verify_ssl: false`` reads normally."""
    crypto = pytest.importorskip("cryptography")  # noqa: F841
    import datetime
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    srv = DavServer(tree, user=USER, password=PW,
                    tls=(str(tmp_path / "c.pem"), str(tmp_path / "k.pem"))).start()
    try:
        assert srv.url.startswith("https://")
        share = {"protocol": "webdavs", "host": f"127.0.0.1:{srv.port}",
                 "base_url": srv.url, "username": USER}
        strict = filesource.create_source(share, password=PW)
        assert not strict.is_dir("/") and "certificate" in strict.last_error.lower()
        lax = filesource.create_source({**share, "verify_ssl": False}, password=PW)
        assert lax.is_dir("/")
        assert lax.read_at("/top.flac", 10, 5) == (tree / "top.flac").read_bytes()[10:15]
    finally:
        srv.stop()


# ── a path-less root is not a prefix of another share's tracks ──────────────

def test_a_pathless_root_never_claims_another_shares_tracks(registered):
    registered("https://dav.example.com", _Stub())
    # A second share on the same host, another port: its tracks start with
    # the first root + ":" too.  Before: ("https://dav.example.com",
    # "8443/dav:/x.flac") — played from the wrong share.
    assert parse_remote_path("https://dav.example.com:8443/dav:/x.flac") == \
        ("https://dav.example.com:8443/dav", "/x.flac")
    registered("https://dav.example.com:8443/dav", _Stub())
    assert parse_remote_path("https://dav.example.com:8443/dav:/x.flac") == \
        ("https://dav.example.com:8443/dav", "/x.flac")
    assert parse_remote_path("https://dav.example.com:/a/x.flac") == \
        ("https://dav.example.com", "/a/x.flac")
    assert parse_remote_path("https://dav.example.com:") == ("https://dav.example.com", "")


# ── the scheme is case-insensitive ──────────────────────────────────────────

def test_a_mixed_case_scheme_is_still_a_remote_path(registered):
    assert is_remote_path("Https://Cloud.example.com/dav:/a.flac")
    assert is_remote_path("SMB://nas/Music:/x.mp3") and is_remote_path("WebDAVs://h/x")
    assert not is_remote_path("/Volumes/Https/x.flac")
    # A share stored as typed before this fix keeps its key (its tracks were
    # indexed under it) and now resolves instead of going to the local disk.
    registered("Https://h/dav", _Stub())
    assert parse_remote_path("Https://h/dav:/a.flac") == ("Https://h/dav", "/a.flac")
    assert parse_remote_path("Ftp://nas/m:/a.mod") == ("Ftp://nas/m", "/a.mod")


def test_new_webdav_shares_are_stored_with_a_lowercase_scheme_and_host():
    from fastapi import HTTPException

    from soniqboom.api.admin import _normalize_webdav_url
    url, parts, user, pw = _normalize_webdav_url(
        "Https://Cloud.Example.COM:8443/Remote.php/dav/U/")
    assert url == "https://cloud.example.com:8443/Remote.php/dav/U"
    assert (parts.netloc, parts.path, user, pw) == ("cloud.example.com:8443",
                                                    "/Remote.php/dav/U", "", "")
    assert is_remote_path(url)
    assert _normalize_webdav_url("HTTP://[::1]:8080/x")[0] == "http://[::1]:8080/x"
    # One spelling per share: the scheme's default port goes, and credentials
    # typed into the URL leave it (track paths and logs) for the share's fields.
    assert _normalize_webdav_url("https://h:443/dav")[0] == "https://h/dav"
    assert _normalize_webdav_url("http://h:80")[0] == "http://h"
    assert _normalize_webdav_url("https://Al%40ice:p%3Ass@H/dav")[1:][1:] == ("Al@ice", "p:ss")
    assert _normalize_webdav_url("https://Al%40ice:p%3Ass@H/dav")[0] == "https://h/dav"
    for bad in ("ftp://h/x", "https://", "https://h:99999/x", "/local"):
        with pytest.raises(HTTPException):
            _normalize_webdav_url(bad)


async def test_add_share_stores_the_normalized_url(tree, monkeypatch, tmp_data_dir):
    from soniqboom.api import admin
    srv = DavServer(tree).start()
    saved = {}
    conf: dict = {"network_shares": {}}
    monkeypatch.setattr("soniqboom.config.load_local_conf", lambda *a, **k: conf)
    monkeypatch.setattr("soniqboom.config.save_local_conf", lambda c: saved.update(c))
    monkeypatch.setattr(admin, "_spawn_scan_task", lambda coro, label=None: coro.close())
    from soniqboom.core.store import TrackStore
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: store)
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    typed = srv.url.replace("http://", "HTTP://")          # as typed
    try:
        out = await admin.add_share({"protocol": "webdav", "base_url": typed + "/"}, _tok="t")
        root = out["scan_root"]
        assert root == srv.url.rstrip("/") and root.startswith("http://")
        assert saved["network_shares"][out["id"]]["base_url"] == root
        assert filesource.get_source(root) is not None
    finally:
        for r in [r for r in filesource.all_sources() if r.startswith(srv.url.rstrip("/"))]:
            remove_source(r)
        try:
            from soniqboom.core import remote_freshness
            await remote_freshness.remove_share(srv.url.rstrip("/"))
        except Exception:
            pass
        srv.stop()
