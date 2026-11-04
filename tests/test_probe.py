"""Kanal holatini tez tekshirish (`stream.probe`) — tarmoqsiz.

Qurilma holati NVR darajasida bo'lgani uchun ro'yxatda hamma kanal "yashil"
ko'rinardi; jonli hisobda 57 dan 22 tasi aslida oqim bermagan. Oflayn kanalda
bulut javob bermaydi (soket jim) — substream'i yo'q kamera ham shunday, shuning
uchun jimlikdan keyin asosiy oqim ham sinaladi."""
import types

from cloudcam.sources.cloud import stream
from cloudcam.sources.cloud.vtm import STREAM_MAIN, STREAM_SUB, DeviceOffline

META = ({"pagelist": 1}, {"vtdu": 1})
SILENT = DeviceOffline("Device offline or unreachable: timed out waiting for VTM stream data")


class FakeStream:
    def __init__(self, packets=1, fail=None, result=0):
        self.packets, self.fail, self.result = packets, fail, result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def start(self):
        return types.SimpleNamespace(result=self.result)

    def iter_packets(self, max_packets=None, keepalive_interval=None):
        if self.fail:
            raise self.fail
        yield from range(min(self.packets, max_packets or self.packets))


def _patch(monkeypatch, behaviour):
    """behaviour[(serial, ch, stream_type)] yoki behaviour[(serial, ch)] (ikkala oqim)."""
    calls = []

    def fake_open(pl, vt, serial, channel=1, **kw):
        st = kw.get("stream_type")
        calls.append((serial, channel, st))
        b = behaviour.get((serial, channel, st), behaviour.get((serial, channel)))
        if isinstance(b, Exception):
            raise b
        return b

    monkeypatch.setattr(stream._vtm, "open_cloud_stream", fake_open)
    monkeypatch.setattr(stream.vtm_cache, "pagelist", lambda c: META[0])
    monkeypatch.setattr(stream.vtm_cache, "vtdu", lambda c: META[1])
    return calls


def test_probe_classifies(monkeypatch):
    _patch(monkeypatch, {
        ("S", 1): FakeStream(),
        ("S", 2): FakeStream(fail=SILENT),
        ("S", 3): RuntimeError("Could not find VTM resource for S"),
        ("S", 4): FakeStream(packets=0),
        ("S", 5): RuntimeError("HTTP 500"),
        ("S", 6): FakeStream(result=5),
        ("S", 7): FakeStream(result=stream.NO_STREAM_RESULT),
    })
    got = {ch: stream.probe(object(), "S", ch, meta=META) for ch in range(1, 8)}
    assert got == {1: "online", 2: "offline", 3: "offline", 4: "offline",
                   5: "unknown", 6: "unknown", 7: "offline"}


def test_substream_6106_still_tries_main(monkeypatch):
    """Jonli: kanal substream'ga 6106, asosiyga 0 bergan — 6106 yolg'iz dalil emas."""
    calls = _patch(monkeypatch, {
        ("B", 1, STREAM_SUB): FakeStream(result=stream.NO_STREAM_RESULT),
        ("B", 1, STREAM_MAIN): FakeStream()})
    assert stream.probe(object(), "B", 1, meta=META) == "online"
    assert [c[2] for c in calls] == [STREAM_SUB, STREAM_MAIN]


def test_camera_without_substream_is_not_offline(monkeypatch):
    """Batareyali kamerada substream yo'q — jim qoladi, asosiy oqim esa ishlaydi."""
    calls = _patch(monkeypatch, {("B", 1, STREAM_SUB): FakeStream(fail=SILENT),
                                 ("B", 1, STREAM_MAIN): FakeStream()})
    assert stream.probe(object(), "B", 1, meta=META) == "online"
    assert [c[2] for c in calls] == [STREAM_SUB, STREAM_MAIN]


def test_online_substream_does_not_touch_main(monkeypatch):
    calls = _patch(monkeypatch, {("A", 1): FakeStream()})
    assert stream.probe(object(), "A", 1, meta=META) == "online"
    assert [c[2] for c in calls] == [STREAM_SUB]


def test_probe_many_fetches_metadata_once(monkeypatch):
    _patch(monkeypatch, {("A", 1): FakeStream(), ("B", 2): FakeStream(packets=0)})
    fetched = []
    monkeypatch.setattr(stream.vtm_cache, "pagelist", lambda c: fetched.append(1) or META[0])
    res = stream.probe_many(object(), [("A", 1), ("B", 2), ("A", 1)], workers=2)
    assert res == {("A", 1): "online", ("B", 2): "offline"}
    assert fetched == [1], "pagelist har kanal uchun qayta so'ralmasligi kerak"


def test_probe_many_cloud_error_gives_unknown_not_traceback(monkeypatch):
    _patch(monkeypatch, {})

    def boom(c):
        raise RuntimeError("cloud down")

    monkeypatch.setattr(stream.vtm_cache, "pagelist", boom)
    assert stream.probe_many(object(), [("A", 1)]) == {("A", 1): "unknown"}


def test_classify():
    live = {("E", 1): "online", ("E", 2): "offline", ("K", 1): "online",
            ("P", 1): "online", ("U", 1): "unknown"}
    got = stream.classify(live, {"E", "K"}, {"K": "ABCDEF", "E": "AUTO"})
    assert got == {("E", 1): "nokey", ("E", 2): "offline", ("K", 1): "ok",
                   ("P", 1): "ok", ("U", 1): "unknown"}
    # holat olinmadi -> shifr noma'lum -> "ok" deb aldamaymiz
    assert stream.classify(live, None, {})[("P", 1)] == "unknown"
