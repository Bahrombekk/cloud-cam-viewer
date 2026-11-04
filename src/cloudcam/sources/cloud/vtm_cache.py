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

import contextlib
import json
import os
import tempfile
import threading
import time
from typing import Any

from cloudcam.core.paths import cache_dir  # noqa: F401  (re-export)
from cloudcam.settings import default_token_file, get_active
from cloudcam.sources.cloud.vtm import cloud as _cloud

TTL_SECONDS = 3600.0            # kesh amal qilish muddati
_ENV_TTL = "CLOUDCAM_VTM_TTL"


def _ttl() -> float:
    try:
        return float(os.environ.get(_ENV_TTL) or TTL_SECONDS)
    except ValueError:
        return TTL_SECONDS


def account_key(token_file: str | None = None) -> str:
    """Hisob kaliti — token fayl nomidan (`token.json` -> `token`)."""
    tok = token_file or get_active().token_file or default_token_file()
    return os.path.splitext(os.path.basename(tok))[0] or "default"


def _path(key: str, kind: str) -> str:
    safe = "".join(c for c in key if c.isalnum() or c in "-_")
    return os.path.join(cache_dir(), f"vtm-{safe}-{kind}.json")


def load(key: str, kind: str) -> Any | None:
    """Kesh yangi bo'lsa qiymat, aks holda None ([[bypass_read]] ichida doim None)."""
    if getattr(_tls, "bypass", False):
        return None
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
# ---- Keshlangan metama'lumot ----------------------------------------------
#
# Hisob kaliti MIJOZ obyektidan o'qiladi ([[tag]]), closure'dan emas. Ilgari
# kesh kutubxona funksiyalari ustiga o'ram sifatida qo'yilardi va har
# `install()` (boshqa hisob bilan) o'ramni QAYTA qo'yib ZANJIRLARDI: B ning keshi
# bo'sh -> ichki = A ning o'rami -> A ning qurilmalari B keshiga yozilardi
# (central-server'da jonli o'lchangan). Endi o'ram yo'q — [[pagelist]] /
# [[vtdu]] oddiy funksiyalar, zanjir paydo bo'la olmaydi.

_KEY_ATTR = "_cloudcam_cache_key"

_tls = threading.local()


@contextlib.contextmanager
def bypass_read():
    """Shu THREAD ichida [[load]] doim None qaytaradi — kesh O'QILMAYDI.

    Keshni yangilash uchun: hali yaroqli eski qiymat qaytmasin, bulutga
    borilsin. Ilgari buning o'rniga `invalidate()` qilinardi — bulut xato bersa
    (504) kesh BO'SH qolar va o'sha hisobdagi har kamera ~6s ortiq ochilardi.
    Endi kesh FAQAT muvaffaqiyatli javob bilan almashadi."""
    prev = getattr(_tls, "bypass", False)
    _tls.bypass = True
    try:
        yield
    finally:
        _tls.bypass = prev


def tag(client: Any, token_file: str | None = None) -> str:
    """Mijozni hisob kaliti bilan belgilaydi (kesh shu kalitda yuritiladi)."""
    key = account_key(token_file)
    try:
        setattr(client, _KEY_ATTR, key)
    except Exception:  # noqa: BLE001 — slots/proxy obyekt: keshsiz ishlaydi
        pass
    return key


def _through(client: Any, kind: str, fetch) -> Any:
    """Keshdan, bo'lmasa bulutdan (va keshga yozib). Belgilanmagan ([[tag]])
    mijoz keshsiz o'tadi — noma'lum hisobni boshqa hisob keshiga yozishdan ko'ra."""
    k = getattr(client, _KEY_ATTR, None)
    if not k:
        return fetch(client)
    hit = load(k, kind)
    if hit is not None:
        return hit
    data = fetch(client)
    store(k, kind, data)
    return data


def pagelist(client: Any) -> dict:
    """VTM pagelist (TO'LIQ, barcha sahifalar) — hisob keshi orqali."""
    return _through(client, "pagelist", _cloud.fetch_vtm_page_list)


def vtdu(client: Any) -> dict:
    """VTDU oqim tokenlari — hisob keshi orqali."""
    return _through(client, "vtdu", _cloud.fetch_vtdu_token)


# ---- Keshni yangilash (o'chirmasdan) --------------------------------------

PAGELIST_REFRESH_FLOOR = 30.0   # shundan yosh ro'yxat qayta so'ralmaydi
REFRESH_FLOOR = 30.0            # [[refresh]]: shundan yosh metama'lumot so'ralmaydi


def refresh(client: Any, kinds: tuple[str, ...] = ("pagelist", "vtdu")) -> bool:
    """Metama'lumotni BULUTDAN qayta oladi; kesh faqat muvaffaqiyatda almashadi.

    Oqim ochilmaganda keshni o'chirish (`invalidate`) o'rniga shu: VTDU tokeni
    eskirgan bo'lsa yangisi yoziladi, bulut xato bersa eski kesh joyida qoladi.

    [[REFRESH_FLOOR]] dan yosh metama'lumot qayta so'ralmaydi (fayl yoshi —
    barcha jarayonlar uchun umumiy): doimiy xato beradigan kamera har 1-2 s da
    qayta urinib, har safar bulutga 2-3 so'rov yuborardi."""
    key = getattr(client, _KEY_ATTR, None)
    if key:
        ages = [age(key, k) for k in kinds]
        if all(a is not None and a < REFRESH_FLOOR for a in ages):
            return False
    try:
        with bypass_read():
            if "pagelist" in kinds:
                pagelist(client)
            if "vtdu" in kinds:
                vtdu(client)
        return True
    except Exception:  # noqa: BLE001 — "best effort"
        return False


def refresh_pagelist(client: Any, key: str) -> bool:
    """Qurilma ro'yxatini yangilaydi — "kamera topilmadi" ni OFLAYN deyishdan oldin.

    Kesh HISOB bo'yicha va 1 soat yashaydi: hisobga YANGI kamera qo'shilsa,
    keshdagi ro'yxatda u yo'q va bulut `Could not find VTM resource` beradi —
    bu oflayn qurilma bilan bir xil ko'rinadi, kamera esa TTL tugaguncha
    "oflayn" bo'lib qolardi. central-server'da jonli o'lchangan (keshda 18
    resurs, bulutda 19 — farq aynan shu kamera; yangilangach darhol ochildi).

    True — ro'yxat haqiqatan yangilandi (qayta urinish ma'noli). Yangi ro'yxat
    [[PAGELIST_REFRESH_FLOOR]] dan yosh bo'lsa so'ralmaydi: oflayn kameralarni
    ketma-ket bosish bulutni urmasin."""
    a = age(key, "pagelist")
    if a is not None and a < PAGELIST_REFRESH_FLOOR:
        return False
    return refresh(client, ("pagelist",))


def warm(client: Any, key: str) -> bool:
    """Kesh yarmidan ko'p eskirgan bo'lsa fonda yangilaydi ([[Warmer]]).

    Real foydalanishda kameralar doim ochiq turmaydi — 1 soatlik TTL ochilishlar
    orasida tugab, keyingi ochilish ~6s metama'lumot so'roviga tushardi.
    central-server o'lchovi: issiq kesh 0.14s, sovuq 6.08s."""
    a = age(key, "pagelist")
    if a is not None and a < _ttl() / 2:
        return False
    return refresh(client)


class Warmer:
    """Asosiy jarayonda VTM keshini fonda issiq tutadi (daemon thread)."""

    def __init__(self, make_client, key: str, interval: float | None = None):
        self._make_client = make_client
        self._key = key
        self._interval = interval or max(60.0, _ttl() / 4)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "Warmer":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="vtm-warmer",
                                            daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                warm(self._make_client(), self._key)
            except Exception:  # noqa: BLE001 — fon vazifasi viewer'ni yiqitmasin
                pass
            self._stop.wait(self._interval)

    def stop(self) -> None:
        self._stop.set()
