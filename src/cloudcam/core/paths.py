"""Where cloudcam keeps its per-account files."""

from __future__ import annotations

import os
import tempfile

from cloudcam.settings import data_dir, default_camkey_file, default_token_file, get_active

__all__ = ["cache_dir", "data_dir", "migrate_legacy", "LEGACY_FILES"]

# Ilgari standart joy — JORIY papka. Shu nomdagi fayllar yangi joyga ko'chiriladi.
LEGACY_FILES = ("token.json", "cam_keys.json", "feature_code.txt")


def cache_dir() -> str:
    """Kesh fayllari joyi — token fayli yonida (u ham hisobga tegishli)."""
    tok = get_active().token_file or default_token_file()
    d = os.path.dirname(os.path.abspath(tok))
    return d or tempfile.gettempdir()


def migrate_legacy(settings, src_dir: str | None = None) -> list[str]:
    """Eski (joriy papkadagi) sessiya/kod/terminal ID fayllarini yangi
    standart papkaga ([[data_dir]]) NUSXALAYDI. Ko'chirilgan nomlar ro'yxatini
    qaytaradi.

    Faqat sozlama STANDART joylarni ishlatsa va u yerda hali fayl bo'lmasa.
    `feature_code.txt` ham ko'chiriladi — busiz bulut shu kompyuterni yangi
    qurilma deb ko'rib, qayta 2FA so'rardi. Asl fayllar o'chirilmaydi."""
    if settings.token_file != default_token_file() \
            or settings.camkey_file != default_camkey_file():
        return []
    src = os.path.abspath(src_dir or os.getcwd())
    dst = data_dir()
    if os.path.normcase(src) == os.path.normcase(dst):
        return []
    # Sessiya bor joyga tegmaymiz — u yerdagi holat yangiroq.
    if os.path.exists(os.path.join(dst, "token.json")):
        return []
    if not os.path.isfile(os.path.join(src, "token.json")):
        return []
    from cloudcam.core.fsutil import write_private
    copied = []
    for name in LEGACY_FILES:
        a, b = os.path.join(src, name), os.path.join(dst, name)
        if not os.path.isfile(a) or os.path.exists(b):
            continue
        try:
            with open(a, encoding="utf-8") as f:
                write_private(b, f.read())
        except OSError:
            continue
        copied.append(name)
    return copied
