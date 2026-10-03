"""Proxy jarayon hayoti, substream sozlamasi, qurilmalar sahifalash,
kod registri va inter-namuna filtri."""
import io
import itertools
import subprocess
import sys
import types

from Crypto.Cipher import AES

from cloudcam import decrypt_proxy, stream_manager
from cloudcam.client import CloudClient
from cloudcam.decrypt_proxy import HevcRtpDecryptor
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
            "from cloudcam.decrypt_proxy import watch_parent; watch_parent();"
            "time.sleep(60)")
    p = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                         cwd=str(stream_manager.os.path.dirname(
                             stream_manager.os.path.dirname(stream_manager.__file__))))
    p.stdin.close()                      # = ota jarayon o'ldi
    assert p.wait(timeout=30) == 0


def test_watch_parent_calls_exit_on_eof():
    called = []
    th = decrypt_proxy.watch_parent(io.BytesIO(b"x" * 10), on_exit=lambda: called.append(1))
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
    assert decrypt_proxy.PARENT_WATCH_ENV == stream_manager.PARENT_WATCH_ENV


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
