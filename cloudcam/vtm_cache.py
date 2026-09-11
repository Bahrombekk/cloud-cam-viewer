"""HISOB darajasidagi bulut metama'lumotlari uchun FAYL keshi.

Muammo: `open_cloud_stream` har ochilishda ikkita bulut so'rovini qiladi —
`get_vtm_page_list` (ko'p NVR'li hisobda ko'p sahifali, o'lchangan ~4.3s / 236
resurs) va `get_vtdu_token_v2` (~1.8s). Ikkalasi ham KAMERAGA emas, HISOBGA
tegishli: bitta hisobdagi 32 kamera uchun javob bir xil. Keshsiz har kamera,
har qayta ulanishda shu ~6s ni bekorga to'laydi va bulutni ham urib beradi.

O'lchov (bitta kamera, bir xil sharoit): issiq kesh **0.24s**, keshsiz **5.76s**.

Har kamera alohida JARAYON bo'lgani uchun kesh xotirada emas, FAYLDA — shunda
u jarayonlar orasida ham ishlaydi.

MUHIM: kesh faqat oqim UMUMAN ochilmaganda bekor qilinadi. Avvalgi tajriba:
"har uzilishda bekor qilish" bitta kameraning lahzalik uzilishi tufayli O'SHA
HISOBDAGI hamma kameraning keshini o'chirardi va keyingi ochilishlar yana to'liq
metama'lumot yuklardi. Bitta paket kelgan bo'lsa — kesh to'g'ri edi.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any

from .settings import get_active

TTL_SECONDS = 3600.0            # kesh amal qilish muddati
_ENV_TTL = "CLOUDCAM_VTM_TTL"


def _ttl() -> float:
    try:
        return float(os.environ.get(_ENV_TTL) or TTL_SECONDS)
    except ValueError:
        return TTL_SECONDS


def cache_dir() -> str:
    """Kesh fayllari joyi — token fayli yonida (u ham hisobga tegishli)."""
    tok = get_active().token_file or "token.json"
    d = os.path.dirname(os.path.abspath(tok))
    return d or tempfile.gettempdir()


def account_key(token_file: str | None = None) -> str:
    """Hisob kaliti — token fayl nomidan (`token.json` -> `token`)."""
    tok = token_file or get_active().token_file or "token.json"
    return os.path.splitext(os.path.basename(tok))[0] or "default"


def _path(key: str, kind: str) -> str:
    safe = "".join(c for c in key if c.isalnum() or c in "-_")
    return os.path.join(cache_dir(), f"vtm-{safe}-{kind}.json")


def load(key: str, kind: str) -> Any | None:
    """Kesh yangi bo'lsa qiymat, aks holda None."""
    try:
        with open(_path(key, kind), encoding="utf-8") as f:
            blob = json.load(f)
        if time.time() - float(blob.get("ts") or 0) > _ttl():
            return None
        return blob.get("data")
    except (OSError, ValueError, TypeError):
        return None


def store(key: str, kind: str, data: Any) -> None:
    """ATOMAR yozadi — bir vaqtda bir necha jarayon yozishi mumkin."""
    try:
        os.makedirs(cache_dir(), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=cache_dir(), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "data": data}, f)
        os.replace(tmp, _path(key, kind))
    except (OSError, TypeError, ValueError):
        pass                       # kesh — optimizatsiya, xato ish to'xtatmasin


def invalidate(key: str, kind: str | None = None) -> None:
    """Keshni o'chiradi. FAQAT oqim ochilmagan bo'lsa chaqiring."""
    for k in ((kind,) if kind else ("pagelist", "vtdu")):
        try:
            os.remove(_path(key, k))
        except OSError:
            pass


def age(key: str, kind: str) -> float | None:
    try:
        with open(_path(key, kind), encoding="utf-8") as f:
            blob = json.load(f)
        return time.time() - float(blob.get("ts") or 0)
    except (OSError, ValueError, TypeError):
        return None


# ---- Kanal darvozasi ------------------------------------------------------

def available_channels(key: str) -> dict[str, set[int]] | None:
    """Keshlangan pagelist'dan {serial: {mavjud kanallar}}.

    NVR bulutda "online" bo'lsa ham, unda MAVJUD BO'LMAGAN kanal hech qachon
    oqim bermaydi — bunday kamera "online, lekin ochilmaydi" bo'lib ko'rinadi.
    Bizda shunday 2 ta kamera NVR'da yo'q kanalga sozlangan edi."""
    data = load(key, "pagelist")
    if data is None:
        return None
    out: dict[str, set[int]] = {}

    def rec(x: Any) -> None:
        if isinstance(x, dict):
            ser = x.get("deviceSerial") or x.get("serial")
            ch = x.get("channelNo", x.get("localIndex", x.get("channel")))
            if isinstance(ser, str):
                out.setdefault(ser, set())
                if ch is not None:
                    try:
                        out[ser].add(int(ch))
                    except (TypeError, ValueError):
                        pass
            for v in x.values():
                rec(v)
        elif isinstance(x, list):
            for i in x:
                rec(i)

    rec(data)
    return out


def channel_missing(key: str, serial: str, channel: int) -> bool:
    """Shu (serial, kanal) bulut resurslarida YO'Qmi?

    Noaniq holatlarda HAR DOIM False — noto'g'ri "yo'q" deyishdan ko'ra
    bilmaslik yaxshi: kesh yo'q, seriya yo'q, yoki kanal to'plami bo'sh
    (bir kanalli qurilma) bo'lsa hech narsa da'vo qilmaymiz."""
    chans = available_channels(key)
    if not chans:
        return False
    have = chans.get(serial)
    if not have:
        return False
    real = {c for c in have if c != 0}      # 0 — ko'pincha "qurilmaning o'zi"
    if not real:
        return False
    return channel not in have


# ---- pyezvizapi'ni kesh bilan o'rash --------------------------------------

def install(token_file: str | None = None) -> str:
    """`get_vtm_page_list` va `get_vtdu_token_v2` ustiga keshni o'rnatadi.

    Sahifalovchi patch USTIGA qo'yiladi — kesh bo'sh bo'lsa baribir TO'LIQ
    (ko'p sahifali) ro'yxat olinadi. Qaytaradi: hisob kaliti (invalidate uchun).
    """
    import pyezvizapi.cloud_stream as _cs

    key = account_key(token_file)
    if getattr(_cs, "_cloudcam_cache_key", None) == key:
        return key                            # allaqachon o'rnatilgan
    inner_pagelist = _cs.get_vtm_page_list
    inner_vtdu = _cs.get_vtdu_token_v2

    def _cached_pagelist(client):
        hit = load(key, "pagelist")
        if hit is not None:
            return hit
        data = inner_pagelist(client)
        store(key, "pagelist", data)
        return data

    def _cached_vtdu(client, *a, **kw):
        hit = load(key, "vtdu")
        if hit is not None:
            return hit
        data = inner_vtdu(client, *a, **kw)
        store(key, "vtdu", data)
        return data

    _cs.get_vtm_page_list = _cached_pagelist
    _cs.get_vtdu_token_v2 = _cached_vtdu
    _cs._cloudcam_cache_key = key
    return key
