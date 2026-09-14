"""Oqim rejimi: "auto" — avval KICHIK oqim, kelmasa asosiysi.

Nega kerak. Substream trafikni ~36 barobar kamaytiradi (o'lchov: plitka
640x360 / ~0.11 Mbit/s, asosiysi ~4 Mbit/s), lekin HAMMA kamerada ham yo'q —
batareyali modellar va ba'zi NVR kanallari uni bermaydi. Qattiq `sub` bunday
kamerada qora ekran beradi, qattiq `main` esa grid'da bekorga 1440p tortadi.
`hikcloudstream` ham shu yo'ldan boradi: substream'ni sinab ko'rib, kerak
bo'lsa asosiysiga qaytadi.

Bu yerda proxy HAQIQATAN ishga tushiriladi (bulut o'rniga soxta oqim bilan)
va HTTP orqali o'qiladi — ya'ni qo'lda ochib-yopiladigan kontekst-menejer
ham tekshiriladi.
"""
import socket
import threading
import time
import urllib.request

import pytest

from cloudcam import decrypt_proxy, vtm_cache


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakePacket:
    def __init__(self, body):
        self.body = body


class FakeStream:
    """Soxta VTM oqimi. `packets` bo'sh bo'lsa — kamera jim (substream yo'q)."""

    def __init__(self, packets):
        self._packets = packets
        self.started = False

    def start(self):
        self.started = True

    def iter_packets(self, **kw):
        for p in self._packets:
            yield FakePacket(p)


class FakeCm:
    """`open_cloud_stream` qaytaradigan kontekst-menejer."""

    def __init__(self, packets):
        self.stream = FakeStream(packets)
        self.entered = 0
        self.exited = 0

    def __enter__(self):
        self.entered += 1
        return self.stream

    def __exit__(self, *exc):
        self.exited += 1
        return False


def _hevc_rtp(payload: bytes) -> bytes:
    """RTP sarlavhasi (v2, PT=96) + HEVC payload."""
    return bytes([0x80, 96, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0]) + payload


def _clear_vps() -> bytes:
    """Shifrlanmagan VPS — dekodlovchi "toza oqim" deb qaror qiladi."""
    return bytes([(32 << 1) & 0xFF, 0x01, 0x0C]) + bytes(range(1, 32))


PACKETS = [_hevc_rtp(_clear_vps()), _hevc_rtp(_clear_vps())]


@pytest.fixture
def proxy(monkeypatch):
    """`serve()` ni fon threadda ishga tushiradi, oxirida yopadi."""
    servers = []
    real_server_cls = decrypt_proxy.ThreadingHTTPServer

    def _capture(*a, **kw):
        srv = real_server_cls(*a, **kw)
        servers.append(srv)
        return srv

    monkeypatch.setattr(decrypt_proxy, "ThreadingHTTPServer", _capture)
    monkeypatch.setattr(decrypt_proxy, "_make_client", lambda: object())
    monkeypatch.setattr(vtm_cache, "channel_missing", lambda *a, **k: False)

    def _run(mode, opener):
        port = _free_port()
        monkeypatch.setattr(decrypt_proxy, "open_cloud_stream", opener)
        t = threading.Thread(
            target=decrypt_proxy.serve,
            args=("S1", port, "AUTO", 1), kwargs={"stream_mode": mode},
            daemon=True)
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("proxy ishga tushmadi")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/S1.ts", timeout=10) as r:
            return r.read()

    yield _run
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def test_main_mode_asks_for_the_main_stream(proxy, monkeypatch):
    asked = []
    monkeypatch.setattr(decrypt_proxy, "set_substream", lambda v: asked.append(v))
    cms = [FakeCm(PACKETS)]
    body = proxy("main", lambda *a, **k: cms[0])
    assert asked == [False], "main rejimda substream so'ralmasligi kerak"
    assert body, "kadr chiqmadi"
    assert cms[0].entered == 1 and cms[0].exited == 1, "oqim yopilmadi"


def test_sub_mode_asks_for_the_substream(proxy, monkeypatch):
    asked = []
    monkeypatch.setattr(decrypt_proxy, "set_substream", lambda v: asked.append(v))
    proxy("sub", lambda *a, **k: FakeCm(PACKETS))
    assert asked == [True]


def test_auto_uses_the_substream_when_it_delivers(proxy, monkeypatch):
    asked = []
    monkeypatch.setattr(decrypt_proxy, "set_substream", lambda v: asked.append(v))
    body = proxy("auto", lambda *a, **k: FakeCm(PACKETS))
    assert asked == [True], "substream ishlaganda asosiysiga o'tilmasligi kerak"
    assert body


def test_auto_falls_back_to_the_main_stream_when_the_substream_is_silent(
        proxy, monkeypatch):
    """Substream'i yo'q kamera: jim qoladi -> asosiy oqimga o'tiladi."""
    asked = []
    monkeypatch.setattr(decrypt_proxy, "set_substream", lambda v: asked.append(v))
    made = []

    def _open(*a, **k):
        cm = FakeCm([] if not made else PACKETS)
        made.append(cm)
        return cm

    body = proxy("auto", _open)
    assert asked == [True, False], f"asosiy oqimga qaytilmadi: {asked}"
    assert body, "asosiy oqimdan ham kadr kelmadi"
    assert made[0].exited == 1, "bo'sh substream ulanishi yopilmadi"


def test_auto_falls_back_when_opening_the_substream_raises(proxy, monkeypatch):
    """Bulut substream uchun umuman resurs bermasa ham asosiysi sinaladi."""
    asked = []
    monkeypatch.setattr(decrypt_proxy, "set_substream", lambda v: asked.append(v))
    calls = []

    def _open(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("no substream resource")
        return FakeCm(PACKETS)

    body = proxy("auto", _open)
    assert asked == [True, False]
    assert body
