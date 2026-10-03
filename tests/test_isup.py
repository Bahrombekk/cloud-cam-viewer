"""ISUP 5.0 — qurilma bulutga emas, bizga ulanadi.

Bu yerda HAQIQIY HTTP server ko'tariladi (soxta `isup-bridge`) va klient u
bilan tarmoq orqali gaplashadi — ya'ni so'rov formati, sarlavhalar va javob
tahlili ham tekshiriladi, faqat mock emas.

Ko'prikning o'zi C++ (Hikvision SDK yopiq), shuning uchun kutubxona tomonidan
qo'riqlanadigan narsa — KONTRAKT: `/want` va `/keys` qanday matn kutadi,
`/devices` javobi qanday o'qiladi, va token qayerga qo'yiladi.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cloudcam.sources.isup import IsupBridge, IsupError, IsupUnavailable
from cloudcam.settings import Settings

DEVICES = {
    "want_set": True, "keys_set": True, "default_key": False,
    "rejected": [{"id": "BAD1", "reason": "kalit mos emas", "count": 3, "age": 12}],
    "devices": [{
        "id": "AA1234567", "valid": True, "serial": "DS-TCG406-E",
        "ip": "10.0.0.8", "firmware": "V5.4.0", "protocol": "ISUP5",
        "key_source": "dev", "info": "", "dev_type": 1,
        "channels_total": 2, "channels_analog": 0, "start_channel": 1,
        "ip_channels": [], "online_seconds": 93,
        "streams": [{"channel": 1, "type": 0, "path": "isup/AA1234567/1",
                     "live": True, "bytes": 2500000, "age": 0.2},
                    {"channel": 2, "type": 1, "path": "isup/AA1234567/2/sub",
                     "live": False, "bytes": 0, "age": -1.0}],
    }],
}


class _Handler(BaseHTTPRequestHandler):
    received = []          # (method, path, body, token)
    token = None

    def log_message(self, *a):
        pass

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n).decode() if n else ""

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        if _Handler.token is None:
            return True
        return self.headers.get("X-Bridge-Token") == _Handler.token

    def do_GET(self):
        if not self._auth_ok():
            return self._reply(401, {"error": "token"})
        _Handler.received.append(("GET", self.path, "", self.headers.get("X-Bridge-Token")))
        if self.path == "/health":
            return self._reply(200, {"ok": True})
        if self.path == "/devices":
            return self._reply(200, DEVICES)
        self._reply(404, {"error": "not found"})

    def do_PUT(self):
        body = self._read()
        if not self._auth_ok():
            return self._reply(401, {"error": "token"})
        _Handler.received.append(("PUT", self.path, body, self.headers.get("X-Bridge-Token")))
        if self.path in ("/want", "/keys"):
            return self._reply(200, {"ok": True, "bad": 0, "kicked": 0})
        self._reply(404, {"error": "not found"})

    do_POST = do_PUT


@pytest.fixture
def bridge():
    _Handler.received = []
    _Handler.token = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield IsupBridge(url, rtsp_base="rtsp://127.0.0.1:8554"), _Handler
    srv.shutdown()
    srv.server_close()


# ── holat o'qish ─────────────────────────────────────────────────────

def test_health(bridge):
    b, _ = bridge
    assert b.health() is True


def test_devices_are_parsed(bridge):
    b, _ = bridge
    (dev,) = b.devices()
    assert dev.id == "AA1234567" and dev.ip == "10.0.0.8"
    assert dev.firmware == "V5.4.0" and dev.channels_total == 2
    assert dev.online_seconds == 93 and dev.key_source == "dev"


def test_channels_follow_the_start_channel(bridge):
    """Qurilma kanallarni 1 dan emas, o'z boshlang'ich raqamidan sanashi mumkin."""
    b, _ = bridge
    (dev,) = b.devices()
    assert dev.channels == [1, 2]


def test_stream_type_1_is_the_substream(bridge):
    """`type` 0/1 ni `sub` bayrog'iga aylantirish — kontraktning nozik joyi."""
    b, _ = bridge
    (dev,) = b.devices()
    main, sub = dev.streams
    assert main.sub is False and main.live is True and main.channel == 1
    assert sub.sub is True and sub.live is False


def test_rejected_devices_are_surfaced(bridge):
    """Kaliti mos kelmagan qurilma JIM yo'qolmasligi kerak — u diagnostika."""
    b, _ = bridge
    st = b.status()
    assert st.rejected and st.rejected[0]["reason"] == "kalit mos emas"


# ── oqim manzili ─────────────────────────────────────────────────────

def test_stream_urls(bridge):
    b, _ = bridge
    assert b.stream_url("AA1", 3) == "rtsp://127.0.0.1:8554/isup/AA1/3"
    assert b.stream_url("AA1", 3, sub=True) == "rtsp://127.0.0.1:8554/isup/AA1/3/sub"


def test_rtsp_base_trailing_slash_does_not_double_up():
    b = IsupBridge("http://x/", rtsp_base="rtsp://h:8554/")
    assert b.stream_url("A", 1) == "rtsp://h:8554/isup/A/1"
    assert b.url == "http://x"


# ── "want" ro'yxati ──────────────────────────────────────────────────

def test_want_sends_one_line_per_stream(bridge):
    b, h = bridge
    b.want(("AA1", 1, False), ("AA1", 2, True))
    _m, path, body, _t = h.received[-1]
    assert path == "/want"
    assert sorted(body.strip().splitlines()) == ["AA1 1 0", "AA1 2 1"]


def test_want_accumulates_until_replace_is_asked(bridge):
    """Ikkinchi kamera ochilganda birinchisi yopilib qolmasligi kerak."""
    b, h = bridge
    b.want(("AA1", 1, False))
    b.want(("AA2", 1, False))
    body = h.received[-1][2]
    assert sorted(body.strip().splitlines()) == ["AA1 1 0", "AA2 1 0"]
    b.want(("AA3", 1, False), replace=True)
    assert h.received[-1][2].strip() == "AA3 1 0"


def test_unwant_closes_only_that_stream(bridge):
    b, h = bridge
    b.want(("AA1", 1, False), ("AA2", 1, False))
    b.unwant(("AA1", 1))
    assert h.received[-1][2].strip() == "AA2 1 0"


def test_empty_want_list_is_still_sent(bridge):
    """Oxirgi kamera yopilganda ko'prik HAMMASINI yopishi kerak — bo'sh
    ro'yxat ham yuboriladi, aks holda oqim 24/7 ochiq qolardi."""
    b, h = bridge
    b.want(("AA1", 1, False))
    b.unwant(("AA1", 1))
    assert h.received[-1][1] == "/want"
    assert h.received[-1][2].strip() == ""


# ── kalitlar ─────────────────────────────────────────────────────────

def test_keys_body_format(bridge):
    b, h = bridge
    b.set_keys({"AA1": "KEY1", "AA2": "KEY2"}, blocked=["BAD1"],
               prefixes={"AA": "PFX"}, default=False)
    body = h.received[-1][2].strip().splitlines()
    assert body == ["dev AA1 KEY1", "dev AA2 KEY2", "block BAD1",
                    "prefix AA PFX", "default off"]


def test_keys_default_on_is_explicit(bridge):
    """`default` qatori DOIM yuboriladi — ko'prikda eski holat qolib ketmasin."""
    b, h = bridge
    b.set_keys({"AA1": "K"})
    assert h.received[-1][2].strip().splitlines()[-1] == "default on"


def test_bridge_reports_unparsed_key_lines(bridge, monkeypatch):
    b, h = bridge

    def _bad(*a, **k):
        return {"ok": True, "bad": 2}

    monkeypatch.setattr(b, "_call", _bad)
    with pytest.raises(IsupError, match="2 ta kalit"):
        b.set_keys({"AA1": "K"})


# ── xatolar ──────────────────────────────────────────────────────────

def test_token_is_sent_and_enforced():
    _Handler.received = []
    _Handler.token = "sekret"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        ok = IsupBridge(url, token="sekret")
        assert ok.health() is True
        bad = IsupBridge(url, token="xato")
        with pytest.raises(IsupError, match="token"):
            bad.status()
    finally:
        _Handler.token = None
        srv.shutdown()
        srv.server_close()


def test_a_missing_bridge_says_so_clearly():
    """Ko'prik ixtiyoriy tashqi bog'liqlik — yo'qligi tushunarli bo'lsin."""
    b = IsupBridge("http://127.0.0.1:1", timeout=0.3)
    with pytest.raises(IsupUnavailable, match="javob bermadi"):
        b.devices()
    assert b.health() is False, "health() xato ko'tarmasligi kerak"


def test_settings_carry_the_bridge_address():
    from cloudcam.sources import isup as _isup
    s = Settings(isup_url="http://h:9/", isup_token="t",
                 isup_rtsp_base="rtsp://h:8554")
    b = _isup.from_settings(s)
    assert b.url == "http://h:9"
    assert b._s.headers["X-Bridge-Token"] == "t"


def test_the_bridge_token_never_reaches_child_processes():
    """Umumiy sir muhit o'zgaruvchisida jarayonlar ro'yxatidan ko'rinadi."""
    env = Settings(isup_token="juda-maxfiy").to_env()
    assert "juda-maxfiy" not in " ".join(env.values())
    assert "CLOUDCAM_ISUP_TOKEN" not in env


# ── oqimga ulanish (bulut proxy'siz) ─────────────────────────────────

def test_ffmpeg_reads_the_rtsp_url_over_tcp():
    """UDP'da paket yo'qolishi tasvirni buzadi va NAT ortidan ko'pincha
    umuman o'tmaydi — RTSP uchun TCP majburan."""
    from cloudcam.stream_manager import CameraStream
    cs = CameraStream("AA1", 1, 8700, url="rtsp://h:8554/isup/AA1/1")
    cmd = cs._ffmpeg_cmd()
    assert "rtsp://h:8554/isup/AA1/1" in cmd
    i = cmd.index("-rtsp_transport")
    assert cmd[i + 1] == "tcp"


def test_cloud_cameras_are_unaffected_by_the_rtsp_flag():
    from cloudcam.stream_manager import CameraStream
    cmd = CameraStream("GK1", 1, 8700)._ffmpeg_cmd()
    assert "-rtsp_transport" not in cmd
    assert "http://127.0.0.1:8700/GK1.ts" in cmd


def test_an_isup_stream_never_starts_the_decrypt_proxy(monkeypatch):
    """ISUP oqimi allaqachon toza — deshifr proxy'si ishga tushsa, u bekorga
    bulutga ulanib, portni band qilib o'tirardi."""
    from cloudcam import stream_manager as sm

    cs = sm.CameraStream("AA1", 1, 8700, url="rtsp://h:8554/isup/AA1/1")
    started = []
    monkeypatch.setattr(cs, "_start_proxy", lambda: started.append(1))
    monkeypatch.setattr(cs, "_wait_port", lambda *a, **k: pytest.fail(
        "ISUP oqimida lokal port kutilmasligi kerak"))

    class _Proc:
        def __init__(self, *a, **k):
            self.stdout = self
            cs.running = False          # bitta urinishdan keyin to'xtaymiz

        def read(self, _n):
            return b""

        def terminate(self):
            pass

    monkeypatch.setattr(sm.subprocess, "Popen", _Proc)
    cs.running = True
    cs._reader_loop()
    assert started == [], "ISUP oqimi uchun proxy ishga tushdi"
