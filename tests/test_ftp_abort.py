"""An early-stopped FTP transfer on a server that never answers an urgent
``ABOR``: the abort is waited for briefly, then sent in-band, then (if that
goes unanswered too) the connection is closed instead (``_FTP_ABORT_MODE``).

The suite FTP server's ``oob_inline=False`` loses the urgent byte of
``ftplib``'s ``ABOR`` (as a server that never reads TCP urgent data inline
does), so that ``ABOR`` line never ends and is never answered."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _ftp_server import FtpServer  # noqa: E402

from soniqboom.core import filesource  # noqa: E402
from soniqboom.core.filesource import FTPFileSource  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_modes(monkeypatch):
    monkeypatch.setattr(filesource, "_FTP_ABORT_MODE", {})
    monkeypatch.setattr(filesource, "_FTP_ABORT_MISSES", {})
    monkeypatch.setattr(filesource, "_FTP_ABORT_LAST_MISS", {})
    monkeypatch.setattr(filesource, "_FTP_ABORT_CLOSED_AT", {})
    monkeypatch.setattr(filesource, "_FTP_ABORT_WAIT_S", 0.5)


def _share(tmp_path: Path, n: int = 6) -> Path:
    lib = tmp_path / "share" / "lib"
    lib.mkdir(parents=True)
    blob = os.urandom(2 * 1024 * 1024)
    for i in range(n):
        (lib / f"f{i}.bin").write_bytes(blob)
    return lib


def _read_heads(src: FTPFileSource, n: int) -> list[bytes]:
    return [src.read_at(f"/f{i}.bin", 0, 4096) for i in range(n)]


def test_an_unanswered_urgent_abor_switches_to_in_band(tmp_path):
    lib = _share(tmp_path)
    srv = FtpServer(lib.parent, oob_inline=False).start()
    try:
        src = FTPFileSource("127.0.0.1", port=srv.port, remote_path="/lib")
        t0 = time.monotonic()
        heads = _read_heads(src, 6)
        took = time.monotonic() - t0
        blob = (lib / "f0.bin").read_bytes()[:4096]
        assert heads == [blob] * 6
        assert filesource.ftp_abort_mode("127.0.0.1", srv.port) == "inband"
        # one short wait to learn it (was the 15 s socket timeout per read)
        assert took < 0.5 + 3.0, took
        assert srv.commands["ABOR"] == 5         # every later read: answered in-band
        # only the first connection was lost (the pool also keeps warm ones;
        # a new login per read would be 6 or more)
        assert srv.logins <= 4, srv.logins
        src.close()
    finally:
        srv.stop()


def test_a_normal_server_keeps_the_urgent_abor(tmp_path, monkeypatch):
    urgent = []
    real = filesource._TypedFTP.abort
    monkeypatch.setattr(filesource._TypedFTP, "abort",
                        lambda self: (urgent.append(1), real(self))[1])
    lib = _share(tmp_path)
    srv = FtpServer(lib.parent).start()
    try:
        src = FTPFileSource("127.0.0.1", port=srv.port, remote_path="/lib")
        _read_heads(src, 4)
        assert filesource.ftp_abort_mode("127.0.0.1", srv.port) == "oob"
        assert len(urgent) == 4                  # sent as urgent data (ftplib's abort)
        assert srv.commands["ABOR"] == 4 and srv.logins <= 3, srv.logins
        src.close()
    finally:
        srv.stop()


def test_a_server_mute_after_a_stopped_transfer_ends_in_close(tmp_path):
    """Neither ABOR is answered (nor anything after it): two misses in a
    row, then each stopped read just closes its connection — no wait."""
    lib = _share(tmp_path, n=8)
    srv = FtpServer(lib.parent, mute_after_abort=True).start()
    try:
        src = FTPFileSource("127.0.0.1", port=srv.port, remote_path="/lib")
        times = []
        for i in range(8):
            t0 = time.monotonic()
            assert src.read_at(f"/f{i}.bin", 0, 4096) == (lib / "f0.bin").read_bytes()[:4096]
            times.append(time.monotonic() - t0)
        assert filesource.ftp_abort_mode("127.0.0.1", srv.port) == "close"
        assert all(t >= 0.45 for t in times[:3]), times      # oob, inband, inband: waited
        assert all(t < 0.4 for t in times[3:]), times        # close: no wait
        assert srv.commands["MUTED"] >= 3
        src.close()
    finally:
        srv.stop()


class _FakeSock:
    def __init__(self):
        self.timeout = 15.0

    def gettimeout(self):
        return self.timeout

    def settimeout(self, t):
        self.timeout = t


class _FakeFTP:
    """Answers ``replies`` in turn; ``TimeoutError`` once they run out."""

    def __init__(self, replies, host="h", port=21):
        self.host, self.port = host, port
        self.sock = _FakeSock()
        self.replies = list(replies)
        self.sent: list[str] = []

    def abort(self):
        self.sent.append("ABOR(urgent)")
        return self.getresp()

    def putcmd(self, line):
        self.sent.append(line)

    def getresp(self):
        if not self.replies:
            raise TimeoutError("timed out")
        return self.replies.pop(0)


def _abort(ftp) -> filesource._PooledFTP:
    h = filesource._PooledFTP(ftp)
    h.abort_transfer()
    return h


def test_the_ladder_and_its_resets():
    # answered: the timeout is put back, nothing learnt
    ftp = _FakeFTP(["426 aborted", "200 ok"])
    h = _abort(ftp)
    assert not h._broken and ftp.sock.timeout == 15.0
    assert filesource.ftp_abort_mode("h", 21) == "oob"
    # unanswered urgent ABOR → in-band, with the short wait applied
    ftp = _FakeFTP([])
    h = _abort(ftp)
    assert h._broken and ftp.sock.timeout == 0.5
    assert filesource.ftp_abort_mode("h", 21) == "inband"
    # one unanswered in-band ABOR is not enough to give up ABOR …
    h = _abort(_FakeFTP([]))
    assert h._broken and filesource.ftp_abort_mode("h", 21) == "inband"
    # … and an answered one in between resets the count
    ftp = _FakeFTP(["225 nothing to abort", "200 ok"])
    assert not _abort(ftp)._broken and ftp.sent == ["ABOR", "NOOP"]
    _abort(_FakeFTP([]))
    assert filesource.ftp_abort_mode("h", 21) == "inband"
    _abort(_FakeFTP([]))
    assert filesource.ftp_abort_mode("h", 21) == "close"
    # close: nothing is sent, the connection goes
    ftp = _FakeFTP(["200 ok"])
    h = _abort(ftp)
    assert h._broken and ftp.sent == []
    # other servers are not affected
    assert filesource.ftp_abort_mode("other", 21) == "oob"


def test_one_stall_counts_once():
    """Misses of ABORs sent before the last counted miss are the same stall:
    eight concurrent in-band misses are one, not a step to "close"."""
    key = ("h", 21)
    filesource._FTP_ABORT_MODE[key] = "inband"
    sent = time.monotonic()                     # all sent before any miss is seen
    gate = threading.Barrier(8)

    def miss():
        gate.wait()
        filesource._abort_unanswered(key, "inband", sent)

    threads = [threading.Thread(target=miss) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert filesource.ftp_abort_mode(*key) == "inband"
    assert filesource._FTP_ABORT_MISSES[key] == 1
    # an ABOR sent after that miss is a new stall: the second counted miss
    filesource._abort_unanswered(key, "inband", time.monotonic())
    assert filesource.ftp_abort_mode(*key) == "close"


def test_concurrent_urgent_misses_step_once():
    key = ("h", 21)
    sent = time.monotonic()
    for _ in range(8):
        filesource._abort_unanswered(key, "oob", sent)
    assert filesource.ftp_abort_mode(*key) == "inband"
    assert filesource._FTP_ABORT_MISSES.get(key, 0) == 0


def test_close_tries_in_band_again_later(monkeypatch):
    key = ("h", 21)
    filesource._FTP_ABORT_MODE[key] = "inband"
    filesource._abort_unanswered(key, "inband", time.monotonic())
    filesource._abort_unanswered(key, "inband", time.monotonic())
    assert filesource.ftp_abort_mode(*key) == "close"
    monkeypatch.setattr(filesource, "_FTP_ABORT_RETRY_S", 0.0)
    assert filesource.ftp_abort_mode(*key) == "inband"
    assert filesource._FTP_ABORT_MISSES.get(key, 0) == 0


def test_a_rejected_urgent_reply_moves_to_in_band():
    """ftplib's ``abort`` refuses a first reply other than 426/225/226 (a 451
    for the transfer): the connection goes, and the server gets in-band
    ABORs, whose exchange reads any reply."""
    ftp = _FakeFTP([])

    def bad_abort():
        ftp.sent.append("ABOR(urgent)")
        raise filesource.ftplib.error_proto("451 local error")

    ftp.abort = bad_abort
    assert _abort(ftp)._broken and ftp.sent == ["ABOR(urgent)"]
    assert filesource.ftp_abort_mode("h", 21) == "inband"
    ftp = _FakeFTP(["451 local error", "226 aborted", "200 ok"])
    assert not _abort(ftp)._broken and ftp.sent == ["ABOR", "NOOP"]


def test_other_errors_teach_nothing():
    ftp = _FakeFTP([])

    def gone():
        raise ConnectionResetError("reset")

    ftp.abort = gone
    assert _abort(ftp)._broken
    assert filesource.ftp_abort_mode("h", 21) == "oob"
