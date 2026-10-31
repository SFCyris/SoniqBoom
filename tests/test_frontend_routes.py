# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SPA routes hang off the frontend check, not the optional-manual check.

main.py registers the frontend routes at import, and it is imported once per
process — so each layout (manual missing, frontend missing) is probed in a
fresh interpreter with that directory hidden from ``pathlib``."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_PROBE = r'''
import json, logging, pathlib, sys
hide = set(filter(None, sys.argv[1].split(",")))

def _hidden(p):
    parts = pathlib.Path(p).parts
    return (("docs" in hide and parts[-2:] == ("docs", "manual"))
            or ("frontend" in hide and parts[-1:] == ("frontend",)))

for _name in ("is_dir", "exists"):
    def _patched(self, *a, _orig=getattr(pathlib.Path, _name), **k):
        return False if _hidden(self) else _orig(self, *a, **k)
    setattr(pathlib.Path, _name, _patched)

records = []
class _Capture(logging.Handler):
    def emit(self, r):
        records.append([r.levelname, r.getMessage()])
_log = logging.getLogger("soniqboom")
_log.addHandler(_Capture())
_log.setLevel(logging.INFO)

from fastapi.testclient import TestClient
from soniqboom import main
c = TestClient(main.app)
hits = {}
for path in ("/some/spa/route", "/api/does-not-exist", "/m", "/sw.js"):
    r = c.get(path, follow_redirects=False)
    hits[path] = [r.status_code, r.headers.get("content-type", "")]
print(json.dumps({
    "main_file": main.__file__,
    "paths": [getattr(r, "path", "") for r in main.app.routes],
    "logs": records,
    "hits": hits,
}))
'''

_SPA_ROUTES = ("/sw.js", "/assets/sw.js", "/m", "/m/{rest:path}",
               "/multiroom", "/multiroom/{rest:path}", "/")
_FALLBACK = "/{full_path:path}"


def _import_main(repo_root, tmp_path, hide):
    conf = tmp_path / "SoniqBoom.conf"
    conf.write_text(json.dumps({"services": {"multiroom": True}}))
    env = {**os.environ, "SONIQBOOM_CONF": str(conf)}
    r = subprocess.run([sys.executable, "-c", _PROBE, ",".join(hide)], cwd=repo_root,
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    # The probe must have imported THIS checkout, not an installed copy.
    assert Path(out["main_file"]).resolve().is_relative_to(Path(repo_root).resolve())
    return out


def _errors(out):
    return [msg for level, msg in out["logs"] if level == "ERROR"]


def test_spa_routes_register_when_the_manual_is_missing(repo_root, tmp_path):
    out = _import_main(repo_root, tmp_path, hide=("docs",))
    paths = out["paths"]
    assert "/manual" not in paths                      # the manual really was hidden
    assert "/assets" in paths
    for p in (*_SPA_ROUTES, _FALLBACK):
        assert paths.count(p) == 1, p
    # The catch-all must follow every explicit SPA route or it shadows them.
    assert paths.index(_FALLBACK) > max(paths.index(p) for p in _SPA_ROUTES)
    assert _errors(out) == []
    assert out["hits"]["/some/spa/route"] == [200, "text/html; charset=utf-8"]
    assert out["hits"]["/api/does-not-exist"] == [404, "application/json"]
    assert out["hits"]["/m"][0] == 200 and out["hits"]["/sw.js"][0] == 200


def test_missing_frontend_logs_the_error_and_registers_no_spa_routes(repo_root, tmp_path):
    out = _import_main(repo_root, tmp_path, hide=("frontend",))
    paths = out["paths"]
    if (repo_root / "docs" / "manual").is_dir():
        assert "/manual" in paths                      # the manual still mounts
    for p in ("/assets", *_SPA_ROUTES, _FALLBACK):
        assert p not in paths, p
    errs = _errors(out)
    assert len(errs) == 1 and errs[0].startswith("Frontend directory not found")
    assert out["hits"]["/some/spa/route"][0] == 404


def test_both_present_serves_frontend_and_manual_without_error(repo_root, tmp_path):
    if not (repo_root / "docs" / "manual").is_dir():
        pytest.skip("checkout ships without docs/manual")
    out = _import_main(repo_root, tmp_path, hide=())
    paths = out["paths"]
    for p in ("/assets", "/manual", *_SPA_ROUTES, _FALLBACK):
        assert paths.count(p) == 1, p
    assert _errors(out) == []
    assert out["hits"]["/some/spa/route"] == [200, "text/html; charset=utf-8"]


# ── / picks the interface: ?ui= pins it, the cookie wins, phones go to /m ────

_UA_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
_UA_IPAD_DESKTOP = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")
_UA_IPAD = ("Mozilla/5.0 (iPad; CPU OS 12_5 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/12.1 Safari/605.1.15")
_UA_ANDROID_TABLET = ("Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_UA_WINDOWS = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


@pytest.fixture
def site():
    from fastapi.testclient import TestClient
    from soniqboom import main
    return TestClient(main.app)          # (no lifespan: only the routes); fresh cookies per test


def _get(site, path, ua, cookie=None):
    headers = {"User-Agent": ua}
    if cookie:
        headers["Cookie"] = cookie
    return site.get(path, headers=headers, follow_redirects=False)


@pytest.mark.parametrize("ua", [_UA_IPHONE, _UA_IPAD, _UA_ANDROID_TABLET])
def test_phone_and_tablet_user_agents_go_to_m_unpinned(site, ua):
    r = _get(site, "/", ua)
    assert r.status_code == 302 and r.headers["location"] == "/m"
    assert "set-cookie" not in r.headers            # auto-detection never pins


@pytest.mark.parametrize("ua", [_UA_IPAD_DESKTOP, _UA_WINDOWS])
def test_desktop_user_agents_get_index_with_the_detection_script(site, ua):
    # An iPad asking for the desktop site sends a Macintosh UA: the server serves
    # index.html, whose head script tells it apart (tests/js/ui_detect.test.mjs).
    r = _get(site, "/", ua)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "set-cookie" not in r.headers
    assert "[ui-detect:begin]" in r.text and "function sbTouchDevice" in r.text
    assert 'id="btn-phone-ui"' in r.text and 'href="/?ui=mobile"' in r.text
    # the script comes before the first stylesheet and the import map
    assert r.text.index("[ui-detect:begin]") < r.text.index('<link rel="stylesheet"')
    assert r.text.index("[ui-detect:begin]") < r.text.index('<script type="importmap">')


@pytest.mark.parametrize("ua", [_UA_IPHONE, _UA_IPAD_DESKTOP, _UA_WINDOWS])
def test_ui_query_pins_the_choice(site, ua):
    r = _get(site, "/?ui=desktop", ua)
    assert r.status_code == 200 and "[ui-detect:begin]" in r.text
    ck = r.headers["set-cookie"]
    assert ck.startswith("sb_ui=desktop;") and "Max-Age=31536000" in ck and "Path=/" in ck
    assert "samesite=lax" in ck.lower()
    r = _get(site, "/?ui=mobile", ua)
    assert r.status_code == 302 and r.headers["location"] == "/m"
    assert r.headers["set-cookie"].startswith("sb_ui=mobile;")
    # anything else is not a choice
    r = _get(site, "/?ui=tablet", ua)
    assert "set-cookie" not in r.headers


def test_pinned_cookie_beats_the_user_agent(site):
    r = _get(site, "/", _UA_IPHONE, cookie="sb_ui=desktop")
    assert r.status_code == 200 and 'id="btn-phone-ui"' in r.text
    r = _get(site, "/", _UA_WINDOWS, cookie="sb_ui=mobile")
    assert r.status_code == 302 and r.headers["location"] == "/m"


def test_the_phone_shell_never_pins(site):
    for ua in (_UA_IPHONE, _UA_IPAD_DESKTOP, _UA_WINDOWS):
        r = _get(site, "/m", ua)
        assert r.status_code == 200 and "set-cookie" not in r.headers
        assert 'id="m-desktop-link"' in r.text and 'href="/?ui=desktop"' in r.text
