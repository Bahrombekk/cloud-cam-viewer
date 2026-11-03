"""Bulutdan KICHIK oqim (substream) so'rash — grid uchun.

O'lchov (bir xil devor, 23 plitka): asosiy oqimda tasvir to'kilib qotgan;
substream bilan plitka 640x360 / ~0.11 Mbit/s (asosiysi ~4 Mbit/s) va hammasi
toza dekodlangan.

Ilgari bu kutubxona URL quruvchisining global almashtirilishi (monkeypatch +
regex) edi — upstream URL formati o'zgarsa substream JIMGINA asosiy oqimga
aylanardi. Endi `stream_type` — URL quruvchining o'z parametri.
"""
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import cloudcam
from cloudcam import decrypt_proxy
from cloudcam.sources.cloud.vtm import STREAM_MAIN, STREAM_SUB, VtmError, build_vtm_url

PKG = Path(cloudcam.__file__).resolve().parent
MGR = PKG / "stream_manager.py"


def _q(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def _url(**kw):
    return build_vtm_url("1.2.3.4", 8554, "A1", "?biz=7&stream=9", "TOK",
                         channel=2, timestamp_ms=1, **kw)


def test_main_stream_is_the_default():
    assert _q(_url())["stream"] == "1"


def test_substream_sets_only_the_stream_parameter():
    main, sub = _q(_url(stream_type=STREAM_MAIN)), _q(_url(stream_type=STREAM_SUB))
    assert sub["stream"] == "2"
    assert {k: v for k, v in sub.items() if k != "stream"} == \
        {k: v for k, v in main.items() if k != "stream"}


def test_our_stream_type_wins_over_stream_biz_params():
    """`streamBizUrl` dagi `stream` bizniki ustidan yozib ketmasligi kerak."""
    q = _q(_url(stream_type=STREAM_SUB))
    assert q["stream"] == "2" and q["biz"] == "7"


def test_unknown_stream_type_is_rejected():
    with pytest.raises(VtmError):
        _url(stream_type=3)


def test_proxy_passes_stream_type_to_the_url_builder(monkeypatch):
    seen = {}

    def _open(pagelist, vtdu, serial, **kw):
        seen.update(kw)
        return "cm"

    monkeypatch.setattr(decrypt_proxy.vtm_cache, "pagelist", lambda c: {})
    monkeypatch.setattr(decrypt_proxy.vtm_cache, "vtdu", lambda c: {})
    monkeypatch.setattr(decrypt_proxy._vtm, "open_cloud_stream", _open)
    assert decrypt_proxy.open_cloud_stream(object(), "A1", channel=3,
                                           stream_type=STREAM_SUB) == "cm"
    assert seen["stream_type"] == STREAM_SUB and seen["channel"] == 3


def test_manager_passes_the_flag_when_enabled():
    text = MGR.read_text(encoding="utf-8")
    assert re.search(r"\.substream", text)
    assert "--stream=" in text, "oqim rejimi bola-jarayonga uzatilmayapti"


def test_auto_mode_tries_substream_first_then_main():
    """"auto" — avval KICHIK oqim, kelmasa asosiysi.

    Hamma kamerada substream yo'q (batareyali modellar, ba'zi NVR kanallari):
    qattiq `sub` bunday kamerada qora ekran, qattiq `main` esa grid'da
    bekorga 1440p. Tartib va probe muddati shu yerda qo'riqlanadi."""
    src = (PKG / "decrypt_proxy.py"
           ).read_text(encoding="utf-8")
    m = re.search(r'attempts\s*=\s*\[\("sub",\s*STREAM_PROBE_TIMEOUT\),\s*\("main"', src)
    assert m, "auto rejimda substream BIRINCHI sinalishi kerak"
    assert decrypt_proxy.STREAM_PROBE_TIMEOUT <= 10.0, \
        "probe uzoq bo'lsa substreamsiz kamera sekin ochiladi"
