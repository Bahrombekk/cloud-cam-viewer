"""cloudcam — yuqori darajali kutubxona API.

Config modulisiz, dastur ichidan ishlatish uchun. Ko'rish (OpenCV) shart
emas — xom kadr, JPEG surat yoki holat oling.

    from cloudcam import CloudCam

    cam = CloudCam(email="...", password="...", platform="hikconnect")
    cam.login()

    for c in cam.cameras():
        print(c.serial, c.channel, c.name)

    stream = cam.open(serial, channel=1)      # shifrli bo'lsa kalit avtomatik
    img = stream.snapshot()                    # JPEG bytes
    for frame in stream.frames():              # numpy BGR generator
        ...
    stream.close()
    cam.close()

Kalitlar: shifrlangan kameraning "tasdiqlash kodi" `cam_keys.json` da
saqlangan bo'lsa avtomatik olinadi; yoki `open(..., key="ABCDEF")`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator, Optional

from cloudcam.sources.cloud.client import CloudClient
from cloudcam.settings import Settings, set_active
from cloudcam.stream_manager import StreamManager, load_cam_keys


@dataclass(frozen=True)
class Camera:
    """Kamera/NVR-kanal tavsifi (ko'rsatish uchun, oqim emas)."""
    serial: str
    channel: int
    name: str


class Stream:
    """Bitta kamera oqimi — xom kadr, surat, holat. `CloudCam.open()` qaytaradi."""

    def __init__(self, cam_stream):
        self._s = cam_stream

    # ── kadr ─────────────────────────────────────────────────────────
    def frame(self):
        """Oxirgi kadr (numpy BGR) yoki None (hali kelmagan)."""
        return self._s.get_frame()

    def frames(self, *, poll: float = 0.03, timeout: Optional[float] = None
               ) -> Iterator["object"]:
        """Kelgan har yangi kadrni beruvchi generator (numpy BGR).

        `timeout` — shuncha soniya kadr kelmasa to'xtaydi (None — cheksiz).
        Oqim `close()` qilinsa ham to'xtaydi.
        """
        last_id = None
        idle = 0.0
        while self._s.running:
            frame = self._s.get_frame()
            fid = self._s.last_frame_time
            if frame is not None and fid != last_id:
                last_id = fid
                idle = 0.0
                yield frame
            else:
                time.sleep(poll)
                idle += poll
                if timeout is not None and idle >= timeout:
                    return

    def snapshot(self, fmt: str = "jpeg", quality: int = 85):
        """Joriy kadrdan surat.

        fmt="jpeg" -> JPEG bytes (yoki None); fmt="numpy" -> BGR massiv.
        JPEG uchun OpenCV kerak (ko'rish bilan bir xil bog'liqlik).
        """
        frame = self._s.get_frame()
        if frame is None:
            return None
        if fmt == "numpy":
            return frame
        import cv2  # kech import — yadro cv2'siz yuklanadi
        ok, buf = cv2.imencode(".jpg", frame,
                               [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
        return buf.tobytes() if ok else None

    def wait_first_frame(self, timeout: float = 15.0) -> bool:
        """Birinchi kadr kelguncha kutadi (True — keldi, False — timeout)."""
        end = time.time() + timeout
        while time.time() < end:
            if self._s.get_frame() is not None:
                return True
            time.sleep(0.1)
        return False

    # ── holat ────────────────────────────────────────────────────────
    def status(self) -> dict:
        return self._s.status()

    @property
    def connected(self) -> bool:
        return self._s.status()["connected"]

    @property
    def fps(self) -> float:
        return self._s.fps

    @property
    def local_url(self) -> str:
        """Proxy chiqaradigan lokal MPEG-TS manzili (ffmpeg/VLC uchun)."""
        return f"http://127.0.0.1:{self._s.port}/{self._s.serial}.ts"

    def close(self) -> None:
        self._s.stop()


class CloudCam:
    """Bulut hisobiga kirish va kameralarni ochish — yuqori darajali fasad."""

    def __init__(self, email: Optional[str] = None, password: Optional[str] = None,
                 *, platform: Optional[str] = None,
                 settings: Optional[Settings] = None, **kw):
        # Sozlama: berilgan `settings`, yoki parametrlar/muhit/config'dан.
        s = settings or Settings.resolve()
        if email is not None:
            s = s.replace(email=email)
        if password is not None:
            s = s.replace(password=password)
        if platform is not None:
            s = s.replace(platform=platform)
        if kw:
            s = s.replace(**kw)
        self.settings = s
        set_active(s)                       # proxy/stream_manager shuni o'qiydi

        self.client = CloudClient(s.email, s.password, s.region, platform=s.platform)
        self._mgr = StreamManager(client=self.client, settings=s)
        self._logged_in = False
        self.auth_mode: Optional[str] = None   # "resumed" | "refreshed" | "login"
        self._key_fetch_blocked = False        # 2FA ko'tarilmagan -> avto-olishni to'xtatamiz
        self._isup = None                      # kech yaratiladi ([[isup]])
        self._warmer = None                    # VTM keshini fonda issiq tutadi
        self.migrated: list[str] = []          # login() eski fayllarni ko'chirgan bo'lsa

    def _start_cache_warmer(self) -> None:
        """Hisob metama'lumot keshini fonda issiq tutish ([[vtm_cache.Warmer]]).

        Kesh 1 soat yashaydi; kameralar doim ochiq turmaydi, shuning uchun TTL
        ochilishlar orasida tugab, keyingi ochilish ~6s bulut so'roviga tushardi
        (central-server o'lchovi: issiq 0.14s, sovuq 6.08s). Har TTL/4 da tekshirib,
        yarmidan ko'p eskirgan bo'lsa yangilaydi — hisobga 2 ta bulut so'rovi."""
        if self._warmer is not None or not self.settings.warm_cache:
            return
        from cloudcam.sources.cloud import stream, vtm_cache
        # SHU hisob: `proxy._CACHE_KEY` import paytidagi faol sozlamadan
        # hisoblanadi — ikkinchi `CloudCam` da boshqa hisobni isitardi.
        s = self.settings
        self._warmer = vtm_cache.Warmer(lambda: stream._make_client(s),
                                        vtm_cache.account_key(s.token_file)).start()

    # ── hisob ────────────────────────────────────────────────────────
    def login(self, *, force: bool = False) -> "CloudCam":
        """Sessiyani tayyorlaydi.

        Standart holatda TO'G'RIDAN-TO'G'RI login QILINMAYDI: avval saqlangan
        token, keyin refresh, va faqat oxirida parol bilan kirish
        ([[CloudClient.connect]]). Har ishga tushirishda login qilish bulutda
        CAPTCHA va "yangi qurilma" 2FA sini keltirib chiqaradi.

        `force=True` — eski xatti-harakat (majburiy to'liq login).
        """
        # Eski versiya sessiyani JORIY papkada saqlardi — yangi standart joyga
        # nusxalaymiz, aks holda qayta login + "yangi qurilma" 2FA bo'lardi.
        from cloudcam.core import paths
        self.migrated = paths.migrate_legacy(self.settings)
        if force:
            from cloudcam.core.fsutil import file_lock
            with file_lock(self.settings.token_file):
                self.client.login()
                self.client.save_token(self.settings.token_file)
            self.auth_mode = "login"
        else:
            self.auth_mode = self.client.connect(self.settings.token_file)
        self._logged_in = True
        self._start_cache_warmer()
        return self

    def refresh_token(self) -> None:
        from cloudcam.core.fsutil import file_lock
        with file_lock(self.settings.token_file):
            self.client.refresh_session()
            self.client.save_token(self.settings.token_file)

    # ── tasdiqlash kodlari ───────────────────────────────────────────
    def fetch_keys(self, serials=None, *, mfa_code=None, overwrite=False):
        """Kameralarning tasdiqlash kodlarini BULUTDAN olib saqlaydi.

        `serials=None` -> hisobdagi hamma qurilma. Natija [[keys.FetchResult]];
        `needs_mfa` bo'lsa `send_key_2fa()` chaqirib, emaildagi kodni
        `mfa_code` bilan qayta chaqiring."""
        from cloudcam.sources.cloud import keys as _keys
        if serials is None:
            serials = sorted({c.serial for c in self.cameras()})
        res = _keys.fetch(self.client, serials, mfa_code=mfa_code,
                          overwrite=overwrite, path=self.settings.camkey_file)
        if mfa_code and not res.needs_mfa:
            self._key_fetch_blocked = False    # sessiya ko'tarildi
        return res

    def send_key_2fa(self):
        """Tasdiqlash kodlarini olish uchun 2FA kodini emailga yuboradi."""
        return self.client.send_verification_2fa()

    def start_token_refresh(self, interval: int = 3600) -> None:
        """Fon threadda davriy token yangilash (24/7 uchun)."""
        self._mgr.start_token_refresh(interval=interval)

    # ── kameralar ────────────────────────────────────────────────────
    def cameras(self) -> list[Camera]:
        """Kamera ulangan kanallar ro'yxati (bo'sh NVR slotlari emas)."""
        from cloudcam.sources.cloud import devices, vtm_cache
        # Shu hisob keshidan foydalanadi (va uni to'ldiradi) — keyingi `open()`
        # pagelist'ni bulutdan qayta so'ramaydi ([[vtm_cache.tag]]).
        vtm_cache.tag(self.client, self.settings.token_file)
        return [Camera(serial, ch, name)
                for serial, ch, name in devices.list_cameras(self.client)]

    def open(self, serial: str, channel: int = 1, *, decrypt: bool = True,
             key: Optional[str] = None, width: Optional[int] = None,
             height: Optional[int] = None, auto_key: bool = True) -> Stream:
        """Kamerani ochadi (fon threadda ulanadi). `Stream` qaytaradi.

        key=None -> `cam_keys.json` dan olinadi; u yerda ham bo'lmasa va
        `auto_key` yoqilgan bo'lsa BULUTDAN so'raladi ([[keys.fetch]]).
        Bulut 2FA talab qilsa jimgina "AUTO" ga qaytamiz — oqimni ochish
        to'xtab qolmasligi kerak; kodni `fetch_keys()` bilan ataylab oling.
        """
        key = self._key_for(serial, key, auto_key)
        cs = self._mgr.add(serial, channel=channel, decrypt=decrypt,
                           width=width, height=height, key=key)
        return Stream(cs)

    def _key_for(self, serial: str, key: Optional[str], auto_key: bool) -> str:
        """Kod: berilgani -> `cam_keys.json` -> (ruxsat bo'lsa) bulutdan -> "AUTO"."""
        if key is None:
            key = load_cam_keys(self.settings.camkey_file).get(serial)
        if key is None and auto_key and not self._key_fetch_blocked:
            try:
                got = self.fetch_keys([serial])
                key = got.fetched.get(serial)
                if got.needs_mfa:
                    # 2FA ko'tarilmagan — qolgan kameralar uchun urinish ham
                    # xuddi shunday tugaydi. 32 kameralik hisobda bu 32 ta
                    # befoyda so'rov; bir marta bilib, to'xtaymiz.
                    self._key_fetch_blocked = True
            except Exception:
                key = None
        return key or "AUTO"

    def publish(self, serial: str, target: str, channel: int = 1, *,
                key: Optional[str] = None, stream_mode: Optional[str] = None,
                auto_key: bool = True, proxy_port: Optional[int] = None) -> Stream:
        """Kamerani RTSP serverga (MediaMTX, go2rtc, ...) TRANSKODSIZ publish qiladi.

        `target` — masalan `rtsp://127.0.0.1:8554/cam/AB1234567_1`. Fon thread'da
        ishlaydi va uzilsa qayta ulanadi; `Stream.status()` holatni beradi,
        `frame()` doim None (kadr dekodlanmaydi). `stream_mode` — "main" | "sub" |
        "auto"; berilmasa sozlamadagisi ([[cloudcam.publisher]]). `proxy_port=0` —
        dekodlovchi proxy uchun OS bergan bo'sh port."""
        key = self._key_for(serial, key, auto_key)
        s = self.settings
        if stream_mode is not None:
            if stream_mode not in ("main", "sub", "auto"):
                raise ValueError(f"stream_mode must be main, sub or auto, got {stream_mode!r}")
            s = s.replace(stream_mode=stream_mode, substream=False)
        pub = self._mgr.publish(serial, channel, target, key=key, settings=s,
                                port=proxy_port)
        return Stream(pub)

    # ── holat, hodisalar, yozuvlar, PTZ ([[cloudcam.sources.cloud.rest]]) ─────
    def status(self, serials=None) -> dict:
        """{serial: DeviceStatus} — online, disklar, shifrlash. Bitta so'rov.

        `serials=None` -> hisobdagi hamma qurilma. Holat QURILMA darajasida
        (NVR online bo'lsa ham undagi alohida kamera uzilgan bo'lishi mumkin)."""
        from cloudcam.sources.cloud import rest as cloud_api
        if serials is None:
            serials = {c.serial for c in self.cameras()}
        return cloud_api.device_status(self.client, serials)

    def check(self, cameras=None) -> dict:
        """{(serial, kanal): "ok" | "nokey" | "offline" | "unknown"} — HAQIQIY holat.

        [[status]] qurilma (NVR) darajasida; bu esa har kanalni bulutdan tez
        so'raydi ([[stream.probe_many]], ffmpeg'siz) va shifrlangan qurilmaning
        kodi yo'q bo'lsa "nokey" beradi."""
        from cloudcam.sources.cloud.stream import classify, probe_many
        cams = list(cameras if cameras is not None else self.cameras())
        live = probe_many(self.client, [(c.serial, c.channel) for c in cams])
        try:
            enc = {s for s, st in self.status({c.serial for c in cams}).items()
                   if st.encrypted}
        except Exception:
            enc = None                    # noma'lum -> onlayn kanallar "unknown"
        return classify(live, enc, load_cam_keys(self.settings.camkey_file))

    def events(self, serials=None, *, limit: int = 20, cursor: str = ""):
        """Oxirgi hodisalar (harakat, odam, ...): (hodisalar, keyingi_kursor)."""
        from cloudcam.sources.cloud import rest as cloud_api
        return cloud_api.messages(self.client, serials, limit=limit, cursor=cursor)

    def event_image(self, event) -> bytes:
        """Hodisa rasmi; shifrlangan bo'lsa `cam_keys.json` dagi kod bilan ochiladi."""
        from cloudcam.sources.cloud import rest as cloud_api
        if not event.picture_url:
            raise ValueError("Event has no picture")
        code = load_cam_keys(self.settings.camkey_file).get(event.serial)
        return cloud_api.download_image(event.picture_url,
                                        None if code in (None, "AUTO") else code)

    def records(self, serial: str, channel: int = 1, start=None, end=None):
        """Qurilma (SD/NVR) yozuvlari: [Record(start, end, type)]. Standart — oxirgi 24 soat."""
        import datetime as _dt
        from cloudcam.sources.cloud import rest as cloud_api
        end = end or _dt.datetime.now()
        start = start or end - _dt.timedelta(days=1)
        return cloud_api.records(self.client, serial, channel, start, end)

    def ptz(self, serial: str, command: str, *, channel: int = 1, speed: int = 5,
            duration: float = 0.5) -> None:
        """PTZ: `command` (UP, DOWN, LEFT, RIGHT, ZOOMIN, ...) `duration` soniya."""
        from cloudcam.sources.cloud import rest as cloud_api
        cloud_api.ptz(self.client, serial, command, "START", channel=channel, speed=speed)
        try:
            time.sleep(max(0.0, duration))
        finally:
            cloud_api.ptz(self.client, serial, command, "STOP", channel=channel, speed=speed)

    def push(self, on_event):
        """Push hodisalari (MQTT): `with cam.push(print): ...`. `cloudcam[events]` kerak."""
        from cloudcam.sources.cloud.push import PushClient
        return PushClient(self.client, on_event)

    # ── ISUP 5.0 ([[cloudcam.sources.isup]]) ─────────────────────────────────
    @property
    def isup(self):
        """`isup-bridge` klienti (birinchi murojaatda yaratiladi).

        Bulut hisobi SHART EMAS — ISUP qurilmalari bulutdan mustaqil."""
        if self._isup is None:
            from cloudcam.sources import isup as _isup
            self._isup = _isup.from_settings(self.settings)
        return self._isup

    def isup_cameras(self) -> list[Camera]:
        """Ko'prikka ulangan ISUP qurilmalarining kanallari."""
        out = []
        for dev in self.isup.devices():
            for ch in dev.channels:
                name = f"{dev.id} ch{ch}" + (f" ({dev.ip})" if dev.ip else "")
                out.append(Camera(dev.id, ch, name))
        return out

    def open_isup(self, device_id: str, channel: int = 1, *, sub: bool = False,
                  width: Optional[int] = None, height: Optional[int] = None,
                  want: bool = True) -> Stream:
        """ISUP kamerasini ochadi (bulutsiz, deshifrsiz).

        `want=True` — ko'prikdan shu oqimni ochishni so'raydi. Asosiy oqim
        ~4 Mbit/s, substream ~0.3 Mbit/s, shuning uchun ko'prik oqimni faqat
        so'ralganda ochadi ([[IsupBridge.want]]); ISUP'da bu ~0.23 s.
        """
        if want:
            self.isup.want((device_id, channel, sub))
        url = self.isup.stream_url(device_id, channel, sub=sub)
        cs = self._mgr.add(device_id, channel=channel, decrypt=False,
                           width=width, height=height, url=url)
        return Stream(cs)

    def close(self) -> None:
        if self._warmer is not None:
            self._warmer.stop()
            self._warmer = None
        if self._isup is not None:
            try:
                self._isup.want(replace=True)   # ko'prik oqimlarni yopsin
            except Exception:
                pass                            # yopilishda xato ish to'xtatmasin
        self._mgr.stop_all()

    # kontekst-menejer: `with CloudCam(...) as cam:`
    def __enter__(self) -> "CloudCam":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
