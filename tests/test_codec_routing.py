# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-browser codec-routing helpers.

The stream handler direct-serves AAC-in-m4a to every browser and ALAC-in-m4a
to Safari (probing the real codec), and routes native Ogg/Opus away from Safari
< 18.4 (which can't decode it) to a WAV transcode.  These tests lock down the
UA-classification helpers that drive those decisions."""
from __future__ import annotations

from soniqboom.api.stream import _is_safari, _safari_lacks_ogg


class _Req:
    def __init__(self, ua: str):
        self.headers = {"user-agent": ua}


_SAFARI_17 = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15")
_SAFARI_183 = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
               "(KHTML, like Gecko) Version/18.3 Safari/605.1.15")
_SAFARI_184 = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
               "(KHTML, like Gecko) Version/18.4 Safari/605.1.15")
_SAFARI_19 = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/19.0 Safari/605.1.15")
_SAFARI_NOVER = "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Safari/605.1.15"
_CHROME = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/148.0 Safari/537.36")
_EDGE = ("Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/148.0 Safari/537.36 Edg/148.0")
_FIREFOX = "Mozilla/5.0 (Windows NT 10.0; rv:130.0) Gecko/20100101 Firefox/130.0"


def test_is_safari_distinguishes_from_chromium():
    assert _is_safari(_Req(_SAFARI_17)) is True
    assert _is_safari(_Req(_SAFARI_19)) is True
    assert _is_safari(_Req(_CHROME)) is False    # Chrome UA also contains "Safari"
    assert _is_safari(_Req(_EDGE)) is False
    assert _is_safari(_Req(_FIREFOX)) is False


def test_safari_lacks_ogg_version_gate():
    # Safari < 18.4 can't decode Opus/Vorbis-in-Ogg → route to WAV transcode.
    assert _safari_lacks_ogg(_Req(_SAFARI_17)) is True
    assert _safari_lacks_ogg(_Req(_SAFARI_183)) is True
    assert _safari_lacks_ogg(_Req(_SAFARI_NOVER)) is True     # unparseable → assume old
    # Safari >= 18.4 plays Ogg natively.
    assert _safari_lacks_ogg(_Req(_SAFARI_184)) is False
    assert _safari_lacks_ogg(_Req(_SAFARI_19)) is False
    # Non-Safari always plays Ogg/Opus natively.
    assert _safari_lacks_ogg(_Req(_CHROME)) is False
    assert _safari_lacks_ogg(_Req(_FIREFOX)) is False
