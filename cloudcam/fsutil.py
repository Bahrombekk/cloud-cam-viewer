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

import os
import tempfile


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
