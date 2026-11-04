"""Bola jarayon ota jarayon bilan birga to'xtasin — ota MAJBURAN o'ldirilsa ham.

`cloudcam serve` MediaMTX'ni ishga tushiradi, MediaMTX esa har kamera uchun
`cloudcam stream` (proxy + ffmpeg) ni. Ota Ctrl+C bilan to'xtasa hammasi tartib bilan
yopiladi; lekin vazifalar menejeri / `kill -9` / `Stop-Process -Force` dan keyin
MediaMTX va uning oqimlari portlarni va bulut sessiyalarini band qilib qolardi.

  * Windows: Job object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`). Ota o'lsa OS job
    handle'ini yopadi va job'dagi HAMMA jarayonni (bolaning bolalari ham — ular job'ni
    meros oladi) o'ldiradi.
  * Linux: `prctl(PR_SET_PDEATHSIG, SIGTERM)` — ota o'lsa kernel bolaga SIGTERM
    yuboradi; MediaMTX SIGTERM'da on-demand buyruqlarini o'zi yopadi.
  * macOS: bunday mexanizm yo'q — eski xatti-harakat (hujjatda cheklov).

Hammasi "best effort": o'rnatib bo'lmasa jarayon baribir ishga tushadi.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

_JOBS: list = []          # job handle'lari jarayon umri davomida ochiq tursin


def _linux_pdeathsig() -> None:                    # preexec_fn: bola ichida ishlaydi
    import ctypes
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGTERM)              # PR_SET_PDEATHSIG = 1
    except OSError:
        pass


def _windows_kill_on_close_job(pid: int) -> None:
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):           # noqa: N801 — Win32 nomlari
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000          # KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        err = ctypes.get_last_error()
        k32.CloseHandle(job)
        raise ctypes.WinError(err)
    proc = k32.OpenProcess(0x0100 | 0x0001, False, pid)     # SET_QUOTA | TERMINATE
    if not proc:
        err = ctypes.get_last_error()
        k32.CloseHandle(job)
        raise ctypes.WinError(err)
    try:
        if not k32.AssignProcessToJobObject(job, proc):
            err = ctypes.get_last_error()
            k32.CloseHandle(job)
            raise ctypes.WinError(err)
    finally:
        k32.CloseHandle(proc)
    _JOBS.append(job)                       # yopilmaydi: ota o'lganda OS o'zi yopadi


def popen_bound(args, **kwargs) -> subprocess.Popen:
    """`subprocess.Popen`, lekin bola (va Windows'da uning bolalari) ota bilan birga o'ladi."""
    if sys.platform.startswith("linux") and "preexec_fn" not in kwargs:
        kwargs["preexec_fn"] = _linux_pdeathsig
    proc = subprocess.Popen(args, **kwargs)
    if os.name == "nt":
        try:
            _windows_kill_on_close_job(proc.pid)
        except OSError:
            pass                            # best effort: masalan job'ga ruxsat yo'q
    return proc
