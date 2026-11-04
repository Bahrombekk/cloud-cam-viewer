"""Ota majburan o'ldirilsa bola jarayonlar ham to'xtasin (`cloudcam serve` -> MediaMTX).

Ilgari `cloudcam serve` vazifalar menejeri / `kill -9` bilan to'xtatilsa MediaMTX va
uning on-demand `cloudcam stream` jarayonlari ishlab qolib, portlar va bulut
sessiyalarini band qilib turardi.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

# Ota: popen_bound bilan bola ochadi. Bola: oddiy Popen bilan nevara ochadi (MediaMTX ->
# `cloudcam stream` modeli), PID'larni yozib, hammasi uxlaydi.
_PARENT = r"""
import subprocess, sys, time
sys.path.insert(0, sys.argv[1])
from cloudcam.core.procutil import popen_bound
child = popen_bound([sys.executable, "-c", sys.argv[3], sys.argv[2]])
time.sleep(120)
"""
_CHILD = r"""
import os, subprocess, sys, time
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(f"{os.getpid()} {g.pid}")
time.sleep(120)
"""


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x1000, False, pid)        # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        k32.CloseHandle(ctypes.c_void_p(h))
        return code.value == 259                       # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS'da PDEATHSIG / job object yo'q")
def test_children_die_when_the_parent_is_killed_hard(tmp_path):
    pids = tmp_path / "pids.txt"
    script = tmp_path / "parent.py"
    script.write_text(_PARENT, encoding="utf-8")
    parent = subprocess.Popen([sys.executable, str(script), str(SRC), str(pids), _CHILD])
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not (pids.exists() and pids.read_text().strip()):
            time.sleep(0.1)
        child, grandchild = map(int, pids.read_text().split())
        assert _alive(child) and _alive(grandchild)
        parent.kill()                                  # TerminateProcess / SIGKILL
        parent.wait(timeout=10)
        deadline = time.time() + 10
        while time.time() < deadline and _alive(child):
            time.sleep(0.1)
        assert not _alive(child), "bola ota bilan birga to'xtamadi"
        if os.name == "nt":
            # Job object nevarani ham qamraydi (MediaMTX ning on-demand oqimlari).
            while time.time() < deadline and _alive(grandchild):
                time.sleep(0.1)
            assert not _alive(grandchild), "nevara ota bilan birga to'xtamadi"
    finally:
        if parent.poll() is None:
            parent.kill()
        for pid in (locals().get("child"), locals().get("grandchild")):
            if pid and _alive(pid):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass


def test_popen_bound_still_starts_the_process_normally(tmp_path):
    from cloudcam.core.procutil import popen_bound
    p = popen_bound([sys.executable, "-c", "print('ok')"], stdout=subprocess.PIPE, text=True)
    out, _ = p.communicate(timeout=30)
    assert p.returncode == 0 and out.strip() == "ok"
