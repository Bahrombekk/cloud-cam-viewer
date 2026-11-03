"""Bulut push xabarlari (MQTT) — harakat/odam aniqlanganda darhol hodisa.

`pyezvizapi` 1.0.5.0 (`pyezvizapi/mqtt.py`) asosida (Apache-2.0,
(c) Renier Moorcroft; litsenziya — `vtm/LICENSE-pyezvizapi`). O'zgarishlar:
  * ichki foydalanuvchi nomi `/v3/users/info` dan olinadi — login javobini
    saqlash shart emas, mavjud `token.json` bilan ishlaydi;
  * `mac` sifatida shu o'rnatmaning terminal kodi ([[identity.feature_code]]);
  * push manzili domeni tekshiriladi ([[client.safe_domain]]) — unga `sessionId` ketadi;
  * xabar [[cloud_api.Event]] ga aylantiriladi.

Talab: `pip install "cloudcam[events]"` (paho-mqtt).

    with PushClient(client, on_event=print):
        time.sleep(3600)
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import requests

from .client import safe_domain
from .cloud_api import Event, _epoch, _int
from .i18n import t
from .identity import feature_code

_LOG = logging.getLogger(__name__)

# EZVIZ ilovasining ochiq push kalitlari (pyezvizapi constants).
APP_KEY = "4c6b3cc2-b5eb-4813-a592-612c1374c1fe"
APP_SECRET = "17454517-cc1c-42b3-a845-99b4a15dd3e6"
MQTT_PORT = 1882
HTTP_TIMEOUT = 25

# `ext` maydoni — vergul bilan ajratilgan qiymatlar shu tartibda.
EXT_FIELDS = (
    "channel_type", "time", "device_serial", "channel_no", "alert_type_code",
    "default_pic_url", "media_url_alt1", "media_url_alt2", "resource_type",
    "status_flag", "file_id", "is_encrypted", "picChecksum", "is_dev_video",
    "metadata", "msgId", "image", "device_name", "reserved", "sequence_number",
)
_EXT_INT = frozenset({"channel_type", "channel_no", "alert_type_code", "resource_type",
                      "status_flag", "is_encrypted", "is_dev_video", "sequence_number"})


class PushError(RuntimeError):
    pass


def decode_message(payload: bytes) -> dict:
    """Xom MQTT yuki -> dict; `ext` nomlangan maydonlarga ajratiladi."""
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise PushError(f"Unable to decode MQTT message: {err}") from err
    if not isinstance(data, dict):
        raise PushError("MQTT message is not an object")
    if isinstance(data.get("ext"), str):
        parts = data["ext"].split(",")
        ext: dict[str, Any] = {}
        for i, name in enumerate(EXT_FIELDS):
            v: Any = parts[i] if i < len(parts) else None
            if v is not None and name in _EXT_INT:
                with suppress(ValueError):
                    v = int(v)
            ext[name] = v
        data["ext"] = ext
    return data


def to_event(msg: dict) -> Event | None:
    ext = msg.get("ext") if isinstance(msg.get("ext"), dict) else {}
    serial = ext.get("device_serial")
    if not serial:
        return None
    enc = ext.get("is_encrypted")
    pic = ext.get("default_pic_url") or ext.get("image") or None
    return Event(
        serial=str(serial),
        channel=_int(ext.get("channel_no")),
        time=_epoch(ext.get("time")) if str(ext.get("time") or "").isdigit() else None,
        time_str=str(ext.get("time") or ""),
        kind=str(ext.get("alert_type_code") or ""),
        title=str(msg.get("alert") or msg.get("title") or ext.get("device_name") or ""),
        picture_url=pic if isinstance(pic, str) and pic else None,
        encrypted=None if enc is None else bool(enc),
        msg_id=ext.get("msgId") or None,
        raw=msg,
    )


class PushClient:
    """Hisob uchun push oqimi. `on_event` paho thread'ida chaqiriladi —
    og'ir ishni o'zingizning navbatingizga o'tkazing."""

    def __init__(self, client, on_event: Callable[[Event], None] | None = None, *,
                 on_raw: Callable[[dict], None] | None = None) -> None:
        self._client = client
        self._on_event = on_event
        self._on_raw = on_raw
        self._http = requests.Session()        # cloud sessiya sarlavhalarisiz
        self._mqtt = None
        self._client_id: str | None = None
        self._username: str | None = None
        self._host: str | None = None
        self._lock = threading.Lock()

    # ---- bulut tomoni ------------------------------------------------------
    def _push_host(self) -> str:
        urls = self._client.get_service_urls() or {}
        host = str(urls.get("pushAddr") or "").strip()
        if not host:
            raise PushError("Missing pushAddr in service URLs")
        return safe_domain(host)

    def _internal_username(self) -> str:
        r = self._client.session.get(f"https://{self._client.api_url}/v3/users/info",
                                     timeout=HTTP_TIMEOUT)
        info = (r.json() or {}).get("userInfo") or {}
        name = info.get("username")
        if not name:
            raise PushError("Could not get the internal account username")
        return str(name)

    def _post(self, path: str, data: dict, **kw) -> dict:
        r = self._http.post(f"https://{self._host}{path}", data=data,
                            timeout=HTTP_TIMEOUT, allow_redirects=False, **kw)
        r.raise_for_status()
        out = r.json()
        if out.get("status") != 200:
            raise PushError(f"{path}: status {out.get('status')}")
        return out

    def _register(self) -> None:
        auth = base64.b64encode(f"{APP_KEY}:{APP_SECRET}".encode("ascii")).decode()
        out = self._post("/v1/getClientId", {
            "appKey": APP_KEY, "clientType": "5", "mac": feature_code(),
            "token": "123456", "version": "v1.3.0",
        }, headers={"Authorization": f"Basic {auth}"})
        self._client_id = out["data"]["clientId"]

    def _start_push(self) -> None:
        self._post("/api/push/start", {
            "appKey": APP_KEY, "clientId": self._client_id, "clientType": 5,
            "sessionId": self._client.session_id, "username": self._username,
            "token": "123456",
        })

    def _stop_push(self) -> None:
        self._post("/api/push/stop", {
            "appKey": APP_KEY, "clientId": self._client_id, "clientType": 5,
            "sessionId": self._client.session_id, "username": self._username,
        })

    # ---- MQTT ----------------------------------------------------------------
    def start(self, *, keepalive: int = 60) -> PushClient:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as err:
            raise PushError(t('Push events need paho-mqtt: pip install "cloudcam[events]"',
                              'Push hodisalari uchun paho-mqtt kerak: '
                              'pip install "cloudcam[events]"')) from err
        with self._lock:
            if self._mqtt is not None:
                return self
            self._host = self._push_host()
            self._username = self._internal_username()
            self._register()
            self._start_push()
            kw: dict[str, Any] = {"client_id": self._client_id, "clean_session": True,
                                  "protocol": mqtt.MQTTv311, "transport": "tcp"}
            cav = getattr(mqtt, "CallbackAPIVersion", None)
            if cav is not None:
                kw["callback_api_version"] = cav.VERSION1
            m = mqtt.Client(**kw)
            m.on_connect = self._on_connect
            m.on_message = self._on_message
            m.username_pw_set(APP_KEY, APP_SECRET)
            m.reconnect_delay_set(min_delay=5, max_delay=60)
            m.connect(self._host, MQTT_PORT, keepalive)
            m.loop_start()
            self._mqtt = m
        return self

    def stop(self) -> None:
        with self._lock:
            m, self._mqtt = self._mqtt, None
            if m is not None:
                with suppress(Exception):
                    m.loop_stop()
                    m.disconnect()
            if self._client_id and self._host:
                with suppress(Exception):
                    self._stop_push()

    def __enter__(self) -> PushClient:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            client.subscribe(f"{APP_KEY}/#", qos=2)
        else:
            _LOG.warning("MQTT connect failed: rc=%s", rc)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            data = decode_message(msg.payload)
        except PushError as err:
            _LOG.warning("%s", err)
            return
        try:
            if self._on_raw:
                self._on_raw(data)
            ev = to_event(data)
            if ev is not None and self._on_event:
                self._on_event(ev)
        except Exception:  # noqa: BLE001 — foydalanuvchi callback'i oqimni to'xtatmasin
            _LOG.exception("push callback raised")
