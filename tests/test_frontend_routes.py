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
