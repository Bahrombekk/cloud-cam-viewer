"""`pyezvizapi` ustidagi tuzatishlar — HAMMASI shu yerda.

Yadro upstream kutubxonaning ICHKI funksiyalarini almashtirishga tayanadi.
Bu kuchli, lekin mo'rt: `pyezvizapi` reverse-engineering kutubxonasi va tez
o'zgaradi. Tuzatishlar uch joyga tarqalgan bo'lsa, upstream relizida nima
sinishini topish qiyin — shuning uchun bitta modul.

Almashtiriladigan/ishlatiladigan nomlar ([[REQUIRED]]) import vaqtida
TEKSHIRILADI. Upstream birortasini qayta nomlasa, `IncompatibleUpstream`
aniq xabar bilan ko'tariladi — `AttributeError` yoki jim nosozlik emas.

Uchta tuzatish:
  1. `get_vtm_page_list` — kutubxona faqat 1-sahifani oladi va clientType'i
     boshqacha; ko'p NVR'li hisobda kameralar umuman topilmaydi.
  2. Hisob bo'yicha fayl keshi ([[vtm_cache]]) — pagelist + VTDU token har
     ochilishda ~6s bulutga ketardi.
  3. `build_vtm_url` — substream so'rash uchun `stream=1` -> `stream=2`.
"""
from __future__ import annotations

import logging
import re

import requests as _rq
import pyezvizapi.cloud_stream as _cs

from ...core.identity import feature_code
from ...settings import get_active
from . import vtm_cache

log = logging.getLogger(__name__)

# Platformaga qarab klient turi (pagelist to'liq natija qaytarishi uchun muhim)
CLIENT_TYPE = "55" if get_active().platform == "hikconnect" else "1"

# Upstream'da BO'LISHI SHART bo'lgan nomlar: (modul, atribut).
REQUIRED = (
    ("pyezvizapi.cloud_stream", "get_vtm_page_list"),
    ("pyezvizapi.cloud_stream", "get_vtdu_token_v2"),
    ("pyezvizapi.cloud_stream", "build_vtm_url"),
    ("pyezvizapi.cloud_stream", "open_cloud_stream"),
    ("pyezvizapi.stream", "rtp_payload"),
    ("pyezvizapi.client", "EzvizClient"),
)


class IncompatibleUpstream(RuntimeError):
    """`pyezvizapi` versiyasi kutilgan interfeysni bermayapti."""


def check_upstream() -> None:
    """[[REQUIRED]] dagi har bir nom borligini tekshiradi."""
    import importlib
    missing = []
    for mod_name, attr in REQUIRED:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError as e:
            raise IncompatibleUpstream(f"{mod_name} import qilinmadi: {e}") from e
        if not hasattr(mod, attr):
            missing.append(f"{mod_name}.{attr}")
    if missing:
        try:
            ver = importlib.import_module("pyezvizapi").__version__
        except Exception:
            ver = "noma'lum"
        raise IncompatibleUpstream(
            f"pyezvizapi {ver} da quyidagilar yo'q: {', '.join(missing)}. "
            f"cloudcam shu nomlarga tayanadi — mos versiyani o'rnating.")


def _paged_vtm_page_list(client):
    """VTM pagelist'ni TO'LIQ (barcha sahifalar) yig'adi.
    Kutubxona faqat 1-sahifani oladi va clientType si boshqacha -> ko'p NVR
    hisobida kameralar topilmaydi. Shuning uchun to'g'ri header bilan xom so'rov."""
    tok = getattr(client, "_token", {}) or {}
    api = tok.get("api_url")
    sess = _rq.Session()
    sess.headers.update({
        "clientType": CLIENT_TYPE, "lang": "en-US",
        "featureCode": feature_code(),        # shu o'rnatmaga xos ([[identity]])
        "sessionId": str(tok.get("session_id")),
    })
    base = None
    merged_res, merged_vtm = [], {}
    offset = 0
    for _ in range(40):
        r = sess.get(f"https://{api}/v3/userdevices/v1/resources/pagelist",
                     params={"filter": "VTM", "groupId": -1, "limit": 50, "offset": offset},
                     timeout=25)
        pl = r.json()
        if base is None:
            base = pl
        res = pl.get("resourceInfos") or []
        merged_res.extend(res)
        vtm = pl.get("VTM")
        if isinstance(vtm, dict):
            merged_vtm.update(vtm)
        page = pl.get("page") or {}
        if not res or not page.get("hasNext"):
            break
        offset += len(res)
    if base is not None:
        base["resourceInfos"] = merged_res
        base["VTM"] = merged_vtm
    return base




_installed = False


def install_patches() -> str:
    """Tuzatishlarni o'rnatadi (idempotent). Qaytaradi: hisob kaliti.

    Ilgari bu MODUL IMPORT QILINGANDA bajarilardi — ya'ni `cloudcam` ni
    import qilishning o'zi `pyezvizapi` ni global o'zgartirardi va buni
    bilmagan boshqa kod ham bizning tuzatishlarimizni olardi. Endi ataylab
    chaqiriladi.
    """
    global _installed
    check_upstream()
    if not _installed:
        # Bir-sahifali funksiyani to'liq sahifalovchi bilan almashtiramiz,
        # uning USTIGA esa hisob bo'yicha fayl keshini qo'yamiz ([[vtm_cache]]).
        _cs.get_vtm_page_list = _paged_vtm_page_list
        _installed = True
    return vtm_cache.install()


_orig_build_vtm_url = None

def set_substream(enable: bool) -> None:
    """Bulutdan KICHIK (substream) oqimni so'raydi — VTM URL'idagi `stream=1`
    o'rniga `stream=2`.

    Nega kerak: grid'da har plitka ekranda ~250-500 px, kamera esa 2560x1440
    yuboradi — bu ekran ko'rsatolmaydigan ~30 barobar ortiqcha piksel. O'lchov
    (bir xil devor, 23 plitka): asosiy oqimda tasvir to'kilib qotgan, substream
    bilan har plitka **640x360, ~0.11 Mbit/s** (asosiysi ~4 Mbit/s — 36 barobar
    kam) va hammasi toza dekodlangan. Zaif uplink'da paket yo'qolishi ham
    kamayadi (yashil chiziqlar/buzilish).

    Hamma kamerada substream bo'lavermaydi (masalan batareyali modellar) —
    bunday holda bulut asosiy oqimni beradi, ya'ni zarari yo'q.
    """
    global _orig_build_vtm_url
    import re as _re
    if _orig_build_vtm_url is None:
        _orig_build_vtm_url = _cs.build_vtm_url          # ASL nusxa (bir marta)
    if enable:
        def _sub(*a, **kw):
            return _re.sub(r"(?<=[?&])stream=1(?=&|$)", "stream=2",
                           _orig_build_vtm_url(*a, **kw))
        _cs.build_vtm_url = _sub
    else:
        _cs.build_vtm_url = _orig_build_vtm_url


