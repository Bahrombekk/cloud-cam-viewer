"""ISUP 5.0 — qurilma bulutga emas, BIZGA ulanadi.

Bulut yo'li (VTM relay -> deshifr -> remux) ishlaydi, lekin u bulutning
shartlariga bog'liq: birinchi kadr 4-14 s, bir vaqtda ochiladigan oqim soni
cheklangan, va oqim ba'zan buziladi. ISUP'da Hikvision qurilmasi bulutni
chetlab o'tib **to'g'ridan-to'g'ri sizning serveringizga ulanadi**, ya'ni
tasdiqlash kodi ham, deshifr ham, VTM ham kerak emas.

O'lchov (`central-server`, prod, DS-TCG406-E fw V5.4.0, 2026-09-30):

    "video yubor" buyrug'i -> birinchi paket   0.23 s   (bulutda 4-14 s)
    birinchi toza kadr (transkod bilan)        0.33-0.38 s
    xato kadrlar (25 MB+)                      0

Me'moriy chegara
----------------
ISUP protokolini Python'da gapirib bo'lmaydi — ro'yxatdan o'tish, heartbeat va
oqim muzokarasi Hikvision'ning yopiq SDK'si (`libHCISUPCMS.so` va boshqalar)
ichida. Shuning uchun protokol TASHQI ko'prik jarayonida gaplashiladi
(`isup-bridge`, C++), xuddi videoni dekodlash tashqi `ffmpeg` da bo'lgani kabi.

Bu modul — o'sha ko'prikning HTTP boshqaruv API'si uchun klient:

    bridge = IsupBridge("http://127.0.0.1:8091", token="...")
    for dev in bridge.devices():
        print(dev.id, dev.ip, dev.channels_total)
    bridge.set_keys({"AA1234567": "KALIT"})
    bridge.want(("AA1234567", 1, False))        # asosiy oqimni ochish
    url = bridge.stream_url("AA1234567", 1)     # rtsp://.../isup/AA1234567/1

Ko'prik oqimni MediaMTX'ga RTSP bilan **transkodsiz** publish qiladi, ya'ni
natija oddiy RTSP manzil — uni [[CameraStream]] to'g'ridan-to'g'ri o'qiydi
(dekodlovchi proxy ishtirok etmaydi).

Ko'prikni o'rnatish `ffmpeg` kabi alohida qadam (Docker; Hikvision SDK
litsenziya sababli tarqatilmaydi) — ya'ni bu modul ko'prik bo'lmasa ham
import qilinadi va tushunarli xato beradi.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import requests

DEFAULT_URL = "http://127.0.0.1:8091"
DEFAULT_RTSP_BASE = "rtsp://127.0.0.1:8554"
DEFAULT_TIMEOUT = 5.0


class IsupError(RuntimeError):
    """Ko'prik bilan aloqa yoki uning javobi bilan bog'liq xato."""


class IsupUnavailable(IsupError):
    """Ko'prik ishlamayapti yoki manzil noto'g'ri."""


@dataclass(frozen=True)
class IsupStream:
    """Ko'prikda ayni damda ochiq turgan bitta oqim."""
    channel: int
    sub: bool                 # True = substream
    path: str                 # MediaMTX yo'li, masalan "isup/AA123/1"
    live: bool                # oxirgi 5 s ichida ma'lumot keldimi
    bytes: int
    age: float                # oxirgi ma'lumotdan beri soniya (-1 = hech qachon)


@dataclass(frozen=True)
class IsupDevice:
    """Ko'prikka ulangan qurilma (NVR yoki kamera)."""
    id: str                   # Device ID — qurilmada sozlanadi
    valid: bool               # Device ID formati to'g'rimi
    serial: str
    ip: str
    firmware: str
    channels_total: int
    channels_analog: int
    start_channel: int
    online_seconds: int
    key_source: str           # kalit qayerdan olindi: dev/prefix/default
    streams: tuple[IsupStream, ...] = ()

    @property
    def channels(self) -> list[int]:
        """Kanal raqamlari (qurilma bergan boshlang'ich raqamdan)."""
        start = self.start_channel or 1
        return list(range(start, start + max(self.channels_total, 0)))


@dataclass(frozen=True)
class IsupStatus:
    """`GET /devices` ning umumiy qismi — tashxis uchun."""
    want_set: bool            # backend "want" ro'yxatini yubordimi
    keys_set: bool            # kalitlar yuborildimi
    default_key: bool         # zaxira kalit (muhitdagi ISUP_KEY) yoqiqmi
    rejected: tuple[dict, ...] = ()   # rad etilgan qurilmalar va sababi
    devices: tuple[IsupDevice, ...] = ()


def _stream(d: dict) -> IsupStream:
    return IsupStream(
        channel=int(d.get("channel", 0)),
        sub=int(d.get("type", 0)) == 1,
        path=str(d.get("path", "")),
        live=bool(d.get("live")),
        bytes=int(d.get("bytes", 0)),
        age=float(d.get("age", -1.0)),
    )


def _device(d: dict) -> IsupDevice:
    return IsupDevice(
        id=str(d.get("id", "")),
        valid=bool(d.get("valid")),
        serial=str(d.get("serial", "")),
        ip=str(d.get("ip", "")),
        firmware=str(d.get("firmware", "")),
        channels_total=int(d.get("channels_total", 0)),
        channels_analog=int(d.get("channels_analog", 0)),
        start_channel=int(d.get("start_channel", 1)),
        online_seconds=int(d.get("online_seconds", 0)),
        key_source=str(d.get("key_source", "")),
        streams=tuple(_stream(s) for s in d.get("streams") or ()),
    )


class IsupBridge:
    """`isup-bridge` ning HTTP boshqaruv API'si uchun klient.

    Ko'prik faqat ichki tarmoqqa ochiladi; `token` berilgan bo'lsa har so'rovda
    `X-Bridge-Token` sarlavhasi yuboriladi.
    """

    def __init__(self, url: str = DEFAULT_URL, *, token: str | None = None,
                 rtsp_base: str = DEFAULT_RTSP_BASE,
                 timeout: float = DEFAULT_TIMEOUT, session=None):
        self.url = url.rstrip("/")
        self.rtsp_base = rtsp_base.rstrip("/")
        self.timeout = timeout
        self._s = session or requests.Session()
        if token:
            self._s.headers["X-Bridge-Token"] = token
        # Ochiq turishi kerak bo'lgan oqimlar: {(device, channel): sub}
        self._wanted: dict[tuple[str, int], bool] = {}

    # ── quyi daraja ──────────────────────────────────────────────────
    def _call(self, method: str, path: str, body: str | None = None):
        try:
            r = self._s.request(method, f"{self.url}{path}", data=body,
                                timeout=self.timeout)
        except requests.RequestException as e:
            raise IsupUnavailable(
                f"isup-bridge javob bermadi ({self.url}): {e}") from e
        if r.status_code == 401:
            raise IsupError("isup-bridge: token noto'g'ri (X-Bridge-Token)")
        if r.status_code >= 400:
            raise IsupError(f"isup-bridge {method} {path} -> {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise IsupError(f"isup-bridge JSON bermadi: {r.text[:200]!r}") from e

    # ── holat ────────────────────────────────────────────────────────
    def health(self) -> bool:
        """Ko'prik tirikmi (xato KO'TARMAYDI — tekshiruv uchun)."""
        try:
            return bool(self._call("GET", "/health").get("ok"))
        except IsupError:
            return False

    def status(self) -> IsupStatus:
        """To'liq holat: qurilmalar, oqimlar va rad etilganlar."""
        d = self._call("GET", "/devices")
        return IsupStatus(
            want_set=bool(d.get("want_set")),
            keys_set=bool(d.get("keys_set")),
            default_key=bool(d.get("default_key")),
            rejected=tuple(d.get("rejected") or ()),
            devices=tuple(_device(x) for x in d.get("devices") or ()),
        )

    def devices(self) -> list[IsupDevice]:
        return list(self.status().devices)

    # ── kalitlar ─────────────────────────────────────────────────────
    def set_keys(self, keys: dict[str, str] | None = None, *,
                 blocked: Iterable[str] = (),
                 prefixes: dict[str, str] | None = None,
                 default: bool = True) -> dict:
        """Qaysi qurilma qaysi kalit bilan qabul qilinishini belgilaydi.

        DIQQAT: bu ro'yxat ko'prikdagi oldingisini BUTUNLAY almashtiradi —
        qo'shmaydi. Ko'prik kaliti o'zgargan qurilmani uzib yuboradi, u yangi
        kalit bilan qayta ulanadi.

        `default=False` — muhitdagi zaxira `ISUP_KEY` ishlatilmasin (ishlab
        chiqarishda shu tavsiya etiladi: har qurilmaning o'z kaliti bo'lsin).
        """
        lines = []
        for dev, key in sorted((keys or {}).items()):
            lines.append(f"dev {dev} {key}")
        for dev in sorted(set(blocked)):
            lines.append(f"block {dev}")
        for pref, key in sorted((prefixes or {}).items()):
            lines.append(f"prefix {pref} {key}")
        lines.append(f"default {'on' if default else 'off'}")
        res = self._call("PUT", "/keys", "\n".join(lines) + "\n")
        if res.get("bad"):
            raise IsupError(f"isup-bridge {res['bad']} ta kalit qatorini tushunmadi")
        return res

    # ── qaysi oqim ochiq tursin ──────────────────────────────────────
    def want(self, *streams: tuple[str, int, bool], replace: bool = False) -> dict:
        """Ochiq turishi kerak bo'lgan oqimlarni belgilaydi.

        Har element: `(device_id, channel, sub)`. `replace=True` — ro'yxatni
        butunlay almashtiradi; aks holda mavjudiga qo'shiladi.

        Nega kerak: asosiy oqim ~4 Mbit/s, substream ~0.3 Mbit/s. 100 kamera
        24/7 asosiy oqimda tursa 420 Mbit/s bo'lardi. ISUP'da oqim ~0.23 s da
        ochilgani uchun uni faqat KO'RILAYOTGANDA ochish sezilmaydi.
        """
        if replace:
            self._wanted.clear()
        for dev, ch, sub in streams:
            self._wanted[(str(dev), int(ch))] = bool(sub)
        return self._flush_want()

    def unwant(self, *streams: tuple[str, int]) -> dict:
        """Oqimlarni ro'yxatdan olib tashlaydi (ko'prik ularni yopadi)."""
        for dev, ch in streams:
            self._wanted.pop((str(dev), int(ch)), None)
        return self._flush_want()

    def _flush_want(self) -> dict:
        lines = [f"{dev} {ch} {1 if sub else 0}"
                 for (dev, ch), sub in sorted(self._wanted.items())]
        return self._call("PUT", "/want", "\n".join(lines) + "\n")

    @property
    def wanted(self) -> dict[tuple[str, int], bool]:
        """Shu klient so'ragan oqimlar (diagnostika uchun nusxa)."""
        return dict(self._wanted)

    # ── oqim manzili ─────────────────────────────────────────────────
    def stream_url(self, device_id: str, channel: int = 1, *,
                   sub: bool = False) -> str:
        """Ko'prik publish qiladigan RTSP manzil.

        Bu oddiy RTSP — dekodlovchi proxy ham, tasdiqlash kodi ham kerak emas.
        """
        path = f"isup/{device_id}/{int(channel)}"
        return f"{self.rtsp_base}/{path}/sub" if sub else f"{self.rtsp_base}/{path}"


def from_settings(settings=None) -> IsupBridge:
    """Sozlamadagi manzil/token bilan ko'prik klienti."""
    from .settings import get_active
    s = settings or get_active()
    return IsupBridge(s.isup_url, token=s.isup_token or None,
                      rtsp_base=s.isup_rtsp_base)
