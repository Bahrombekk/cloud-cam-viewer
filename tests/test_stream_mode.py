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

from cloudcam.sources.cloud import proxy as decrypt_proxy

from cloudcam.sources.cloud import vtm_cache
from cloudcam.sources.cloud.vtm import STREAM_SUB


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
        self.timeouts = []

    def set_timeout(self, t):
        self.timeouts.append(t)

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

    asked = []          # har ochilishda: substream so'raldimi (stream_type)

    def _run(mode, opener):
        port = _free_port()

        def _open(*a, **k):
            asked.append(k.get("stream_type") == STREAM_SUB)
            return opener(*a, **k)

        monkeypatch.setattr(decrypt_proxy, "open_cloud_stream", _open)
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

    _run.asked = asked
    yield _run
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def test_main_mode_asks_for_the_main_stream(proxy, monkeypatch):
    asked = proxy.asked
    cms = [FakeCm(PACKETS)]
    body = proxy("main", lambda *a, **k: cms[0])
    assert asked == [False], "main rejimda substream so'ralmasligi kerak"
    assert body, "kadr chiqmadi"
    assert cms[0].entered == 1 and cms[0].exited == 1, "oqim yopilmadi"


def test_sub_mode_asks_for_the_substream(proxy, monkeypatch):
    asked = proxy.asked
    proxy("sub", lambda *a, **k: FakeCm(PACKETS))
    assert asked == [True]


def test_auto_uses_the_substream_when_it_delivers(proxy, monkeypatch):
    asked = proxy.asked
    body = proxy("auto", lambda *a, **k: FakeCm(PACKETS))
    assert asked == [True], "substream ishlaganda asosiysiga o'tilmasligi kerak"
    assert body


def test_auto_falls_back_to_the_main_stream_when_the_substream_is_silent(
        proxy, monkeypatch):
    """Substream'i yo'q kamera: jim qoladi -> asosiy oqimga o'tiladi."""
    asked = proxy.asked
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
    asked = proxy.asked
    calls = []

    def _open(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("no substream resource")
        return FakeCm(PACKETS)

    body = proxy("auto", _open)
    assert asked == [True, False]
    assert body


def test_probe_timeout_does_not_stay_for_the_whole_session(proxy, monkeypatch):
    """Substream 6 s lik SINOV timeout'i bilan ochiladi. U sessiyaga qolsa, bulutning
    ~10 s pauzasida (bir kanalning ikkinchi oqimi yopilganda, DS-7608NI-Q1 da
    o'lchangan) recv uzilib, `cloudcam serve` dagi `/sub` yo'li o'zini yopardi."""
    cm = FakeCm(PACKETS)
    proxy("auto", lambda *a, **k: cm)
    assert cm.stream.timeouts == [decrypt_proxy.STREAM_TIMEOUT]
    assert decrypt_proxy.STREAM_TIMEOUT > 10 > decrypt_proxy.STREAM_PROBE_TIMEOUT


def test_main_mode_keeps_its_timeout(proxy, monkeypatch):
    cm = FakeCm(PACKETS)
    proxy("main", lambda *a, **k: cm)
    assert cm.stream.timeouts == [], "asosiy oqim boshidanoq STREAM_TIMEOUT bilan ochiladi"


def test_vtm_client_set_timeout_applies_to_the_open_socket():
    from cloudcam.sources.cloud.vtm.protocol import VtmStreamClient

    class Sock:
        def __init__(self):
            self.t = []

        def settimeout(self, t):
            self.t.append(t)

        def close(self):
            pass

    sk = Sock()
    c = VtmStreamClient("ysproto://h:8554/live?x=1", timeout=6.0,
                        socket_factory=lambda addr, t: sk)
    c.connect()
    c.set_timeout(20.0)
    assert sk.t == [6.0, 20.0] and c.timeout == 20.0
