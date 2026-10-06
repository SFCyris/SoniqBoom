"""The song database downloads from one fixed upstream commit, and the
settings' "Validated set" link opens exactly that commit."""
import re
from pathlib import Path

from soniqboom.core import songdb


def test_download_is_pinned_to_a_full_commit():
    assert re.fullmatch(r"[0-9a-f]{40}", songdb.COMMIT)
    assert f"/{songdb.COMMIT}/tsv/pretty/md5/" in songdb.BASE_URL
    assert "/master/" not in songdb.BASE_URL


def test_settings_link_points_at_the_same_commit():
    html = (Path(songdb.__file__).parents[1] / "frontend" / "index.html").read_text()
    m = re.search(r'id="md-songdb-validated"[^>]*?href="([^"]+)"', html, re.S)
    assert m and m.group(1) == songdb.VALIDATED_URL


import pytest
from soniqboom.core import folder_album as fa
from tests.test_songdb import META, LENS, _tsvs  # noqa: E402


@pytest.fixture
def refresh_env(tmp_path, monkeypatch):
    monkeypatch.setattr(songdb, "_MIN_META_ROWS", 1)
    monkeypatch.setattr(songdb, "_MIN_LENGTH_ROWS", 1)
    db = tmp_path / "scene" / "songdb.sqlite"
    db.parent.mkdir()
    monkeypatch.setattr(songdb, "_db_path", lambda: db)
    _tsvs(tmp_path, META, LENS)
    songdb._status.update(refreshing=False, error=None, meta_rows=0, length_rows=0, built_at=None)
    return tmp_path


def _fake(tmp, log, gone_bases=()):
    def download(name, dest, base=None):
        base = base or songdb.BASE_URL
        log.append((name, base))
        if base in gone_bases:
            raise songdb._PinnedGone(f"{name}: 404")
        dest.write_bytes((tmp / name).read_bytes())
    return download


def test_the_validated_commit_is_downloaded_and_labelled(refresh_env, monkeypatch):
    log = []
    monkeypatch.setattr(songdb, "_download", _fake(refresh_env, log))
    st = songdb.refresh_index()
    assert st["error"] is None
    assert {b for _, b in log} == {songdb.BASE_URL}
    assert st["source_set"] == "validated" and st["source_commit"] == songdb.COMMIT
    assert st["newer_upstream"] is None


def test_a_gone_pinned_commit_falls_back_to_the_latest_files_labelled_unvalidated(refresh_env, monkeypatch):
    log = []
    monkeypatch.setattr(songdb, "_download", _fake(refresh_env, log, gone_bases={songdb.BASE_URL}))
    st = songdb.refresh_index()
    assert st["error"] is None and st["meta_rows"] > 0
    assert (log[0][1], log[-1][1]) == (songdb.BASE_URL, songdb.LATEST_URL)
    assert st["source_set"] == "latest" and st["source_commit"] == "master"


def test_a_transient_failure_does_not_fall_back(refresh_env, monkeypatch):
    log = []

    def flaky(name, dest, base=None):
        log.append(base or songdb.BASE_URL)
        raise RuntimeError("download of x failed: timeout")
    monkeypatch.setattr(songdb, "_download", flaky)
    st = songdb.refresh_index()
    assert "index refresh failed" in st["error"]
    assert set(log) == {songdb.BASE_URL}, "only a 404 may switch to the moving branch"


def test_newer_upstream_data_is_reported_not_installed(refresh_env, monkeypatch):
    log = []
    monkeypatch.setattr(songdb, "_download", _fake(refresh_env, log))
    monkeypatch.setattr(songdb, "_latest_upstream",
                        lambda: {"sha": "f" * 40, "date": "2026-09-28T16:34:50Z"})
    st = songdb.refresh_index()
    assert st["source_set"] == "validated"
    assert st["newer_upstream"] == {"sha": "f" * 40, "date": "2026-09-28T16:34:50Z"}
    assert {b for _, b in log} == {songdb.BASE_URL}
    # the pinned commit itself, or an older one, is not "newer"
    assert songdb._newer_than_pinned({"sha": songdb.COMMIT, "date": "2099-01-01T00:00:00Z"}) is None
    assert songdb._newer_than_pinned({"sha": "a" * 40, "date": "2026-01-01T00:00:00Z"}) is None


def test_download_raises_pinned_gone_on_404_without_retrying(tmp_path, monkeypatch):
    import httpx
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(404)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(songdb._PinnedGone):
        songdb._download("metadata.tsv", tmp_path / "m.part")
    assert len(calls) == 1 and songdb.COMMIT in calls[0]
