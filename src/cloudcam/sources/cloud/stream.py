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


def _make_client(settings=None):
    """Token faylidan sessiya (login QILINMAYDI).

    [[CloudClient.load_token]] domen va terminal kodini tekshiradi: token fayli
    begona hostga ishora qilsa yoki boshqa kompyuterdan ko'chirilgan bo'lsa
    sessiya `sessionId` bilan hech qayerga yuborilmaydi.

    `settings` — qaysi hisob (standart: faol sozlama). Bir jarayonda bir nechta
    `CloudCam` bo'lsa har biri o'zinikini beradi."""
    s = settings or get_active()
    client = CloudClient(None, None, platform=s.platform)
    if not client.load_token(s.token_file):
        raise RuntimeError(t(f"No usable session in {s.token_file} — log in first",
                             f"{s.token_file} da yaroqli sessiya yo'q — avval login qiling"))
    # Hisob kaliti MIJOZGA bog'lanadi — kesh shundan o'qiydi ([[vtm_cache.tag]]).
    vtm_cache.tag(client, s.token_file)
    return client


# ---- kanal holatini tez tekshirish ---------------------------------------

ONLINE, OFFLINE, UNKNOWN = "online", "offline", "unknown"

# `StreamInfoRsp.result`: kanal oqim bermaydi (bo'sh NVR porti / uzilgan kamera).
# Jonli: 57 kanaldan 21 tasi substream ham, asosiy ham uchun darhol 6106 qaytardi
# (oldin bu tekshirilmay, video 8 s kutilib timeout bo'lardi).
NO_STREAM_RESULT = 6106


def _first_packet(meta, serial, channel, stream_type, timeout) -> str:
    """Bitta urinish: ONLINE (video paketi keldi) | "silent" (javob/video yo'q) |
    "no-stream" (bulut 6106 dedi) | OFFLINE (kanal ro'yxatda yo'q) | UNKNOWN."""
    pl, vt = meta
    try:
        with _vtm.open_cloud_stream(pl, vt, serial, channel=channel, client_type=9,
                                    stream_type=stream_type, timeout=timeout) as s:
            result = getattr(s.start(), "result", 0)
            if result == NO_STREAM_RESULT:
                return "no-stream"
            if result not in (0, None):
                return UNKNOWN                     # boshqa bulut xato kodi
            for _ in s.iter_packets(max_packets=1, keepalive_interval=None):
                return ONLINE
            return "silent"
    except _vtm.DeviceOffline:
        return "silent"                            # soket jim qoldi (timeout)
    except Exception as e:                         # noqa: BLE001
        return OFFLINE if _is_missing_resource(e) else UNKNOWN


def probe(client, serial: str, channel: int = 1, *, timeout: float = 6.0,
          main_timeout: float = 10.0, meta: tuple | None = None) -> str:
    """Kanal haqiqatan oqim beradimi: `online` | `offline` | `unknown`.

    Nega kerak: qurilma holati (`devices/status`) NVR DARAJASIDA — NVR onlayn
    bo'lsa unga ulangan hamma kanal "yashil" ko'rinardi. Jonli tekshiruvda
    57 kameradan 22 tasi (bo'sh NVR portlari, uzilgan kameralar) aslida oqim
    bermagan. ffmpeg/deshifr YO'Q: avval kichik oqim (substream) so'raladi —
    ishlaydigan kanal ~1-3 s da birinchi paketni beradi, trafik juda kam.

    Oflayn kanalga bulut darhol `result=6106` qaytaradi ([[NO_STREAM_RESULT]]).
    Lekin substream so'rovidagi 6106 yoki jimlik yetarli dalil emas: substream'i
    yo'q kamera (batareyali modellar) ham shunday javob beradi, jonli hisobda esa
    bir kanal substream'ga 6106, asosiyga 0 bergan. Shuning uchun ASOSIY oqim ham
    sinaladi; u ham 6106 yoki video bermasa — `offline`.
    `unknown` — tarmoq/sessiya/bulut xatosi (kanal haqida xulosa yo'q).
    `meta` — oldindan olingan (pagelist, vtdu); bo'lmasa hisob keshidan."""
    from cloudcam.sources.cloud.vtm import STREAM_MAIN, STREAM_SUB
    if meta is None:
        try:
            meta = (vtm_cache.pagelist(client), vtm_cache.vtdu(client))
        except Exception:                          # noqa: BLE001
            return UNKNOWN
    r = _first_packet(meta, serial, channel, STREAM_SUB, timeout)
    if r in ("silent", "no-stream"):
        r = _first_packet(meta, serial, channel, STREAM_MAIN, main_timeout)
    return OFFLINE if r in ("silent", "no-stream") else r


def probe_many(client, channels, *, workers: int = 8, timeout: float = 6.0,
               main_timeout: float = 10.0) -> dict:
    """{(serial, channel): holat} — [[probe]] parallel. `channels`: [(serial, ch), ...].

    Bir vaqtda `workers` tadan ortiq ochilmaydi: bitta NVR'dan ko'p oqim birdan
    so'ralsa uning internet kanali to'lib, natija buzilishi mumkin.
    Metama'lumot (pagelist, VTDU) olinmasa — hammasi `unknown` (xato ko'tarilmaydi)."""
    from concurrent.futures import ThreadPoolExecutor
    channels = list(dict.fromkeys(channels))
    # Metama'lumot BIR MARTA: keshga belgilanmagan mijozda ([[vtm_cache.tag]])
    # har probe uni bulutdan qayta so'rardi — 57 kanalda 114 ortiqcha so'rov.
    try:
        meta = (vtm_cache.pagelist(client), vtm_cache.vtdu(client))
    except Exception:                              # noqa: BLE001
        return {sc: UNKNOWN for sc in channels}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        res = ex.map(lambda sc: probe(client, sc[0], sc[1], timeout=timeout,
                                      main_timeout=main_timeout, meta=meta), channels)
        return dict(zip(channels, res))


def classify(live: dict, encrypted, keys: dict) -> dict:
    """[[probe_many]] natijasi -> "ok" | "nokey" | "offline" | "unknown".

    `encrypted` — shifrlangan qurilmalar seriyalari to'plami; None bo'lsa holat
    olinmagan: onlayn kanal "ok" deb ko'rsatilmaydi (kodi yo'q shifrli kamera
    ochilganda baribir xato berardi), `unknown` bo'ladi."""
    out = {}
    for (serial, ch), st in live.items():
        if st == ONLINE:
            if encrypted is None:
                st = UNKNOWN
            elif serial in encrypted and keys.get(serial) in (None, "", "AUTO"):
                st = "nokey"
            else:
                st = "ok"
        out[(serial, ch)] = st
    return out
