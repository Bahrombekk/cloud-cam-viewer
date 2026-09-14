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
from typing import Callable, Iterator, Optional

from .client import CloudClient
from .settings import Settings, set_active
from .stream_manager import StreamManager, load_cam_keys


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

    # ── hisob ────────────────────────────────────────────────────────
    def login(self, *, force: bool = False) -> "CloudCam":
        """Sessiyani tayyorlaydi.

        Standart holatda TO'G'RIDAN-TO'G'RI login QILINMAYDI: avval saqlangan
        token, keyin refresh, va faqat oxirida parol bilan kirish
        ([[CloudClient.connect]]). Har ishga tushirishda login qilish bulutda
        CAPTCHA va "yangi qurilma" 2FA sini keltirib chiqaradi.

        `force=True` — eski xatti-harakat (majburiy to'liq login).
        """
        if force:
            self.client.login()
            self.client.save_token(self.settings.token_file)
            self.auth_mode = "login"
        else:
            self.auth_mode = self.client.connect(self.settings.token_file)
        self._logged_in = True
        return self

    def refresh_token(self) -> None:
        self.client.refresh_session()
        self.client.save_token(self.settings.token_file)

    # ── tasdiqlash kodlari ───────────────────────────────────────────
    def fetch_keys(self, serials=None, *, mfa_code=None, overwrite=False):
        """Kameralarning tasdiqlash kodlarini BULUTDAN olib saqlaydi.

        `serials=None` -> hisobdagi hamma qurilma. Natija [[keys.FetchResult]];
        `needs_mfa` bo'lsa `send_key_2fa()` chaqirib, emaildagi kodni
        `mfa_code` bilan qayta chaqiring."""
        from . import keys as _keys
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
        from . import decrypt_proxy
        return [Camera(serial, ch, name)
                for serial, ch, name in decrypt_proxy.list_cameras(self.client)]

    def open(self, serial: str, channel: int = 1, *, decrypt: bool = True,
             key: Optional[str] = None, width: Optional[int] = None,
             height: Optional[int] = None, auto_key: bool = True) -> Stream:
        """Kamerani ochadi (fon threadda ulanadi). `Stream` qaytaradi.

        key=None -> `cam_keys.json` dan olinadi; u yerda ham bo'lmasa va
        `auto_key` yoqilgan bo'lsa BULUTDAN so'raladi ([[keys.fetch]]).
        Bulut 2FA talab qilsa jimgina "AUTO" ga qaytamiz — oqimni ochish
        to'xtab qolmasligi kerak; kodni `fetch_keys()` bilan ataylab oling.
        """
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
        if key is None:
            key = "AUTO"
        cs = self._mgr.add(serial, channel=channel, decrypt=decrypt,
                           width=width, height=height, key=key)
        return Stream(cs)

    def close(self) -> None:
        self._mgr.stop_all()

    # kontekst-menejer: `with CloudCam(...) as cam:`
    def __enter__(self) -> "CloudCam":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
