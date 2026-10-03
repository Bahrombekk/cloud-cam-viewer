"""Inter-shifr qarorining keshi (kamera bo'yicha).

Inter (P/B) slice shifrlanganmi — bu KAMERANING O'ZGARMAS xususiyati, lekin
har ochilishda qaytadan aniqlanardi va qaror chiqquncha NAL lar buferda
ushlanardi. O'lchov (`central-server`, bir xil dekoder): "birinchi paket" dan
"ffmpeg ga birinchi kadr" gacha 0.65-0.85s aynan shu kutish edi.

Qaror BARIBIR tekshiriladi ([[nal._decide_inter]]) — kamera proshivkasi
o'zgarsa kesh o'zini tuzatadi.
"""
from __future__ import annotations

import json
import os
import tempfile

from ..settings import get_active


def _path(key: str) -> str:
    safe = "".join(c for c in str(key) if c.isalnum() or c in "-_")
    return os.path.join(get_active().cache_dir(), f"enc-{safe}.json")


def load(key: str):
    """Oldingi ochilishda aniqlangan qaror (yo'q/buzuq bo'lsa None)."""
    try:
        with open(_path(key), encoding="utf-8") as f:
            return bool(json.load(f)["inter_enc"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


def store(key: str, value: bool) -> None:
    """ATOMAR yozadi — kesh optimizatsiya, xatosi ishni to'xtatmasin."""
    try:
        d = get_active().cache_dir()
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"inter_enc": bool(value)}, f)
        os.replace(tmp, _path(key))
    except (OSError, TypeError, ValueError):
        pass
