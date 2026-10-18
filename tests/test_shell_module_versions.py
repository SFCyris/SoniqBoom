# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The HTML shells name every frontend JS file by a content-hashed URL
(``core.shell_html``): an import map covers the modules' own imports, and the
shell's own ``/assets/js/…`` URLs are rewritten — so an updated module has a new
URL and a browser that keeps modules by URL (WebKit) loads the new one.
"""
from __future__ import annotations

import json
import os
import re
import time

import pytest
from fastapi.testclient import TestClient

from soniqboom.core import shell_html

_MAP_RE = re.compile(r'<script type="importmap">(.*?)</script>', re.S)


def _map(html: str) -> dict:
    m = _MAP_RE.search(html)
    assert m, "no import map"
    return json.loads(m.group(1))["imports"]


@pytest.fixture
def tree(tmp_path):
    js = tmp_path / "js"
    (js / "mobile").mkdir(parents=True)
    (js / "app.js").write_text("import { P } from './player.js';\n")
    (js / "player.js").write_text("export const P = 1;\n")
    (js / "mobile" / "app.js").write_text("import { P } from '../player.js';\n")
    html = tmp_path / "index.html"
    html.write_text(
        '<!doctype html><html><head><meta charset="utf-8">\n'
        '<link rel="modulepreload" href="/assets/js/player.js">\n'
        '</head><body>\n'
        '<script type="module" src="/assets/js/app.js?v=194"></script>\n'
        "<script src='/assets/js/missing.js?v=2' defer></script>\n"
        '</body></html>\n')
    return tmp_path, js, html


def _bump(path, text):
    st = os.stat(path)
    path.write_text(text)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_every_module_url_carries_its_content_hash(tree):
    _root, js, html = tree
    body, etag = shell_html.render_shell(html, js)
    text = body.decode()
    imports = _map(text)
    assert set(imports) == {"/assets/js/app.js", "/assets/js/player.js", "/assets/js/mobile/app.js"}
    vers = shell_html.module_versions(js)
    for rel, h in vers.items():
        assert imports[f"/assets/js/{rel}"] == f"/assets/js/{rel}?v={h}"
    # the entry script (its hand-set ?v=194 replaced) and the preload link
    assert f'src="/assets/js/app.js?v={vers["app.js"]}"' in text
    assert f'href="/assets/js/player.js?v={vers["player.js"]}"' in text
    assert "?v=194" not in text
    # a URL to a file that isn't there is left as written
    assert "'/assets/js/missing.js?v=2'" in text
    # the map comes before the first thing that loads a module
    assert text.index('type="importmap"') < text.index('rel="modulepreload"')
    assert text.index('<meta charset="utf-8">') < text.index('type="importmap"')
    assert etag.startswith('"') and etag.endswith('"')


def test_a_changed_module_gets_a_new_url_and_the_others_keep_theirs(tree):
    _root, js, html = tree
    body1, etag1 = shell_html.render_shell(html, js)
    before = _map(body1.decode())
    _bump(js / "player.js", "export const P = 2;\n")
    body2, etag2 = shell_html.render_shell(html, js)
    after = _map(body2.decode())
    assert after["/assets/js/player.js"] != before["/assets/js/player.js"]
    assert after["/assets/js/app.js"] == before["/assets/js/app.js"]
    assert after["/assets/js/mobile/app.js"] == before["/assets/js/mobile/app.js"]
    assert etag2 != etag1
    # unchanged files: the same shell, the same ETag
    body3, etag3 = shell_html.render_shell(html, js)
    assert (body3, etag3) == (body2, etag2)


def test_the_hash_follows_the_content_not_the_mtime(tree):
    _root, js, _html = tree
    v1 = shell_html.module_versions(js)["player.js"]
    _bump(js / "player.js", "export const P = 1;\n")       # same bytes, new mtime
    assert shell_html.module_versions(js)["player.js"] == v1


def test_if_none_match():
    assert shell_html.shell_etag('"abc"', '"abc"')
    assert shell_html.shell_etag('W/"abc", "x"', '"abc"')
    assert shell_html.shell_etag("*", '"abc"')
    assert not shell_html.shell_etag('"abd"', '"abc"')
    assert not shell_html.shell_etag(None, '"abc"')


@pytest.fixture(scope="module")
def site():
    from soniqboom import main
    return TestClient(main.app)          # (no lifespan: only the routes + middleware)


@pytest.mark.parametrize("path,entry", [("/", "app.js"), ("/m", "mobile/app.js"),
                                         ("/some/spa/route", "app.js"), ("/?ui=desktop", "app.js")])
def test_served_shells_are_versioned(site, path, entry):
    from soniqboom import main
    r = site.get(path, headers={"User-Agent": "Mozilla/5.0 (Macintosh)"}, follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    imports = _map(r.text)
    vers = shell_html.module_versions(main.FRONTEND_DIR / "js")
    # every JS file the frontend has — player.js, library.js, admin.js (a lazy
    # import), the phone's modules — is in the map with its current hash
    assert len(imports) == len(vers) and len(vers) > 20
    for rel in ("player.js", "library.js", "admin.js", "auth.js", "mobile/app.js"):
        assert imports[f"/assets/js/{rel}"] == f"/assets/js/{rel}?v={vers[rel]}"
    assert f'/assets/js/{entry}?v={vers[entry]}"' in r.text
    # no module URL in the shell is left unversioned
    rest = _MAP_RE.sub("", r.text)
    assert not re.search(r'["\']/assets/js/[^"\'?]+\.js["\']', rest)


def test_shell_revalidates_with_a_304(site):
    r = site.get("/m")
    tag = r.headers["etag"]
    again = site.get("/m", headers={"If-None-Match": tag})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["cache-control"] == "no-cache"
