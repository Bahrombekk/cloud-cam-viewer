"""Open a cloud (VTM) stream for one camera; session from token file."""

from __future__ import annotations



from cloudcam.i18n import t
from cloudcam.settings import get_active
from cloudcam.sources.cloud import vtm as _vtm
from cloudcam.sources.cloud import vtm_cache
from cloudcam.sources.cloud.client import CloudClient
from cloudcam.sources.cloud.vtm import STREAM_MAIN


def _is_missing_resource(exc) -> bool:
    """Qurilma bulut ro'yxatida YO'Q (oflayn kamera ro'yxatdan tushadi).

    Bu ESKIRGAN metama'lumot EMAS — pagelist muvaffaqiyatli olingan, qurilmaning
    o'zi yo'q. Shuning uchun bu holatda VTM keshini bekor qilish zarar: kesh
    hisob bo'yicha, ya'ni bitta oflayn kamera butun hisobning keshini o'chirardi
    (bizda 151 kameradan 31 tasi oflayn edi — bu doim ishlab turardi)."""
    return "could not find vtm resource" in str(exc or "").lower()



def open_cloud_stream(client, serial: str, *, channel: int = 1,
                      stream_type: int = STREAM_MAIN, timeout: float | None = 20.0):
    """Kamera oqimini ochadi (ishga tushirilmagan [[vtm.VtmStreamClient]]).

    Metama'lumot (pagelist + VTDU token) hisob keshidan olinadi ([[vtm_cache]]).

    `stream_type=STREAM_SUB` — bulutdan KICHIK oqim (substream). Nega kerak:
    grid'da har plitka ekranda ~250-500 px, kamera esa 2560x1440 yuboradi —
    ekran ko'rsatolmaydigan ~30 barobar ortiqcha piksel. O'lchov (bir xil devor,
    23 plitka): asosiy oqimda tasvir to'kilib qotgan, substream bilan har plitka
    **640x360, ~0.11 Mbit/s** (asosiysi ~4 Mbit/s — 36 barobar kam) va hammasi
    toza dekodlangan. Hamma kamerada substream bo'lavermaydi (batareyali
    modellar) — shuning uchun "auto" rejim bor ([[serve]]).

    Ilgari bu kutubxonaning URL quruvchisini GLOBAL almashtirish (monkeypatch)
    bilan qilinardi: ko'p threadli serverda ikki so'rov bir-birining rejimini
    bosib ketishi mumkin edi, upstream URL formati o'zgarsa esa jimgina asosiy
    oqimga aylanardi. Endi bu oddiy parametr."""
    return _vtm.open_cloud_stream(
        vtm_cache.pagelist(client), vtm_cache.vtdu(client), serial,
        channel=channel, client_type=9, stream_type=stream_type, timeout=timeout)


def _make_client():
    """Token faylidan sessiya (login QILINMAYDI).

    [[CloudClient.load_token]] domen va terminal kodini tekshiradi: token fayli
    begona hostga ishora qilsa yoki boshqa kompyuterdan ko'chirilgan bo'lsa
    sessiya `sessionId` bilan hech qayerga yuborilmaydi."""
    s = get_active()
    client = CloudClient(None, None, platform=s.platform)
    if not client.load_token(s.token_file):
        raise RuntimeError(t(f"No usable session in {s.token_file} — log in first",
                             f"{s.token_file} da yaroqli sessiya yo'q — avval login qiling"))
    # Hisob kaliti MIJOZGA bog'lanadi — kesh shundan o'qiydi ([[vtm_cache.tag]]).
    vtm_cache.tag(client)
    return client
