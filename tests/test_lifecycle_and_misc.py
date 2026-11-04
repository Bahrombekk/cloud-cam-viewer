"""Proxy jarayon hayoti, substream sozlamasi, qurilmalar sahifalash,
kod registri va inter-namuna filtri."""
import io
import itertools
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from Crypto.Cipher import AES

from cloudcam.core import ipc

from cloudcam import stream_manager
from cloudcam.sources.cloud.client import CloudClient
from cloudcam.codec.rtp import HevcRtpDecryptor
from cloudcam.settings import Settings

# ---- 1. jarayon hayoti ------------------------------------------------------

def _sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)


def test_stop_proc_waits_until_process_is_gone():
    p = _sleeper()
    stream_manager._stop_proc(p, timeout=5)
    assert p.poll() is not None          # terminate() + wait() — zombie/band port yo'q
    assert p.stdin.closed and p.stdout.closed


def test_stop_proc_tolerates_none_and_finished():
    stream_manager._stop_proc(None)
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    stream_manager._stop_proc(p)


def test_proxy_exits_when_parent_pipe_closes():
    """Ota jarayon o'lsa OS stdin quvurini yopadi -> proxy o'zi chiqadi."""
    code = ("import time, os; os.environ['CLOUDCAM_PARENT_WATCH']='1';"
            "from cloudcam.core.ipc import watch_parent; watch_parent();"
            "time.sleep(60)")
    p = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                         cwd=str(stream_manager.os.path.dirname(
                             stream_manager.os.path.dirname(stream_manager.__file__))))
    p.stdin.close()                      # = ota jarayon o'ldi
    assert p.wait(timeout=30) == 0


def test_watch_parent_calls_exit_on_eof():
    called = []
    th = ipc.watch_parent(io.BytesIO(b"x" * 10), on_exit=lambda: called.append(1))
    th.join(timeout=5)
    assert called == [1]


def test_proxy_is_started_with_parent_watch(monkeypatch):
    seen = {}
    monkeypatch.setattr(stream_manager.subprocess, "Popen",
                        lambda cmd, **kw: seen.update(kw) or None)
    cs = stream_manager.CameraStream("SER1", 1, 8799, decrypt=True, key="ABCDEF")
    cs._start_proxy()
    assert seen["stdin"] is subprocess.PIPE
    assert seen["env"][stream_manager.PARENT_WATCH_ENV] == "1"
    assert ipc.PARENT_WATCH_ENV == stream_manager.PARENT_WATCH_ENV


def test_proxy_started_after_stop_is_killed(monkeypatch):
    """stop() _start_proxy() bilan poyga qilsa ham proxy yetim qolmaydi."""
    p = _sleeper()
    monkeypatch.setattr(stream_manager.subprocess, "Popen", lambda cmd, **kw: p)
    cs = stream_manager.CameraStream("SER1", 1, 8799, decrypt=True, key="ABCDEF")
    cs.running = False
    cs._start_proxy()
    assert p.poll() is not None and cs.proxy_proc is None


# ---- 4. substream sozlamasi -------------------------------------------------

def test_substream_from_env(monkeypatch):
    monkeypatch.setenv("CLOUDCAM_SUBSTREAM", "yes")
    assert Settings.from_env(Settings()).substream is True
    monkeypatch.setenv("CLOUDCAM_SUBSTREAM", "0")
    assert Settings.from_env(Settings(substream=True)).substream is False


def test_substream_from_config_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "config", types.SimpleNamespace(SUBSTREAM=True))
    assert Settings.from_config_module().substream is True


# ---- 5. qurilmalar sahifalash -----------------------------------------------

class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def test_get_devices_follows_pages():
    c = CloudClient("e", "p")
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(params["offset"])
        start = params["offset"]
        n = 50 if start < 100 else 7
        devs = [{"deviceSerial": f"S{start + i}"} for i in range(n)]
        return _Resp({"deviceInfos": devs, "page": {"hasNext": start + n < 107}})

    c.session.get = fake_get
    devs = c.get_devices()
    assert len(devs) == 107 and calls == [0, 50, 100]


# ---- 6. kod registri ---------------------------------------------------------

def _check_code(monkeypatch, correct):
    sys.modules.setdefault("config", types.ModuleType("config"))
    import check_code
    tried = []

    def fake_check(client, serial, code, channel=1):
        tried.append(code)
        return "correct" if code == correct else "wrong"

    monkeypatch.setattr(check_code, "check", fake_check)
    return check_code, tried


def test_lowercase_input_falls_back_to_uppercase(monkeypatch):
    cc, tried = _check_code(monkeypatch, correct="ABCDEF")
    assert cc.check_with_case_fallback(None, "S", "abcdef") == ("ABCDEF", "correct")
    assert tried == ["abcdef", "ABCDEF"]


def test_lowercase_custom_code_is_kept_as_is(monkeypatch):
    cc, tried = _check_code(monkeypatch, correct="mycode")
    assert cc.check_with_case_fallback(None, "S", "mycode") == ("mycode", "correct")
    assert tried == ["mycode"]


# ---- 7. inter-namuna faqat slice'lardan --------------------------------------

_K = b"ABCDEF".ljust(16, b"\0")
_seq = itertools.count()


def _rtp(payload):
    s = next(_seq) & 0xFFFF
    return bytes([0x80, 96, s >> 8, s & 0xFF]) + b"\x00" * 8 + payload


def _body(first, i):
    return bytes([first]) + bytes((i * 7 + k) % 200 + 20 for k in range(34))


def _enc(b):
    return AES.new(_K, AES.MODE_ECB).encrypt(b[:32]) + b[32:]


def test_clear_sei_does_not_outvote_encrypted_slices():
    """Toza SEI (1-bayti doim bir xil) namunaga kirsa, shifrli P-slice'lar bilan
    durang chiqib (4+4) qaror noto'g'ri TOZA bo'lardi."""
    dec = HevcRtpDecryptor("ABCDEF")
    dec.feed(_rtp(b"\x40\x01" + _enc(_body(0x0C, 0))))       # VPS, variant A shifrli
    assert dec._decrypt is True and dec._clear == 2
    for i in range(4):
        dec.feed(_rtp(b"\x4e\x01" + _body(0x05, 50 + i)))    # toza prefix SEI
        dec.feed(_rtp(b"\x02\x01" + _enc(_body(0xAF, i))))   # shifrli TRAIL slice
    for i in range(4, 8):
        dec.feed(_rtp(b"\x02\x01" + _enc(_body(0xAF, i))))
    assert dec._inter_enc is True, dec.inter_note


def test_ffmpeg_does_not_read_the_console():
    """N ta ffmpeg konsol tugmalarini o'qimasin ('q' ffmpeg'ni to'xtatadi)."""
    from cloudcam import stream_manager as sm
    text = Path(sm.__file__).read_text(encoding="utf-8")
    assert '"-nostdin"' in text and "stdin=subprocess.DEVNULL" in text


def test_missing_ffmpeg_is_reported_not_swallowed():
    from cloudcam import stream_manager as sm
    text = Path(sm.__file__).read_text(encoding="utf-8")
    assert "except FileNotFoundError" in text


def test_proxy_port_is_not_shared_on_windows():
    """Windows'da SO_REUSEADDR band portga ikkinchi bog'lanishga ruxsat beradi."""
    import os

    import pytest

    from cloudcam.sources.cloud.proxy import _Server
    assert _Server.allow_reuse_address == (os.name != "nt")
    if os.name != "nt":
        return
    from http.server import BaseHTTPRequestHandler
    a = _Server(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        port = a.server_address[1]
        with pytest.raises(OSError):
            _Server(("127.0.0.1", port), BaseHTTPRequestHandler).server_close()
    finally:
        a.server_close()


def test_two_accounts_in_one_process_keep_their_own_settings(monkeypatch):
    """Ikkinchi `CloudCam`/`StreamManager` global faol sozlamani almashtiradi.
    Birinchi hisob kamerasi qayta ulanganda proxy IKKINCHI hisobning token
    fayli bilan ishga tushardi."""
    from cloudcam.settings import Settings, get_active, set_active

    seen = []
    monkeypatch.setattr(stream_manager.subprocess, "Popen",
                        lambda cmd, **kw: seen.append(kw["env"]) or None)
    prev = get_active()
    try:
        a = stream_manager.StreamManager(settings=Settings(token_file="a.json",
                                                           platform="ezviz"))
        cs = stream_manager.CameraStream("SER1", 1, 8799, settings=a.settings)
        stream_manager.StreamManager(settings=Settings(token_file="b.json"))
        assert get_active().token_file == "b.json"
        cs.running = True
        cs._start_proxy()                     # = qayta ulanish
        env = seen[-1]
        assert env["CLOUDCAM_TOKEN_FILE"].endswith("a.json")
        assert env["CLOUDCAM_PLATFORM"] == "ezviz"
    finally:
        set_active(prev)


def _listener():
    """Portni band qilib turgan "begona" tinglovchi."""
    import socket as _s
    srv = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    return srv, srv.getsockname()[1]


def test_busy_port_is_not_shared_with_a_foreign_proxy(monkeypatch):
    """Port boshqa jarayonda band bo'lsa, proxy BOSHQA portda ochiladi —
    ffmpeg begona kameraning oqimini o'qimaydi."""
    srv, busy = _listener()
    try:
        seen = []
        monkeypatch.setattr(stream_manager.subprocess, "Popen",
                            lambda cmd, **kw: seen.append(cmd) or None)
        cs = stream_manager.CameraStream("SER1", 1, busy)
        cs.running = True
        cs._start_proxy()
        assert cs.port != busy
        assert str(cs.port) in seen[-1] and str(busy) not in seen[-1]
        assert str(cs.port) in cs._ffmpeg_cmd()[cs._ffmpeg_cmd().index("-i") + 1]
    finally:
        srv.close()


def test_wait_port_ignores_a_listener_when_our_proxy_died():
    """O'z proxy'miz yiqilgan (port band edi) — portda kimdir tinglasa ham
    "tayyor" demaymiz."""
    srv, busy = _listener()
    try:
        cs = stream_manager.CameraStream("SER1", 1, busy)
        cs.running = True
        cs.proxy_proc = subprocess.Popen([sys.executable, "-c", "pass"])
        cs.proxy_proc.wait()
        assert cs._wait_port(timeout=2) is False
    finally:
        srv.close()


def test_proxy_only_answers_its_own_local_host_header():
    """DNS rebinding: begona sahifa `Host: evil.example:<port>` bilan keladi."""
    from cloudcam.sources.cloud.proxy import host_allowed
    assert host_allowed("127.0.0.1:8700", 8700)
    assert host_allowed("localhost:8700", 8700)
    for bad in ("evil.example:8700", "127.0.0.1:8701", "127.0.0.1", "", None):
        assert not host_allowed(bad, 8700), bad


def test_flag_files_stay_inside_the_private_flag_dir():
    from cloudcam.core import ipc
    d = os.path.realpath(ipc.flag_dir())
    for p in (ipc.keyerror_flag_path("../../etc/x", 1),
              ipc.offline_flag_path("SER1/..", "1/../2")):
        assert os.path.dirname(os.path.realpath(p)) == d, p


@pytest.mark.skipif(os.name != "posix", reason="POSIX ruxsatlari")
def test_flag_dir_is_private_on_posix():
    import stat as _stat

    from cloudcam.core import ipc
    st = os.stat(ipc.flag_dir())
    assert st.st_uid == os.getuid()
    assert _stat.S_IMODE(st.st_mode) & 0o077 == 0
