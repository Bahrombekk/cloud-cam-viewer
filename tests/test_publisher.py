"""`cloudcam stream` / `CloudCam.publish()` — kamerani RTSP serverga transkodsiz chiqarish.

Eng nozik joy vaqt belgilari: proxy xom Annex-B beradi, unda PTS yo'q. ffmpeg uni
25 fps deb hisoblasa, 9-15 fps li bulut kamerasi RTSP o'yinchida tezlashib yoki
orqada qolib ketadi — shuning uchun kelish vaqti bo'yicha belgilanadi.
"""
import http.server
import io
import shutil
import socket
import subprocess
import threading
import time

import pytest

from cloudcam import cli
from cloudcam.publisher import RtspPublisher, check_target, redact_url

TARGET = "rtsp://127.0.0.1:8554/cam/SER1_1"


def _pub(**kw):
    return RtspPublisher("SER1", 1, 8799, kw.pop("target", TARGET), **kw)


# ---- ffmpeg buyrug'i ---------------------------------------------------------

def test_proxy_source_is_copied_with_wallclock_timestamps():
    cmd = _pub()._ffmpeg_cmd()
    i = cmd.index("-i")
    assert cmd[i + 1] == "http://127.0.0.1:8799/SER1.ts"
    assert "-use_wallclock_as_timestamps" in cmd[:i], "xom Annex-B'da PTS yo'q"
    assert cmd[cmd.index("-c:v") + 1] == "copy", "transkod bo'lmasligi kerak"
    assert cmd[-1] == TARGET
    out = cmd[cmd.index("-f", i):]
    assert out[1] == "rtsp" and out[out.index("-rtsp_transport") + 1] == "tcp"
    assert "-an" in cmd


@pytest.mark.parametrize("major, expect", [(8, True), (5, True), (4, False), (None, False)])
def test_connect_timeout_only_where_ffmpeg_understands_it(monkeypatch, major, expect):
    """Server javob bermasa ffmpeg 30 s+ xatosiz osilardi; `-timeout` bilan ~5 s da
    xato beradi. ffmpeg < 5 da RTSP `-timeout` tinglash rejimini yoqadi — qo'yilmaydi."""
    from cloudcam import publisher
    monkeypatch.setattr(publisher, "ffmpeg_major", lambda: major)
    cmd = _pub()._ffmpeg_cmd()
    assert ("-timeout" in cmd) is expect
    if expect:
        t = cmd.index("-timeout")
        assert cmd[t + 1] == "5000000" and t > cmd.index("-i"), "chiqish opsiyasi bo'lishi kerak"


def test_publisher_never_decodes_or_drops_the_first_idr():
    cmd = _pub()._ffmpeg_cmd()
    joined = " ".join(cmd)
    assert "nobuffer" not in joined
    assert "rawvideo" not in joined and "-vf" not in cmd
    assert "-hwaccel" not in cmd
    for name in ("-analyzeduration", "-probesize"):
        assert int(cmd[cmd.index(name) + 1]) <= 200000


def test_rtsp_source_keeps_its_own_timestamps():
    """ISUP ko'prigidan kelgan RTSP o'z vaqt belgilari bilan keladi."""
    cmd = _pub(url="rtsp://127.0.0.1:8554/isup/AA1/1")._ffmpeg_cmd()
    i = cmd.index("-i")
    assert cmd[i + 1] == "rtsp://127.0.0.1:8554/isup/AA1/1"
    assert "-use_wallclock_as_timestamps" not in cmd
    assert cmd[cmd.index("-rtsp_transport") + 1] == "tcp"


def test_target_must_be_an_rtsp_url():
    assert check_target("rtsps://h:8322/a/b") == "rtsps://h:8322/a/b"
    for bad in ("http://h/a", "rtsp://h", "rtsp://h/", "", "cam/1"):
        with pytest.raises(ValueError):
            check_target(bad)


def test_password_is_hidden_in_output():
    assert redact_url("rtsp://u:secret@h:8554/p") == "rtsp://u:***@h:8554/p"
    assert redact_url("rtsp://h:8554/p") == "rtsp://h:8554/p"
    st = _pub(target="rtsp://u:secret@h:8554/p").status()
    assert "secret" not in str(st)


# ---- progress o'qish ---------------------------------------------------------

class _Proc:
    def __init__(self, out: bytes, err: bytes = b""):
        self.stdout = io.BytesIO(out)
        self.stderr = io.BytesIO(err)


def test_progress_frames_mark_the_stream_live():
    p = _pub()
    p.running = True
    out = b"frame=0\nfps=0.0\nprogress=continue\nframe=12\nfps=12.0\nprogress=continue\nframe=30\n"
    assert p._consume(_Proc(out)) == 30
    st = p.status()
    assert st["connected"] is True and p.last_frame_time > 0


def test_ffmpeg_error_becomes_the_stream_error():
    """RTSP server javob bermasa sabab ko'rinsin (ilgari stderr DEVNULL edi)."""
    p = _pub()
    p.running = True
    err = b"[tcp @ 0x1] Connection to tcp://127.0.0.1:8554 failed: Connection refused\n"
    assert p._consume(_Proc(b"frame=0\nprogress=end\n", err)) == 0
    assert p.status()["error"].startswith("ffmpeg: ") and "Connection refused" in p.error


# ---- CLI va API --------------------------------------------------------------

def test_cli_stream_publishes_with_the_given_options(monkeypatch):
    calls = {}

    class FakeStream:
        def status(self):
            raise KeyboardInterrupt          # bir aylanishdan keyin chiqamiz

    class FakeCam:
        def start_token_refresh(self):
            calls["refresh"] = True

        def publish(self, serial, target, channel=1, *, key=None, stream_mode=None):
            calls.update(serial=serial, target=target, channel=channel, key=key,
                         mode=stream_mode)
            return FakeStream()

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(cli, "_cam", lambda args: FakeCam())
    rc = cli.main(["stream", "AB1", "--rtsp", "rtsp://u:pw@h:8554/cam/AB1_2",
                   "--channel", "2", "--stream", "sub", "--key", "ABCDEF"])
    assert rc == 0
    assert calls == {"refresh": True, "serial": "AB1", "target": "rtsp://u:pw@h:8554/cam/AB1_2",
                     "channel": 2, "key": "ABCDEF", "mode": "sub", "closed": True}


def test_cli_stream_rejects_a_bad_target_before_logging_in(monkeypatch):
    monkeypatch.setattr(cli, "_cam", lambda args: pytest.fail("login qilinmasligi kerak"))
    with pytest.raises(SystemExit):
        cli.main(["stream", "AB1", "--rtsp", "http://h/x"])


def test_publish_uses_its_own_stream_mode(monkeypatch, tmp_path):
    from cloudcam import api
    from cloudcam.settings import Settings
    cam = api.CloudCam(settings=Settings(token_file=str(tmp_path / "t.json"),
                                         camkey_file=str(tmp_path / "k.json"),
                                         stream_mode="main"))
    seen = {}
    monkeypatch.setattr(cam._mgr, "publish",
                        lambda serial, ch, target, **kw: seen.update(kw) or object())
    cam.publish("AB1", "rtsp://h:8554/a", stream_mode="sub", key="K", auto_key=False)
    assert seen["settings"].stream_mode == "sub" and seen["key"] == "K"
    assert cam.settings.stream_mode == "main", "umumiy sozlama o'zgarmasligi kerak"
    with pytest.raises(ValueError):
        cam.publish("AB1", "rtsp://h:8554/a", stream_mode="hd", key="K")


# ---- haqiqiy ffmpeg bilan uchidan-uchiga -------------------------------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _has_ffmpeg5_x264():
    """libx264 (manba yasash) va ffmpeg >= 5 (`-listen_timeout` bilan RTSP server)."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return False
    from cloudcam.publisher import ffmpeg_major
    r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return "libx264" in r.stdout and (ffmpeg_major() or 0) >= 5


def _frames(raw: bytes) -> list[bytes]:
    """Annex-B -> kadrlar (x264: kadr = parametr/SEI NAL'lar + bitta slice)."""
    idx = [i for i in range(len(raw) - 3) if raw[i:i + 4] == b"\x00\x00\x00\x01"]
    units, cur = [], b""
    for a, b in zip(idx, idx[1:] + [len(raw)]):
        cur += raw[a:b]
        if raw[a + 4] & 0x1F in (1, 5):
            units.append(cur)
            cur = b""
    return units


FPS = 12


@pytest.mark.skipif(not _has_ffmpeg5_x264(), reason="ffmpeg >= 5 + libx264 + ffprobe kerak")
def test_end_to_end_raw_h264_reaches_an_rtsp_server_with_real_timing(tmp_path):
    """Proxy o'rnida HTTP server xom H.264 (PTS'siz) ni bulut proxy'si kabi KADRMA-KADR,
    12 fps da beradi; publisher uni RTSP serverga (ffmpeg listen rejimi) uzatadi.
    Server tomonda kadrlar soni va VAQT BELGILARI tekshiriladi: ular 25 fps taxminiga
    emas, haqiqiy kelish tezligiga (12 fps) mos bo'lishi kerak."""
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"testsrc=size=320x240:rate={FPS}", "-t", "4", "-c:v", "libx264",
         "-preset", "ultrafast", "-g", "24", "-bsf:v", "h264_mp4toannexb", "-f", "h264", "-"],
        capture_output=True, check=True).stdout
    units = _frames(raw)
    assert len(units) >= 40

    class Src(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            for u in units:
                self.wfile.write(u)
                self.wfile.flush()
                time.sleep(1 / FPS)

    http_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Src)
    threading.Thread(target=http_srv.serve_forever, daemon=True).start()
    src = f"http://127.0.0.1:{http_srv.server_address[1]}/s"

    rport = _free_port()
    out = tmp_path / "got.mkv"
    server = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_flags", "listen",
         "-listen_timeout", "20", "-i", f"rtsp://127.0.0.1:{rport}/cam/T_1",
         "-c", "copy", "-y", str(out)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    time.sleep(1.0)
    pub = RtspPublisher("T", 1, 0, f"rtsp://127.0.0.1:{rport}/cam/T_1", url=src)
    try:
        pub.start()
        deadline = time.time() + 20
        while time.time() < deadline and not pub.status()["connected"]:
            time.sleep(0.2)
        assert pub.status()["connected"], f"publisher jonli bo'lmadi: {pub.status()}"
        server.wait(timeout=20)                # manba tugagach RTSP sessiya yopiladi
    finally:
        pub.stop()
        if server.poll() is None:
            server.kill()
        http_srv.shutdown()
    pts = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "packet=pts_time", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True).stdout.split()
    pts = [float(x) for x in pts if x not in ("", "N/A")]
    assert len(pts) >= len(units) - 4, f"server tomonda kadr kam: {len(pts)} / {len(units)}"
    gaps = sorted(b - a for a, b in zip(pts, pts[1:]))
    median = gaps[len(gaps) // 2]
    assert abs(median - 1 / FPS) < 0.02, f"kadr oralig'i {median:.3f}s, kutilgan {1/FPS:.3f}s"
    span = pts[-1] - pts[0]
    expected = (len(pts) - 1) / FPS
    msg = f"davomiylik {span:.2f}s, kutilgan {expected:.2f}s"
    assert abs(span - expected) / expected < 0.25, msg
