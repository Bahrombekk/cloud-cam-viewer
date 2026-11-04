"""
EZVIZ / Hik-Connect bulut klienti.

Android ilova sifatida ulanadi (terminal limit, code 1069 ni chetlab o'tadi).

Sessiya boshqaruvi uch qoidaga tayanadi — uchalasi ham boshqa loyihalarda
o'zini oqlagan (`ezviz-ha-addon`, `hikconnect`, `pyezvizapi`):

  1. `featureCode` shu O'RNATMAGA xos ([[identity]]) — qattiq yozilgan umumiy
     kod emas. Aks holda loyihaning hamma nusxasi bulut uchun BITTA terminal.
  2. Har ishga tushirishda QAYTA LOGIN QILINMAYDI. Saqlangan token sinab
     ko'riladi, kerak bo'lsa refresh qilinadi; login — oxirgi chora. Har safar
     login qilish bulutda CAPTCHA (1015) va "yangi qurilma" 2FA sini keltirib
     chiqaradi.
  3. Login muvaffaqiyatsiz bo'lsa qayta-qayta urinilmaydi — kutish 60s dan
     30 daqiqagacha o'sadi.
"""

import hashlib
import json
import re
import time

import requests

from cloudcam.core.fsutil import harden, write_private
from cloudcam.i18n import t
from cloudcam.core.identity import feature_code


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


# Login md5(parol) ni, keyingi so'rovlar esa `sessionId` ni shu domenga yuboradi.
# Domen uch joydan keladi — sozlama (`region`), bulut javobi (`loginArea.apiDomain`,
# 1100 redirect) va `token.json` — va ilgari hech biri tekshirilmasdi: token fayli
# o'zgartirilsa, parol xeshi va sessiya begona serverga ketardi. Faqat EZVIZ /
# Hik-Connect bulut domenlari (central-server bilan bir xil ro'yxat).
_DOMAIN_RE = re.compile(
    r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.(ezvizlife\.com|ezviz\.com|hik-connect\.com"
    r"|hik-connectru\.com|hikvision\.com)\Z"
)


class UnsafeDomain(ValueError):
    """Bulut domeni ruxsat etilgan ro'yxatda yo'q."""


def safe_domain(domain) -> str:
    """Domenni tekshirib, normallashtirilgan holda qaytaradi; aks holda [[UnsafeDomain]]."""
    d = str(domain or "").strip().lower()
    if d.startswith("https://"):
        d = d[len("https://"):]
    d = d.rstrip("/")
    if not _DOMAIN_RE.match(d):
        raise UnsafeDomain(t(f"Cloud domain not allowed: {domain!r}",
                              f"Ruxsat etilmagan bulut domeni: {domain!r}"))
    return d


class MfaRequired(Exception):
    """Kalit olish uchun 2FA (email) kodi kerak bo'lganda ko'tariladi."""


class SessionExpired(RuntimeError):
    """Bulut refresh tokenni RAD ETDI — faqat shu holda parol bilan qayta kiriladi.

    Tarmoq xatosi bu emas: u o'zicha ko'tariladi. Aks holda qisqa uzilish
    to'liq loginga (CAPTCHA 1015 / yangi qurilma 2FA) olib kelardi."""


class CloudClient:
    """EZVIZ / Hik-Connect bulutiga kirish va qurilmalar ro'yxati."""

    # Platforma sozlamalari: clientType va boshlang'ich domen
    PLATFORMS = {
        "hikconnect": {"client_type": "55", "domain": "api.hik-connect.com", "lang": "en-US"},
        "ezviz":      {"client_type": "1",  "domain": "apiisgp.ezvizlife.com", "lang": "en_US"},
    }

    # Sessiya shuncha soniyadan eski bo'lsa yangilanadi. `hikconnect`
    # kutubxonasi ham shu yo'ldan boradi (`is_refresh_login_needed`): davriy
    # "har soatda" taymeridan farqli ravishda, YOSHGA qarab yangilash qayta
    # ulanishdan keyin ham to'g'ri ishlaydi.
    SESSION_MAX_AGE = 1500.0
    # Login muvaffaqiyatsiz bo'lsa kutish: 60s -> ikkilanib -> 30 daqiqa.
    LOGIN_BACKOFF_MIN = 60.0
    LOGIN_BACKOFF_MAX = 1800.0

    def __init__(self, email, password, region=None, platform="hikconnect"):
        self.email = email
        self.password = password
        self.platform = platform if platform in self.PLATFORMS else "hikconnect"
        cfg = self.PLATFORMS[self.platform]
        # region berilgan bo'lsa uni, aks holda platforma standartini ishlatamiz
        self.api_url = safe_domain(region or cfg["domain"])
        self.session_id = None
        self.rf_session_id = None
        self.user_code = None
        self.session_time = 0.0      # sessiya qachon olingan (epoch) — faylga ham yoziladi
        self._login_fail_at = 0.0    # oxirgi muvaffaqiyatsiz login
        self._login_backoff = 0.0    # keyingi urinishgacha kutish
        self.token_path = None       # sessiya qaysi fayldan tiklangan / yozilgan

        self.session = requests.Session()
        self.session.headers.update(self._base_headers())

    @property
    def FEATURE_CODE(self) -> str:      # noqa: N802 — eski nom saqlanadi
        """Shu o'rnatmaning terminal kodi ([[identity.feature_code]])."""
        return feature_code()

    def _base_headers(self) -> dict:
        """Android ilovasining to'liq sarlavhalari.

        Ilgari faqat 4 tasi yuborilardi. Bulutning ba'zi endpointlari
        (ayniqsa `devconfig/*` — tasdiqlash kodi shu yerdan olinadi) to'liq
        to'plamsiz bo'sh yoki qisqartirilgan javob beradi."""
        cfg = self.PLATFORMS[self.platform]
        return {
            "clientType": cfg["client_type"],
            "lang": cfg["lang"],
            "language": cfg["lang"],
            "featureCode": self.FEATURE_CODE,
            "netType": "WIFI",
            "customno": "1000001",
            "clientNo": "web_site",
            "appId": "ys7",
            "osVersion": "",
            "clientVersion": "",
            "ssid": "",
            "User-Agent": "okhttp/3.12.1",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def login(self):
        """Hisobga kirish. Hik-Connect va EZVIZ login API'lari farq qiladi.

        Ketma-ket muvaffaqiyatsizlikda kutish oshib boradi ([[LOGIN_BACKOFF_MIN]]):
        bulut noto'g'ri parolga tez-tez urinishni bloklaydi va CAPTCHA so'raydi,
        24/7 sikl esa aks holda bir soniyada bir marta urinaverardi."""
        wait = self._login_fail_at + self._login_backoff - time.time()
        if wait > 0:
            raise RuntimeError(t(
                f"Login will be retried in {wait:.0f}s (previous attempt failed)",
                f"Login {wait:.0f}s dan keyin qayta urinib ko'riladi "
                f"(oldingi urinish muvaffaqiyatsiz)"))
        try:
            data = (self._login_ezviz() if self.platform == "ezviz"
                    else self._login_hikconnect())
        except Exception:
            self._login_fail_at = time.time()
            self._login_backoff = min(
                self.LOGIN_BACKOFF_MAX,
                max(self.LOGIN_BACKOFF_MIN, self._login_backoff * 2))
            raise
        self._login_fail_at = 0.0
        self._login_backoff = 0.0
        return data

    def _login_hikconnect(self):
        """Hik-Connect: /v2 endpoint, global domen, region redirect (1100)."""
        data = None
        for _ in range(4):
            r = self.session.post(
                f"https://{self.api_url}/v3/users/login/v2",
                data={
                    "account": self.email,
                    "password": md5(self.password),
                    "featureCode": self.FEATURE_CODE,
                },
                timeout=25,
            )
            data = r.json()
            code = data.get("meta", {}).get("code")
            if code == 1100:  # boshqa regionga yo'naltirish
                self.api_url = safe_domain(data["loginArea"]["apiDomain"])
                continue
            if code in (1013, 1014, 1226):
                raise RuntimeError(t("Login failed: wrong email or password",
                                     "Login xatosi: email yoki parol noto'g'ri"))
            if code == 1015:
                raise RuntimeError(t(
                    "CAPTCHA requested — log in to the Hik-Connect app first, then retry",
                    "CAPTCHA so'raldi — avval Hik-Connect ilovasiga kiring, keyin qayta urining"))
            break
        return self._finish_login(data)

    def _login_ezviz(self):
        """EZVIZ: /v5 endpoint, clientType 1 (terminal limitni chetlab o'tadi)."""
        r = self.session.post(
            f"https://{self.api_url}/v3/users/login/v5",
            data={
                "account": self.email,
                "password": md5(self.password),
                "featureCode": self.FEATURE_CODE,
                "msgType": 0,
                "cuName": "SSmartPhone_Android",
            },
            timeout=25,
        )
        return self._finish_login(r.json())

    def _finish_login(self, data):
        if not data or data.get("meta", {}).get("code") != 200:
            meta = data.get("meta") if data else t("no response", "javob yo`q")
            raise RuntimeError(t(f"Login failed: {meta}", f"Login xatosi: {meta}"))
        self.session_id = data["loginSession"]["sessionId"]
        self.rf_session_id = data["loginSession"]["rfSessionId"]
        self.api_url = safe_domain(data.get("loginArea", {}).get("apiDomain", self.api_url))
        self.user_code = data.get("loginUser", {}).get("userCode")
        self.session.headers.update({"sessionId": self.session_id})
        self.session_time = time.time()
        return data

    # ── sessiyani qayta ishlatish ────────────────────────────────────────
    def load_token(self, path=None) -> bool:
        """Saqlangan sessiyani fayldan tiklaydi (login QILMAYDI).

        Qaytaradi: tiklandimi. Terminal kodi boshqa bo'lsa (token boshqa
        kompyuterdan ko'chirilgan) tiklamaymiz — bulut baribir rad etadi."""
        path = path or self._default_token_file()
        harden(path)                    # eski versiya 0644 bilan yozgan bo'lishi mumkin
        try:
            with open(path, encoding="utf-8") as f:
                tok = json.load(f)
        except (OSError, ValueError):
            return False
        if not tok.get("session_id"):
            return False
        saved_fc = tok.get("feature_code")
        if saved_fc and saved_fc != self.FEATURE_CODE:
            return False
        try:
            # Sessiya shu domenga yuboriladi — token fayli begona hostga ishora
            # qilsa TIKLAMAYMIZ (parol bilan qayta kirish xavfsiz domenga boradi).
            safe_domain(tok.get("api_url") or self.api_url)
        except UnsafeDomain:
            return False
        saved_user = (tok.get("username") or "").strip().lower()
        if self.email and saved_user and saved_user != str(self.email).strip().lower():
            # Boshqa hisobning sessiyasi: uni tiklasak boshqa hisob kameralari
            # chiqadi, keyingi loginda esa eski email + yangi parol ketadi.
            return False
        saved_pf = tok.get("platform")
        if self.email and saved_pf in self.PLATFORMS and saved_pf != self.platform:
            return False                # platforma o'zgartirilgan -> yangi login
        self.token_path = path
        self.session_id = tok["session_id"]
        self.rf_session_id = tok.get("rf_session_id")
        self.api_url = safe_domain(tok.get("api_url") or self.api_url)
        self.email = tok.get("username") or self.email
        if tok.get("platform") in self.PLATFORMS:
            self.platform = tok["platform"]
            self.session.headers.update(self._base_headers())
        self.session_time = float(tok.get("session_time") or 0.0)
        self.session.headers.update({"sessionId": self.session_id})
        return True

    def is_refresh_needed(self, max_age=None) -> bool:
        """Sessiya yangilanishi kerakmi (yoshiga qarab).

        `hikconnect` dagi `is_refresh_login_needed()` bilan bir xil g'oya:
        "har soatda" degan taymer qayta ulanish/uyqudan keyin noto'g'ri
        ishlaydi, yosh esa doim to'g'ri."""
        if not self.session_id:
            return True
        age = time.time() - (self.session_time or 0.0)
        return age >= (self.SESSION_MAX_AGE if max_age is None else max_age)

    def connect(self, path=None):
        """Sessiyani TAYYOR holga keltiradi — iloji boricha login QILMASDAN.

        Tartib: saqlangan token -> (eskirgan bo'lsa) refresh -> to'liq login.
        Har ishga tushirishda to'g'ridan-to'g'ri `login()` chaqirish bulutda
        CAPTCHA (1015) va "yangi qurilma" 2FA sini keltirib chiqaradi —
        `ezviz-ha-addon` aynan shuning uchun sessiyani diskda saqlaydi."""
        path = path or self._default_token_file()
        if self.load_token(path):
            if not self.is_refresh_needed():
                return "resumed"
            try:
                self.refresh_session(allow_login=False)
                self.save_token(path)
                return "refreshed"
            except SessionExpired:
                pass                 # bulut rad etdi -> to'liq login
            # Tarmoq xatosi o'zicha ko'tariladi: parol bilan kirish ham
            # baribir o'tmasdi, faqat CAPTCHA xavfi qo'shilardi.
        self.login()
        self.save_token(path)
        return "login"

    @staticmethod
    def _default_token_file() -> str:
        from cloudcam.settings import get_active
        return get_active().token_file or "token.json"

    def refresh_session(self, allow_login=True):
        """
        Session ni refresh token bilan yangilash — parol/MFA so'ramaydi.
        24/7 ishlash uchun muhim. Muvaffaqiyatsiz bo'lsa to'liq qayta login
        qiladi (`allow_login=False` bo'lsa xato ko'taradi).
        """
        # Tarmoq xatosi (requests.RequestException) shu yerdan o'zicha chiqadi.
        r = self.session.put(
            f"https://{self.api_url}/v3/apigateway/login",
            data={
                "refreshSessionId": self.rf_session_id,
                "featureCode": self.FEATURE_CODE,
            },
            timeout=25,
        )
        try:
            data = r.json()
        except ValueError:
            data = {}
        meta = data.get("meta", {}) if isinstance(data, dict) else {}
        if meta.get("code") == 200:
            info = data.get("sessionInfo", {})
            self.session_id = info.get("sessionId", self.session_id)
            self.rf_session_id = info.get("refreshSessionId", self.rf_session_id)
            self.session.headers.update({"sessionId": self.session_id})
            self.session_time = time.time()
            return True
        # Refresh rad etildi — to'liq qayta login
        if not allow_login:
            raise SessionExpired(t(
                f"Could not refresh the session (refresh token expired, "
                f"HTTP {r.status_code}, code {meta.get('code')})",
                f"Sessiyani yangilab bo'lmadi (refresh token eskirgan, "
                f"HTTP {r.status_code}, kod {meta.get('code')})"))
        self.login()
        return True

    def get_service_urls(self):
        r = self.session.get(
            f"https://{self.api_url}/v3/configurations/system/info", timeout=25)
        return r.json().get("systemConfigInfo", {})

    def get_devices(self):
        """Barcha kameralar: {serial: {name, status, model, channels}}

        Sahifalab oladi — bulut bir so'rovda ko'pi bilan 50 ta qurilma beradi,
        avval 50 dan ortig'i jimgina tushib qolardi."""
        result = {}
        offset = 0
        for _ in range(40):          # 40 x 50 = 2000 qurilma — cheksiz tsikldan himoya
            r = self.session.get(
                f"https://{self.api_url}/v3/userdevices/v1/devices/pagelist",
                params={
                    "filter": "CLOUD,CONNECTION,SWITCH,STATUS,WIFI,NODISTURB,"
                              "P2P,CHANNEL,VTM,FEATURE,UPGRADE,VIDEO_QUALITY,QOS",
                    "groupId": -1, "limit": 50, "offset": offset,
                },
                timeout=25,
            )
            data = r.json()
            devs = data.get("deviceInfos") or []
            for dev in devs:
                serial = dev.get("deviceSerial")
                if serial:
                    result[serial] = {
                        "name": dev.get("name", serial),
                        "status": dev.get("status", 0),
                        "model": dev.get("deviceType", ""),
                        "channels": dev.get("channelNumber", 1),
                    }
            page = data.get("page") or {}
            if not devs or not page.get("hasNext"):
                break
            offset += len(devs)
        return result

    def send_mfa_code(self):
        """
        2FA (elevation) kodini emailga yuborish — get_cam_key 20002 qaytarsa kerak.
        pyezvizapi kutubxonasi bilan bir xil endpoint.
        """
        r = self.session.post(
            f"https://{self.api_url}/v3/sms/nologin/checkcode",
            data={"from": self.email, "bizType": "TERMINAL_BIND"},
            timeout=25,
        )
        return r.json()

    def get_cam_key(self, serial, channel=1, mfa_code=None):
        """
        Kamera shifr kalitini (encryptkey) olish.

        EZVIZ buni 2FA "elevation" bilan himoyalaydi:
          - resultCode 0     -> kalit qaytadi
          - resultCode 20002 -> 2FA kod kerak (MfaRequired ko'tariladi)
          - resultCode 2009  -> qurilma ulanmagan

        Muhim: mfa_code bilan muvaffaqiyatli chaqiruv SESSIYANI "elevate" qiladi —
        shundan keyin save_token() bilan saqlangan token orqali proxy --decrypt-video
        kalitni o'zi (kodsiz) ola oladi.
        """
        payload = {
            "checkcode": mfa_code,
            "serial": serial,
            "clientNo": "web_site",
            "clientType": 3,
            "netType": "WIFI",
            "featureCode": self.FEATURE_CODE,
            "sessionId": self.session_id,
        }
        r = self.session.post(
            f"https://{self.api_url}/api/device/query/encryptkey",
            data=payload, timeout=25,
        )
        data = r.json()
        code = str(data.get("resultCode"))
        if code == "0":
            return data.get("encryptkey")
        if code == "20002":
            raise MfaRequired(t("2FA code required (resultCode 20002)",
                                "2FA kod kerak (resultCode 20002)"))
        if code == "10001":
            raise RuntimeError(t("Repeated request — wait 1-2 minutes and retry (10001)",
                                 "Takroriy so'rov — 1-2 daqiqa kutib qayta urining (10001)"))
        if code == "2009":
            raise RuntimeError(t("Device offline (2009)", "Qurilma ulanmagan (2009)"))
        # Xitoycha resultDes konsolda crash bermasligi uchun faqat kod ko'rsatamiz
        raise RuntimeError(t(f"Could not get the key (resultCode {code})",
                             f"Kalitni ololmadim (resultCode {code})"))

    # ── tasdiqlash kodi (verification code) ──────────────────────────────
    def send_verification_2fa(self, biz_type="DEVICE_AUTH_CODE"):
        """Tasdiqlash kodini olish uchun 2FA kodini emailga yuboradi.

        Javobdagi `contact.fuzzyContact` — kod qaysi manzilga ketgani."""
        r = self.session.post(
            f"https://{self.api_url}/v3/users/checkcode/mt",
            data={"bizType": biz_type, "from": self.email},
            timeout=25,
        )
        return r.json()

    def get_verification_code(self, serial, mfa_code=None):
        """Kameraning TASDIQLASH KODINI bulutdan oladi (yorliqdagi kod).

        Aynan shu kod — oqimni ochuvchi AES kaliti. Ilgari uni qo'lda
        `set_code.py` bilan kiritish kerak edi; bulut esa uni o'zi beradi
        (`pyezvizapi.get_cam_auth_code` shu endpointni ishlatadi).

        Birinchi chaqiruvda bulut 2FA so'raydi (meta.code 80000) ->
        `MfaRequired`. U holda `send_verification_2fa()` bilan kod yuboring
        va emaildagi kodni `mfa_code` da qaytaring. Muvaffaqiyatli 2FA
        SESSIYANI ko'taradi — qolgan kameralar uchun kod kerak emas.
        """
        params = {
            "encrptPwd": None,
            "msgAuthCode": mfa_code,
            "senderType": 3 if mfa_code else 0,
        }
        r = self.session.get(
            f"https://{self.api_url}/v3/devconfig/authcode/query/{serial}",
            params={k: v for k, v in params.items() if v is not None},
            timeout=25,
        )
        data = r.json()
        code = data.get("meta", {}).get("code")
        if code == 200 and data.get("devAuthCode"):
            return str(data["devAuthCode"]).strip().upper()
        if code == 80000:
            raise MfaRequired(t("2FA code required (meta.code 80000)",
                                "2FA kod kerak (meta.code 80000)"))
        if code == 2009:
            raise RuntimeError(t("Device offline (2009)", "Qurilma ulanmagan (2009)"))
        raise RuntimeError(t(f"Could not get the verification code (meta.code {code})",
                             f"Tasdiqlash kodini ololmadim (meta.code {code})"))

    def refresh_and_save(self, path=None) -> str:
        """401 dan keyin: sessiyani yangilab DARHOL faylga yozadi (parolsiz).

        Bir token faylini ko'r jarayon (ko'ruvchi, CLI, har kamera proxy'si)
        bo'lishadi. Refresh tokeni aylanadi — yangilangan sessiya yozilmasa
        fayldagi eski refresh token o'lik bo'lib qoladi va keyingi ishga
        tushirish to'liq loginga tushardi. Avval faylni qayta o'qiymiz: boshqa
        jarayon allaqachon yangilagan bo'lsa, uning sessiyasini olamiz."""
        path = path or self.token_path or self._default_token_file()
        try:
            with open(path, encoding="utf-8") as f:
                tok = json.load(f)
            newer = (tok.get("session_id") and tok["session_id"] != self.session_id
                     and float(tok.get("session_time") or 0) > (self.session_time or 0))
        except (OSError, ValueError, TypeError):
            newer = False
        if newer and self.load_token(path):
            return "adopted"
        self.refresh_session(allow_login=False)
        self.save_token(path)
        return "refreshed"

    def save_token(self, path="ezviz_token.json"):
        """Sessiyani diskka yozadi — keyingi ishga tushirish login QILMAYDI.

        `feature_code` ham yoziladi: token boshqa kompyuterga ko'chirilsa
        [[load_token]] uni rad etadi (bulut baribir qabul qilmaydi).
        Fayl 0600 bilan atomar yoziladi ([[fsutil.write_private]])."""
        self.token_path = path
        write_private(path, json.dumps({
            "session_id": self.session_id,
            "rf_session_id": self.rf_session_id,
            "username": self.email,
            "api_url": self.api_url,
            "platform": self.platform,
            "feature_code": self.FEATURE_CODE,
            # Haqiqiy yosh yoziladi: noma'lum (0) bo'lsa `connect()`
            # yangilaydi. `time.time()` qo'yilsa sessiya "yangi" ko'rinib,
            # eskirgan holda ishlatilardi.
            "session_time": self.session_time,
        }))
