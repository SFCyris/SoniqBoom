# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web folder shares (``httpindex``, ``core/filesource_http.py``): a web
server's folder listing read as a read-only share."""
from __future__ import annotations

import httpx
import pytest

from soniqboom.core.filesource import create_source, scan_root_for_share
from soniqboom.core.filesource_http import HTTPIndexFileSource

BASE = "https://music.example.org/mods"

APACHE = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN"><html><body>
<h1>Index of /mods</h1><table>
<tr><th><a href="?C=N;O=D">Name</a></th><th><a href="?C=M;O=A">Last modified</a></th>
<th><a href="?C=S;O=A">Size</a></th></tr>
<tr><td><a href="/">Parent Directory</a></td><td>&nbsp;</td><td align="right">  - </td></tr>
<tr><td><a href="Amiga/"><img src="/icons/folder.gif"></a></td><td><a href="Amiga/">Amiga/</a></td>
<td align="right">2026-10-01 12:30  </td><td align="right">  - </td></tr>
<tr><td><a href="space%20cowboy.mod">space cowboy.mod</a></td>
<td align="right">2026-09-30 08:15  </td><td align="right">1.2K</td></tr>
<tr><td><a href="https://elsewhere.example.com/x.mod">mirror</a></td></tr>
<tr><td><a href="Amiga/deep.mod">deep</a></td></tr>
</table></body></html>"""

NGINX = """<html><head><title>Index of /mods/</title></head><body><h1>Index of /mods/</h1><hr><pre>
<a href="../">../</a>
<a href="Amiga/">Amiga/</a>                                             01-Oct-2026 12:30                   -
<a href="toskaa.mod">toskaa.mod</a>                                         30-Sep-2026 08:15               89986
</pre><hr></body></html>"""

PYSERVER = """<!DOCTYPE HTML><html><body><h1>Directory listing for /mods/</h1><hr><ul>
<li><a href="Amiga/">Amiga/</a></li><li><a href="a%26b.xm">a&amp;b.xm</a></li></ul></body></html>"""


def _source(handler) -> HTTPIndexFileSource:
    src = HTTPIndexFileSource(BASE)
    src._client.close()
    src._client = httpx.Client(base_url=src._base, transport=httpx.MockTransport(handler),
                               follow_redirects=True)
    return src


def _html(body):
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


def test_an_apache_listing_gives_its_direct_children_only():
    src = _source(lambda req: _html(APACHE))
    entries = {e.name: e for e in src.list_dir("/")}
    assert set(entries) == {"Amiga", "space cowboy.mod"}
    assert entries["Amiga"].is_dir and entries["Amiga"].path == "/Amiga"
    f = entries["space cowboy.mod"]
    assert not f.is_dir and f.path == "/space cowboy.mod"
    assert f.size == 0                       # "1.2K" is rounded: unknown
    assert f.mtime > 0                       # 2026-09-30 08:15


def test_an_nginx_listing_states_exact_sizes():
    src = _source(lambda req: _html(NGINX))
    entries = {e.name: e for e in src.list_dir("/")}
    assert set(entries) == {"Amiga", "toskaa.mod"}
    assert entries["toskaa.mod"].size == 89986 and entries["toskaa.mod"].mtime > 0
    assert entries["Amiga"].is_dir


def test_a_plain_listing_and_a_subfolder_path():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return _html(PYSERVER.replace("/mods/", "/mods/Amiga/"))
    src = _source(handler)
    out = src.list_dir("/Amiga")
    assert seen == ["/mods/Amiga/"]
    # the page's links are relative to /mods/Amiga/: its children
    assert sorted((e.name, e.path) for e in out) == [("Amiga", "/Amiga/Amiga"),
                                                      ("a&b.xm", "/Amiga/a&b.xm")]


def test_is_dir_wants_a_listing():
    assert _source(lambda req: _html(PYSERVER)).is_dir("/")
    src = _source(lambda req: httpx.Response(404))
    assert not src.is_dir("/") and "not found" in src.last_error
    src = _source(lambda req: httpx.Response(200, content=b"x",
                                             headers={"content-type": "audio/x-mod"}))
    assert not src.is_dir("/") and "not a folder listing" in src.last_error


def test_stat_asks_the_server_and_falls_back_to_one_byte():
    def head_ok(req):
        assert req.method == "HEAD"
        return httpx.Response(200, headers={"content-length": "4096",
                                            "last-modified": "Wed, 30 Sep 2026 08:15:00 GMT"})
    st = _source(head_ok).stat("/a.mod")
    assert (st.size, st.is_dir) == (4096, False) and st.mtime > 0

    def no_head(req):
        if req.method == "HEAD":
            return httpx.Response(405)
        assert req.headers.get("range") == "bytes=0-0"
        return httpx.Response(206, content=b"M", headers={"content-range": "bytes 0-0/77777"})
    assert _source(no_head).stat("/a.mod").size == 77777
    with pytest.raises(FileNotFoundError):
        _source(lambda req: httpx.Response(404)).stat("/gone.mod")


def test_reads_are_plain_ranged_gets():
    data = bytes(range(256)) * 4

    def handler(req):
        rng = req.headers.get("range")
        if rng:
            a, b = rng.split("=")[1].split("-")
            part = data[int(a):int(b) + 1] if b else data[int(a):]
            return httpx.Response(206, stream=httpx.ByteStream(part),
                                  headers={"content-length": str(len(part))})
        return httpx.Response(200, stream=httpx.ByteStream(data),
                              headers={"content-length": str(len(data))})
    src = _source(handler)
    assert src.read_file("/x.mod") == data
    assert src.read_at("/x.mod", 10, 5) == data[10:15]


def test_a_scan_walk_asks_for_the_facts_a_listing_lacks():
    heads = []

    def handler(req):
        if req.method == "HEAD":
            heads.append(req.url.path)
            if req.url.path.endswith("gone.mod"):
                return httpx.Response(404)
            return httpx.Response(200, headers={
                "content-length": "4321", "last-modified": "Wed, 30 Sep 2026 08:15:00 GMT"})
        if req.url.path == "/mods/":
            return _html(NGINX.replace(
                '<a href="Amiga/">Amiga/</a>',
                '<a href="Amiga/">Amiga/</a>\n<a href="gone.mod">gone.mod</a>  '
                '                                     -                       -'))
        return _html(PYSERVER.replace("/mods/", "/mods/Amiga/").replace(
            '<li><a href="Amiga/">Amiga/</a></li>', ""))
    src = _source(handler)
    files = {e.path: e for _d, _dirs, fs in src.walk_with_stat("/") for e in fs}
    # nginx states toskaa.mod exactly: no request; the plain listing's file
    # and the one without facts are asked for
    assert sorted(heads) == ["/mods/Amiga/a&b.xm", "/mods/gone.mod"]
    assert (files["/toskaa.mod"].size, files["/Amiga/a&b.xm"].size) == (89986, 4321)
    assert files["/Amiga/a&b.xm"].mtime > 0
    assert (files["/gone.mod"].size, files["/gone.mod"].mtime) == (0, 0.0)   # stat failed
    # browsing a folder (list_dir) stays one request
    heads.clear()
    src.list_dir("/Amiga")
    assert heads == []


def test_reconnect_reads_the_root_listing():
    methods = []

    def handler(req):
        methods.append(req.method)
        return _html(PYSERVER)
    src = _source(handler)
    src._new_client = lambda: httpx.Client(base_url=src._base,
                                           transport=httpx.MockTransport(handler))
    assert src.reconnect() and methods == ["GET"]


def test_a_web_folder_share_is_its_own_protocol():
    share = {"protocol": "httpindex", "base_url": BASE, "host": "music.example.org"}
    assert scan_root_for_share(share) == BASE
    src = create_source(share)
    try:
        assert isinstance(src, HTTPIndexFileSource)
    finally:
        src.close()


def test_nginx_rounded_sizes_are_not_taken_as_bytes():
    """``autoindex_exact_size off`` lists "12K" / "1.5M": unknown, so the scan
    asks the server — "12K" read as 12 bytes made every scan drop the cached
    copy of an archive and fetch it again (QA 2026-10-28)."""
    from soniqboom.core.filesource_http import _tail_facts
    assert _tail_facts("   30-Sep-2026 08:15   89986")[0] == 89986
    for rounded in ("12K", "1.5M", "3G", "-"):
        size, mtime = _tail_facts(f"   30-Sep-2026 08:15   {rounded}")
        assert size == 0 and mtime > 0, rounded


def test_a_link_naming_the_default_port_is_the_same_server_and_nul_names_are_skipped():
    page = APACHE.replace('<a href="Amiga/deep.mod">deep</a>',
                          '<a href="https://music.example.org:443/mods/port.mod">port</a>'
                          '<a href="bad%00name.mod">bad</a>')
    names = {e.name for e in _source(lambda req: _html(page)).list_dir("/")}
    assert names == {"Amiga", "space cowboy.mod", "port.mod"}


def test_a_share_redirected_from_http_to_https_lists_its_files():
    """A web folder entered as http:// that the server answers on https://:
    the listing's https:// links are the share's (QA 2026-10-28)."""
    def handler(req):
        if req.url.scheme == "http":
            return httpx.Response(301, headers={"Location": str(req.url.copy_with(scheme="https"))})
        return _html(NGINX)
    src = HTTPIndexFileSource("http://music.example.org/mods")
    src._client.close()
    src._client = httpx.Client(base_url=src._base, transport=httpx.MockTransport(handler),
                               follow_redirects=True)
    assert {e.name for e in src.list_dir("/")} == {"Amiga", "toskaa.mod"}
