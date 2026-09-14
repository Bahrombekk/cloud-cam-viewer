"""Bulutdan KICHIK oqim (substream) so'rash — grid uchun.

O'lchov (bir xil devor, 23 plitka): asosiy oqimda tasvir to'kilib qotgan;
substream bilan plitka 640x360 / ~0.11 Mbit/s (asosiysi ~4 Mbit/s) va hammasi
toza dekodlangan.
"""
import re
from pathlib import Path

import pyezvizapi.cloud_stream as _cs
import pytest

from cloudcam import decrypt_proxy

MGR = Path(__file__).resolve().parents[1] / "cloudcam" / "stream_manager.py"


@pytest.fixture(autouse=True)
def _fresh_patch():
    """ASL `build_vtm_url` BIR MARTA saqlanadi (qayta-patch qilmaslik uchun) —
    testlar orasida shu xotirani tozalaymiz."""
    decrypt_proxy._orig_build_vtm_url = None
    yield
    decrypt_proxy._orig_build_vtm_url = None


def test_substream_rewrites_only_the_stream_parameter():
    _cs.build_vtm_url = lambda *a, **k: "rtsp://vtm/x?dev=A1&stream=1&ch=2"
    try:
        decrypt_proxy.set_substream(True)
        assert _cs.build_vtm_url() == "rtsp://vtm/x?dev=A1&stream=2&ch=2"
    finally:
        decrypt_proxy.set_substream(False)


def test_substream_can_be_switched_back():
    orig = "rtsp://vtm/x?stream=1"
    _cs.build_vtm_url = lambda *a, **k: orig
    decrypt_proxy.set_substream(True)
    decrypt_proxy.set_substream(False)
    assert _cs.build_vtm_url() == orig


def test_substream_does_not_touch_similar_looking_values():
    _cs.build_vtm_url = lambda *a, **k: "rtsp://vtm/x?substream=1&stream=1&downstream=1"
    try:
        decrypt_proxy.set_substream(True)
        out = _cs.build_vtm_url()
        assert "substream=1" in out and "downstream=1" in out and "&stream=2" in out
    finally:
        decrypt_proxy.set_substream(False)


def test_manager_passes_the_flag_when_enabled():
    text = MGR.read_text(encoding="utf-8")
    assert re.search(r"\.substream", text)
    assert "--stream=" in text, "oqim rejimi bola-jarayonga uzatilmayapti"


def test_auto_mode_tries_substream_first_then_main():
    """"auto" — avval KICHIK oqim, kelmasa asosiysi.

    Hamma kamerada substream yo'q (batareyali modellar, ba'zi NVR kanallari):
    qattiq `sub` bunday kamerada qora ekran, qattiq `main` esa grid'da
    bekorga 1440p. Tartib va probe muddati shu yerda qo'riqlanadi."""
    src = (Path(__file__).resolve().parents[1] / "cloudcam" / "decrypt_proxy.py"
           ).read_text(encoding="utf-8")
    m = re.search(r'attempts\s*=\s*\[\("sub",\s*STREAM_PROBE_TIMEOUT\),\s*\("main"', src)
    assert m, "auto rejimda substream BIRINCHI sinalishi kerak"
    assert decrypt_proxy.STREAM_PROBE_TIMEOUT <= 10.0, \
        "probe uzoq bo'lsa substreamsiz kamera sekin ochiladi"
