"""Bulutning oqimdan tashqari API'lari: holat, disklar, hodisalar, yozuvlar, PTZ.

Endpoint'lar va javob shakllari `pyezvizapi` 1.0.5.0 (`client.py`, `camera.py`;
Apache-2.0, (c) Renier Moorcroft — `vtm/LICENSE-pyezvizapi`) dan olingan.
Hik-Connect hisobida jonli tekshirilgani (2026-11-03): holat, hodisalar va
xabarlar ro'yxati, NVR yozuvlari, bulut videolari ro'yxati. PTZ va `capture`
kamerada amal bajaradi — ular jonli sinalmagan.

Har funksiya birinchi argument sifatida sessiyasi bor [[CloudClient]] oladi
(`api_url`, `session`).
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import zlib
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlparse

import requests

from cloudcam.sources.cloud.vtm.image import code_hash, decrypt_image, is_encrypted

HTTP_TIMEOUT = 25
_STATUS_BATCH = 50


class CloudApiError(RuntimeError):
    """Bulut so'rovi muvaffaqiyatsiz (meta.code / resultCode)."""


def _get(client, path: str, **params) -> dict:
    return _check(client.session.get(f"https://{client.api_url}{path}",
                                     params=params, timeout=HTTP_TIMEOUT), path)


def _check(resp, path: str) -> dict:
    try:
        data = resp.json()
    except ValueError as err:
        raise CloudApiError(f"{path}: HTTP {resp.status_code}, not JSON") from err
    meta = (data.get("meta") or {}).get("code") if isinstance(data, dict) else None
    rc = data.get("resultCode") if isinstance(data, dict) else None
    ok = str(meta) == "200" if meta is not None else str(rc) == "0"
    if not ok:
        raise CloudApiError(f"{path}: code {meta if meta is not None else rc}")
    return data


# ---- qurilma holati va disklar -------------------------------------------

@dataclass(frozen=True)
class Disk:
    slot: int                       # 1 dan
    state: str                      # "0" — normal; boshqa raqam — muammo kodi
    health: int | None              # -1/None — noma'lum
    capacity_mb: int | None

    @property
    def ok(self) -> bool:
        return self.state == "0"


@dataclass(frozen=True)
class DeviceStatus:
    serial: str
    online: bool | None
    encrypted: bool | None          # video shifrlash yoqilganmi
    privacy: bool                   # "maxfiylik" rejimi (oqim bermaydi)
    disks: tuple[Disk, ...]
    raw: dict = field(repr=False, compare=False, default_factory=dict)

    @property
    def disks_ok(self) -> bool:
        return all(d.ok for d in self.disks)

    def code_matches(self, code: str) -> bool | None:
        """Tasdiqlash kodi qurilmadagisiga mosmi — oqim OCHMASDAN.

        Bulut `encryptPwd` da md5(md5(kod)) beradi (hodisa rasmlari sarlavhasi
        bilan bir xil formula). Ma'lumot bo'lmasa None.
        DIQQAT: formula jonli kamerada hali tasdiqlanmagan — qaror uchun emas,
        maslahat sifatida ishlating."""
        h = self.raw.get("encryptPwd")
        if not h or not code:
            return None
        return code_hash(code) == str(h).lower()


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_disks(raw: dict) -> tuple[Disk, ...]:
    opt = raw.get("optionals") or {}
    states = str(raw.get("diskState") or "")
    health = str(opt.get("diskHealth") or "").split(",")
    cap = str(opt.get("diskCapacity") or "").split(",")
    disks = []
    for i, ch in enumerate(states):
        if ch == "-":                     # slotda disk yo'q
            continue
        h = _int(health[i]) if i < len(health) else None
        c = _int(cap[i]) if i < len(cap) else None
        disks.append(Disk(slot=i + 1, state=ch,
                          health=None if h in (None, -1) else h,
                          capacity_mb=None if c in (None, -1) else c))
    return tuple(disks)


def parse_status(serial: str, raw: dict) -> DeviceStatus:
    opt = raw.get("optionals") or {}
    online = opt.get("OnlineStatus")
    enc = raw.get("isEncrypt")
    return DeviceStatus(
        serial=serial,
        online=None if online is None else str(online) == "1",
        encrypted=None if enc is None else str(enc) == "1",
        privacy=str(raw.get("privacyStatus") or "0") == "1",
        disks=_parse_disks(raw),
        raw=raw,
    )


def device_status(client, serials: Iterable[str]) -> dict[str, DeviceStatus]:
    """Qurilmalar holati (online, disklar, shifrlash) — bir so'rovda ko'p qurilma.

    Holat QURILMA darajasida: NVR online bo'lsa ham undagi alohida kanal
    (kamera) uzilgan bo'lishi mumkin."""
    want = sorted({str(s) for s in serials if s})
    out: dict[str, DeviceStatus] = {}
    for i in range(0, len(want), _STATUS_BATCH):
        part = want[i:i + _STATUS_BATCH]
        data = _get(client, "/v3/userdevices/v1/devices/status",
                    deviceSerials=",".join(part))
        for serial, raw in (data.get("statusInfos") or {}).items():
            if isinstance(raw, dict):
                out[serial] = parse_status(serial, raw)
    return out


# ---- hodisalar (alarm / xabarlar) ----------------------------------------

@dataclass(frozen=True)
class Event:
    serial: str
    channel: int | None
    time: dt.datetime | None        # mahalliy vaqt (naive), bilinsa
    time_str: str
    kind: str                       # alarm turi kodi (masalan "10000")
    title: str
    picture_url: str | None
    encrypted: bool | None
    msg_id: str | None
    raw: dict = field(repr=False, compare=False, default_factory=dict)


def _epoch(v) -> dt.datetime | None:
    try:
        ts = float(v)
    except (TypeError, ValueError):
        return None
    if ts > 1e11:                    # millisekund
        ts /= 1000.0
    try:
        return dt.datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError):
        return None


def _first_str(*vals) -> str | None:
    return next((v for v in vals if isinstance(v, str) and v), None)


def _event_from_message(m: dict) -> Event:
    """`/v3/unifiedmsg/list` elementi (pyezvizapi `_normalize_unified_message`)."""
    ext = m.get("ext") if isinstance(m.get("ext"), dict) else {}
    pics = ext.get("pics")
    multi = next((p for p in pics.split(";") if p), None) if isinstance(pics, str) else None
    crypt = m.get("picCrypt", ext.get("picCrypt"))
    return Event(
        serial=str(m.get("deviceSerial") or ""),
        channel=_int(m.get("channel")),
        time=_epoch(m.get("time")),
        time_str=str(m.get("timeStr") or ext.get("alarmStartTime") or ""),
        kind=str(ext.get("alarmType") or m.get("subType") or ""),
        title=str(m.get("title") or m.get("detail") or m.get("sampleName") or ""),
        picture_url=_first_str(m.get("pic"), multi, m.get("defaultPic")),
        encrypted=None if crypt is None else str(crypt) not in ("0", ""),
        msg_id=_first_str(m.get("msgId")),
        raw=m,
    )


def _event_from_alarm(a: dict) -> Event:
    """`/v3/alarms/v2/advanced` elementi (eski "alarm" shakli)."""
    crypt = a.get("isEncrypt", a.get("picCrypt"))
    return Event(
        serial=str(a.get("deviceSerial") or ""),
        channel=_int(a.get("channelNo", a.get("channel"))),
        time=_epoch(a.get("alarmStartTime") or a.get("alarmTime")),
        time_str=str(a.get("alarmStartTimeStr") or a.get("alarmTimeStr") or ""),
        kind=str(a.get("alarmType") or ""),
        title=str(a.get("sampleName") or a.get("alarmName") or ""),
        picture_url=_first_str(a.get("picUrl"), a.get("alarmPicUrl")),
        encrypted=None if crypt is None else str(crypt) not in ("0", ""),
        msg_id=_first_str(str(a["alarmId"]) if a.get("alarmId") is not None else None),
        raw=a,
    )


DEFAULT_MESSAGE_TYPES = "92"        # ilova standarti (pyezvizapi DEFAULT_UNIFIEDMSG_STYPE)


def messages(client, serials: Iterable[str] | None = None, *, limit: int = 20,
             date: dt.date | str | None = None, cursor: str = "",
             types: str = DEFAULT_MESSAGE_TYPES) -> tuple[list[Event], str | None]:
    """Hodisalar lentasi (yangilari birinchi). Qaytaradi: (hodisalar, keyingi_kursor).

    `date=None` — sanaga bog'lamasdan oxirgilari. Keyingi sahifa uchun qaytgan
    kursorni `cursor` ga bering (None — boshqa sahifa yo'q)."""
    params: dict[str, Any] = {"stype": types, "limit": max(1, min(int(limit), 50)),
                              "endTime": cursor or ""}
    if serials:
        params["serials"] = ",".join(sorted(set(serials)))
    if date:
        params["date"] = date.strftime("%Y%m%d") if isinstance(date, dt.date) else str(date)
    data = _get(client, "/v3/unifiedmsg/list", **params)
    items = [_event_from_message(m) for m in (data.get("message") or data.get("messages") or [])
             if isinstance(m, dict)]
    nxt = items[-1].msg_id if (data.get("hasNext") and items) else None
    return items, nxt


def alarms(client, serial: str | None = None, *, limit: int = 20) -> list[Event]:
    """Eski "alarm" ro'yxati (ba'zi hisoblarda `messages` bo'sh, bu esa to'la)."""
    params: dict[str, Any] = {"queryType": -1, "limit": max(1, int(limit)), "stype": -1}
    if serial:
        params["deviceSerials"] = serial
    data = _get(client, "/v3/alarms/v2/advanced", **params)
    return [_event_from_alarm(a) for a in (data.get("alarms") or []) if isinstance(a, dict)]


def download_image(url: str, code: str | None = None, *, timeout: float = HTTP_TIMEOUT) -> bytes:
    """Hodisa rasmini yuklaydi va kerak bo'lsa tasdiqlash kodi bilan ochadi.

    Sessiya sarlavhalari (`sessionId`) YUBORILMAYDI — rasm havolasi tayyor
    imzolangan URL va istalgan domenda bo'lishi mumkin. Shifrlangan rasm kodsiz
    so'ralsa `ValueError`; xato kodda [[vtm.image.ImageKeyError]]."""
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError(f"Unsupported image URL: {url!r}")
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    data = r.content
    if not is_encrypted(data):
        return data
    if not code:
        raise ValueError("Image is encrypted — a verification code is required")
    return decrypt_image(data, code)


# ---- yozuvlar (SD karta / NVR) va bulut videolari -------------------------

@dataclass(frozen=True)
class Record:
    start: dt.datetime
    end: dt.datetime
    type: int | None                # 1 — doimiy yozuv; boshqalari — hodisa turi

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def _decode_records(value) -> list:
    """Yozuvlar ro'yxati base64+zlib JSON bo'lib keladi (yoki oddiy ro'yxat)."""
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value:
        return []
    try:
        out = json.loads(zlib.decompress(base64.b64decode(value, validate=True)).decode())
    except (ValueError, zlib.error, UnicodeDecodeError):
        return []
    return out if isinstance(out, list) else []


def _parse_time(v) -> dt.datetime | None:
    if not isinstance(v, str):
        return _epoch(v)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y%m%dT%H%M%SZ"):
        try:
            return dt.datetime.strptime(v, fmt)
        except ValueError:
            pass
    return None


def records(client, serial: str, channel: int, start: dt.datetime, end: dt.datetime,
            *, page_size: int = 200, max_pages: int = 50) -> list[Record]:
    """Qurilmadagi (SD karta / NVR diski) yozuv bo'laklari, vaqt bo'yicha.

    Vaqt — qurilma soati bo'yicha mahalliy vaqt. Bulut bir sahifada `page_size`
    tagacha beradi; `isFinished` bo'lmaguncha oxirgi bo'lak oxiridan davom etiladi."""
    out: list[Record] = []
    cur = start
    for _ in range(max_pages):
        data = _get(client, "/v3/streaming/v2/records", deviceSerial=serial,
                    channelNo=channel, startTime=cur.strftime("%Y-%m-%d %H:%M:%S"),
                    stopTime=end.strftime("%Y-%m-%d %H:%M:%S"), size=page_size,
                    sortBy=0, requireLabel=0)
        page = []
        for item in _decode_records(data.get("records")):
            if not isinstance(item, dict):
                continue
            b = _parse_time(item.get("B", item.get("startTime")))
            e = _parse_time(item.get("E", item.get("stopTime")))
            if b and e:
                page.append(Record(b, e, _int(item.get("Type", item.get("type")))))
        new = [r for r in page if not out or r.start >= out[-1].end]
        out.extend(new)
        if str(data.get("isFinished")) == "1" or not new or len(page) < page_size:
            break
        cur = out[-1].end
    return out


def cloud_videos(client, serial: str, channel: int, *, limit: int = 20) -> list[dict]:
    """Bulut xotirasidagi (obuna bo'lsa) video yozuvlar tavsifi."""
    data = _get(client, "/v3/clouds/videos/list", deviceSerial=serial, channelNo=channel,
                limit=limit, videoType=2, supportMultiChannelSharedService=0)
    v = data.get("videos")
    return v if isinstance(v, list) else []


# ---- boshqaruv: PTZ va kadr olish -----------------------------------------

PTZ_COMMANDS = frozenset({"UP", "DOWN", "LEFT", "RIGHT", "UPLEFT", "UPRIGHT", "DOWNLEFT",
                          "DOWNRIGHT", "ZOOMIN", "ZOOMOUT", "FOCUSNEAR", "FOCUSFAR"})
PTZ_ACTIONS = frozenset({"START", "STOP"})


def ptz(client, serial: str, command: str, action: str, *, channel: int = 1,
        speed: int = 5) -> None:
    """PTZ harakati. `action="START"` boshlaydi, `"STOP"` to'xtatadi.

    pyezvizapi `channelNo` ni doim 1 yuboradi — NVR kanallari uchun bu yerda
    `channel` uzatiladi."""
    import uuid
    command, action = command.upper(), action.upper()
    if command not in PTZ_COMMANDS:
        raise ValueError(f"Unknown PTZ command: {command}")
    if action not in PTZ_ACTIONS:
        raise ValueError(f"Unknown PTZ action: {action}")
    if not 1 <= int(speed) <= 7:
        raise ValueError("PTZ speed must be 1..7")
    path = f"/v3/devices/{serial}/ptzControl"
    _check(client.session.put(f"https://{client.api_url}{path}", data={
        "command": command, "action": action, "channelNo": int(channel),
        "speed": int(speed), "uuid": str(uuid.uuid4()), "serial": serial,
    }, timeout=HTTP_TIMEOUT), path)


def capture(client, serial: str, channel: int = 1) -> dict:
    """Kameraga kadr olishni buyuradi (natija bulut hodisalarida paydo bo'ladi)."""
    path = f"/v3/devconfig/v1/{serial}/{channel}/capture"
    return _check(client.session.put(f"https://{client.api_url}{path}",
                                     timeout=HTTP_TIMEOUT), path)
