"""Parent <-> per-camera child process contract.

stream_manager starts one decrypting proxy per camera. They talk through
environment variables (the key never appears on the command line) and flag
files (wrong code / camera offline). Both sides import the names from HERE,
so the two can never drift apart again.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading




def keyerror_flag_path(serial: str, channel: int = 1) -> str:
    """Kod xato bo'lganda stream_manager bilan aloqa uchun bayroq fayl yo'li."""
    return os.path.join(tempfile.gettempdir(), f"ezviz_keyerr_{serial}_{channel}.flag")


def offline_flag_path(serial: str, channel: int = 1) -> str:
    """Kamera offline (VTM ma'lumot bermayapti) bo'lganda bayroq fayl yo'li."""
    return os.path.join(tempfile.gettempdir(), f"ezviz_offline_{serial}_{channel}.flag")



KEY_ENV = "CLOUDCAM_CAM_KEY"   # stream_manager.KEY_ENV bilan bir xil
PARENT_WATCH_ENV = "CLOUDCAM_PARENT_WATCH"   # stream_manager.PARENT_WATCH_ENV bilan bir xil


def watch_parent(stream=None, on_exit=None) -> threading.Thread:
    """Ota jarayon o'lsa proxy ham chiqadi.

    stream_manager proxy'ni stdin=PIPE bilan ochadi va unga hech narsa yozmaydi.
    Ota jarayon har qanday sabab bilan (hatto majburan o'ldirilsa ham) tugasa,
    OS quvurni yopadi -> read() EOF qaytaradi. Busiz yetim proxy portni band
    qilib qolardi va keyingi ishga tushirishda shu port ishlamasdi."""
    stream = stream if stream is not None else sys.stdin.buffer
    on_exit = on_exit or (lambda: os._exit(0))

    def run():
        try:
            while stream.read(4096):
                pass
        except Exception:
            pass
        on_exit()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th
