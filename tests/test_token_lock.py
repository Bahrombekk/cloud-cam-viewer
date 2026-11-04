"""`token.json` ni bir nechta jarayon bo'lishganda sessiya yangilash poygasi.

Refresh tokeni HAR yangilanishda almashadi. `cloudcam serve` o'nlab `cloudcam stream`
jarayonini birdan ochadi: qulfsiz ular bitta eskirgan refresh tokenni bir vaqtda
yuborar, bulut birinchisidan boshqasini rad etar va ular to'liq loginga (CAPTCHA /
"yangi qurilma" 2FA) tushardi.
"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cloudcam.core.fsutil import file_lock
from cloudcam.sources.cloud.client import CloudClient, SessionExpired

SRC = Path(__file__).resolve().parents[1] / "src"


# ---- qulfning o'zi -----------------------------------------------------------

_WORKER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from cloudcam.core.fsutil import file_lock
path = sys.argv[2]
for _ in range(int(sys.argv[3])):
    with file_lock(path):
        n = int(open(path).read() or 0)
        time.sleep(0.002)                 # o'qish va yozish orasida oyna — poygani kuchaytiradi
        open(path, "w").write(str(n + 1))
"""


def test_lock_serialises_separate_processes(tmp_path):
    counter = tmp_path / "counter.txt"
    counter.write_text("0")
    script = tmp_path / "worker.py"
    script.write_text(_WORKER, encoding="utf-8")
    procs = [subprocess.Popen([sys.executable, str(script), str(SRC), str(counter), "25"])
             for _ in range(4)]
    for p in procs:
        assert p.wait(timeout=120) == 0
    assert int(counter.read_text()) == 100, "yangilanish yo'qoldi — qulf jarayonlararo ishlamadi"


def test_lock_is_reentrant_in_one_thread(tmp_path):
    p = str(tmp_path / "token.json")
    with file_lock(p):
        with file_lock(p):               # ichma-ich chaqiruv o'zini bloklamasligi kerak
            pass


def test_lock_times_out_while_another_thread_holds_it(tmp_path):
    p = str(tmp_path / "token.json")
    entered, release = threading.Event(), threading.Event()

    def holder():
        with file_lock(p):
            entered.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    assert entered.wait(5)
    try:
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            with file_lock(p, timeout=0.3):
                pass
        assert time.monotonic() - t0 < 3
    finally:
        release.set()
        t.join(5)
    with file_lock(p, timeout=1):         # bo'shagandan keyin olinadi
        pass


# ---- sessiya yangilash poygasi -------------------------------------------------

class _Cloud:
    """Soxta bulut: refresh tokenni HAR safar almashtiradi, eskisini rad etadi."""

    def __init__(self):
        self.rf = "RF0"
        self.calls = 0
        self.rejected = 0
        self.mu = threading.Lock()

    def refresh(self, client):
        with self.mu:
            self.calls += 1
            sent = client.rf_session_id
        time.sleep(0.05)                  # bulutga borib kelish — poyga oynasi
        with self.mu:
            if sent != self.rf:
                self.rejected += 1
                raise SessionExpired("refresh token rejected")
            n = int(self.rf[2:]) + 1
            self.rf = f"RF{n}"
            client.session_id, client.rf_session_id = f"S{n}", self.rf
            client.session_time = time.time()
        return True


def _write_token(path, session_time):
    c = CloudClient("a@b.c", "pw")
    c.session_id, c.rf_session_id, c.session_time = "S0", "RF0", session_time
    c.save_token(str(path))


def test_concurrent_401s_refresh_once_and_nobody_is_rejected(tmp_path, monkeypatch):
    tok = tmp_path / "token.json"
    _write_token(tok, time.time() - 10)
    cloud = _Cloud()
    monkeypatch.setattr(CloudClient, "refresh_session",
                        lambda self, allow_login=True: cloud.refresh(self))
    clients = []
    for _ in range(8):
        c = CloudClient("a@b.c", "pw")
        assert c.load_token(str(tok))
        clients.append(c)
    results, errors = [], []
    start = threading.Barrier(len(clients))

    def go(c):
        start.wait()
        try:
            results.append(c.refresh_and_save(str(tok)))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go, args=(c,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert cloud.rejected == 0, "eskirgan refresh token yuborildi"
    assert cloud.calls == 1, "bitta jarayon yangilashi, qolganlari natijani olishi kerak"
    assert sorted(results) == ["adopted"] * 7 + ["refreshed"]
    assert all(c.rf_session_id == cloud.rf for c in clients)
    assert json.loads(tok.read_text(encoding="utf-8"))["rf_session_id"] == cloud.rf


def test_concurrent_startup_with_a_stale_session_refreshes_once(tmp_path, monkeypatch):
    """`cloudcam serve` ostida o'nlab `cloudcam stream` bir vaqtda ishga tushadi."""
    tok = tmp_path / "token.json"
    _write_token(tok, time.time() - 10_000)         # eskirgan: SESSION_MAX_AGE dan katta
    cloud = _Cloud()
    monkeypatch.setattr(CloudClient, "refresh_session",
                        lambda self, allow_login=True: cloud.refresh(self))
    monkeypatch.setattr(CloudClient, "login",
                        lambda self: pytest.fail("to'liq login bo'lmasligi kerak"))
    modes, errors = [], []
    start = threading.Barrier(6)

    def go():
        start.wait()
        try:
            modes.append(CloudClient("a@b.c", "pw").connect(str(tok)))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert cloud.calls == 1 and cloud.rejected == 0
    assert sorted(modes) == ["refreshed"] + ["resumed"] * 5
