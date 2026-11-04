"""Sir saqlaydigan fayllarni yozish: ATOMAR va faqat egasiga ochiq.

`token.json` (bulut sessiyasi), `cam_keys.json` (tasdiqlash kodlari = AES
kalitlari) va `feature_code` (shu o'rnatmaning bulut terminali) ilgari oddiy
`open(..., "w")` bilan yozilardi — Linux/macOS'da 0644, ya'ni shu kompyuterdagi
har bir foydalanuvchi o'qiy olardi. central-server 2026-11-03 auditida xuddi shu
topilma (tokenlar/snapshotlar 0644) tuzatilgan.

`mkstemp` faylni 0600 bilan yaratadi; `os.replace` uni atomar almashtiradi —
yozish o'rtasida uzilsa eski fayl buzilmaydi. Windows'da POSIX ruxsatlari yo'q
(fayl foydalanuvchi profilida, NTFS ACL bilan) — u yerda faqat atomarlik qoladi.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import time


def write_private(path: str, text: str) -> None:
    """`text` ni `path` ga 0600 huquq bilan atomar yozadi."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def harden(path: str) -> None:
    """Avval ochiq yozilgan fayl huquqini 0600 ga tushiradi (POSIX'da)."""
    if os.name != "posix":
        return
    try:
        if os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        pass


# ---- jarayonlararo qulf ------------------------------------------------------
# `token.json` ni ko'p jarayon bo'lishadi (viewer, CLI, `cloudcam serve` ostidagi
# o'nlab `cloudcam stream`). Refresh tokeni HAR yangilanishda almashadi: ikki jarayon
# bir vaqtda yangilasa, ikkinchisi eskirgan tokenni yuboradi, bulut rad etadi va u
# to'liq loginga (CAPTCHA / "yangi qurilma" 2FA) tushadi. Qulf "o'qi -> yangila ->
# yoz" ni bittalab bajaradi; qulfni olgan jarayon faylni QAYTA o'qiydi va boshqasi
# allaqachon yangilagan bo'lsa o'shani oladi.

_held = threading.local()


def _try_lock(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def file_lock(path: str, timeout: float = 60.0):
    """`path` uchun jarayonlar (va thread'lar) aro eksklyuziv qulf (`<path>.lock`).

    Bitta thread ichida qayta kiriladi: ichma-ich chaqiruv o'zini bloklamaydi.
    `timeout` ichida olinmasa `TimeoutError` (osilib qolgan jarayon hammani
    to'xtatib qo'ymasin). Jarayon o'lsa OS qulfni o'zi bo'shatadi."""
    key = os.path.abspath(path)
    held = getattr(_held, "keys", None)
    if held is None:
        held = _held.keys = {}
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    os.makedirs(os.path.dirname(key) or ".", exist_ok=True)
    fd = os.open(key + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() > deadline:
                raise TimeoutError(f"Could not lock {key} within {timeout:.0f}s")
            time.sleep(0.05)
        held[key] = 1
        try:
            yield
        finally:
            held.pop(key, None)
            _unlock(fd)
    finally:
        os.close(fd)
