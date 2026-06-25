"""
EZVIZ Client — terminal limit chetlab o'tuvchi custom login.

Android client sifatida ulanib, terminal limit (code 1069) ni chetlab o'tadi.
"""

import hashlib
import json
import os
import requests


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


class MfaRequired(Exception):
    """Kalit olish uchun 2FA (email) kodi kerak bo'lganda ko'tariladi."""


class CloudClient:
    """EZVIZ / Hik-Connect bulutiga kirish va qurilmalar ro'yxati."""
    FEATURE_CODE = "1fc28fa018178a1cd1c091b13b2f9f02"

    # Platforma sozlamalari: clientType va boshlang'ich domen
    PLATFORMS = {
        "hikconnect": {"client_type": "55", "domain": "api.hik-connect.com", "lang": "en-US"},
        "ezviz":      {"client_type": "1",  "domain": "apiisgp.ezvizlife.com", "lang": "en_US"},
    }

    def __init__(self, email, password, region=None, platform="hikconnect"):
        self.email = email
        self.password = password
        self.platform = platform if platform in self.PLATFORMS else "hikconnect"
        cfg = self.PLATFORMS[self.platform]
        # region berilgan bo'lsa uni, aks holda platforma standartini ishlatamiz
        self.api_url = region or cfg["domain"]
        self.session_id = None
        self.rf_session_id = None
        self.user_code = None

        self.session = requests.Session()
        self.session.headers.update({
            "clientType": cfg["client_type"],
            "lang": cfg["lang"],
            "featureCode": self.FEATURE_CODE,
            "Content-Type": "application/x-www-form-urlencoded",
        })

    def login(self):
        """Hisobga kirish. Hik-Connect va EZVIZ login API'lari farq qiladi."""
        if self.platform == "ezviz":
            return self._login_ezviz()
        return self._login_hikconnect()

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
                self.api_url = data["loginArea"]["apiDomain"]
                continue
            if code in (1013, 1014, 1226):
                raise RuntimeError("Login xatosi: email yoki parol noto'g'ri")
            if code == 1015:
                raise RuntimeError(
                    "CAPTCHA so'raldi — avval Hik-Connect ilovasiga kiring, keyin qayta urining")
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
            raise RuntimeError(f"Login xatosi: {data.get('meta') if data else 'javob yo`q'}")
        self.session_id = data["loginSession"]["sessionId"]
        self.rf_session_id = data["loginSession"]["rfSessionId"]
        self.api_url = data.get("loginArea", {}).get("apiDomain", self.api_url)
        self.user_code = data.get("loginUser", {}).get("userCode")
        self.session.headers.update({"sessionId": self.session_id})
        return data

    def refresh_session(self):
        """
        Session ni refresh token bilan yangilash — parol/MFA so'ramaydi.
        24/7 ishlash uchun muhim. Muvaffaqiyatsiz bo'lsa to'liq qayta login qiladi.
        """
        try:
            r = self.session.put(
                f"https://{self.api_url}/v3/apigateway/login",
                data={
                    "refreshSessionId": self.rf_session_id,
                    "featureCode": self.FEATURE_CODE,
                },
                timeout=25,
            )
            data = r.json()
            if data.get("meta", {}).get("code") == 200:
                info = data.get("sessionInfo", {})
                self.session_id = info.get("sessionId", self.session_id)
                self.rf_session_id = info.get("refreshSessionId", self.rf_session_id)
                self.session.headers.update({"sessionId": self.session_id})
                return True
        except Exception:
            pass
        # Refresh ishlamasa — to'liq qayta login
        self.login()
        return True

    def get_service_urls(self):
        r = self.session.get(
            f"https://{self.api_url}/v3/configurations/system/info", timeout=25)
        return r.json().get("systemConfigInfo", {})

    def get_devices(self):
        """Barcha kameralar: {serial: {name, status, model, channels}}"""
        r = self.session.get(
            f"https://{self.api_url}/v3/userdevices/v1/devices/pagelist",
            params={
                "filter": "CLOUD,CONNECTION,SWITCH,STATUS,WIFI,NODISTURB,"
                          "P2P,CHANNEL,VTM,FEATURE,UPGRADE,VIDEO_QUALITY,QOS",
                "groupId": -1, "limit": 50, "offset": 0,
            },
            timeout=25,
        )
        data = r.json()
        result = {}
        for dev in data.get("deviceInfos", []):
            serial = dev.get("deviceSerial")
            if serial:
                result[serial] = {
                    "name": dev.get("name", serial),
                    "status": dev.get("status", 0),
                    "model": dev.get("deviceType", ""),
                    "channels": dev.get("channelNumber", 1),
                }
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
            raise MfaRequired("2FA kod kerak (resultCode 20002)")
        if code == "10001":
            raise RuntimeError("Takroriy so'rov — 1-2 daqiqa kutib qayta urining (10001)")
        if code == "2009":
            raise RuntimeError("Qurilma ulanmagan (2009)")
        # Xitoycha resultDes konsolda crash bermasligi uchun faqat kod ko'rsatamiz
        raise RuntimeError(f"Kalitni ololmadim (resultCode {code})")

    def save_token(self, path="ezviz_token.json"):
        with open(path, "w") as f:
            json.dump({
                "session_id": self.session_id,
                "rf_session_id": self.rf_session_id,
                "username": self.email,
                "api_url": self.api_url,
            }, f)
