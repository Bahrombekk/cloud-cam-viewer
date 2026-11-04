"""Per-camera cache of the inter-slice encryption decision."""

from __future__ import annotations

import json
import os
import tempfile


from cloudcam.core.paths import cache_dir


# ---- Inter-shifr qarorining KESHI --------------------------------------
# Inter (P/B) slice shifrlanganmi — bu KAMERANING O'ZGARMAS xususiyati, lekin
# har ochilishda qaytadan aniqlanardi va qaror chiqquncha NAL lar buferda
# ushlanardi. O'lchov (central-server'da bir xil dekoder): "birinchi paket" dan
# "ffmpeg ga birinchi kadr" gacha 0.65-0.85s aynan shu kutish edi. Kesh bilan
# birinchi kadr darhol ketadi; qaror esa BARIBIR tekshiriladi, shuning uchun
# kamera proshivkasi o'zgarsa kesh o'zini tuzatadi ([[_decide_inter]]).

def _enc_cache_path(key: str) -> str:
    safe = "".join(c for c in str(key) if c.isalnum() or c in "-_")
    return os.path.join(cache_dir(), f"enc-{safe}.json")


def _enc_cache_load(key: str):
    """Oldingi ochilishda aniqlangan qaror (yo'q/buzuq bo'lsa None)."""
    try:
        with open(_enc_cache_path(key), encoding="utf-8") as f:
            return bool(json.load(f)["inter_enc"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _enc_cache_store(key: str, value: bool) -> None:
    """ATOMAR yozadi — kesh optimizatsiya, xatosi ishni to'xtatmasin."""
    try:
        d = cache_dir()
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"inter_enc": bool(value)}, f)
        os.replace(tmp, _enc_cache_path(key))
    except (OSError, TypeError, ValueError):
        pass
