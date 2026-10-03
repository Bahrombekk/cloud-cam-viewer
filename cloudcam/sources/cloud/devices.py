"""Bulutdagi qurilmalar va kanallar ro'yxati.

VTM resurslaridan olinadi — bo'sh NVR kanallari (kamera ulanmagan)
ko'rsatilmaydi.
"""
from __future__ import annotations

import json
import re

import requests as _rq
import pyezvizapi.cloud_stream as _cs
from pyezvizapi.client import EzvizClient

from ...core.identity import feature_code
from ...settings import get_active
from .compat import CLIENT_TYPE, install_patches

def make_client():
    import re
    with open(get_active().token_file, encoding="utf-8") as f:
        token = json.load(f)
    client = EzvizClient(token.get("username"), None, token.get("api_url"), token=token)
    # Klient turi — busiz pagelist hamma resurslarni qaytarmaydi (platformaga qarab)
    try:
        client._session.headers.update({"clientType": CLIENT_TYPE, "lang": "en-US"})
    except Exception:
        pass
    # Hik-Connect authAddr ni "https://null" qaytaradi -> regiondan derive qilamiz
    # (apiiSGP.hik-connect.com -> sgpauth.ezvizlife.com). EZVIZ uchun ham to'g'ri.
    su = (token.get("service_urls") or {})
    if not su.get("authAddr") or "null" in str(su.get("authAddr")).lower():
        m = re.match(r"apii([a-z]+)\.", str(token.get("api_url", "")))
        if m:
            client._token.setdefault("service_urls", {})["authAddr"] = \
                f"https://{m.group(1)}auth.ezvizlife.com"
    return client


def _device_names(client):
    """{serial: qurilma_nomi} — ilovadagi nomlar (NVR/kamera nomi)."""
    tok = getattr(client, "_token", {}) or {}
    api = tok.get("api_url")
    sess = _rq.Session()
    sess.headers.update({
        "clientType": CLIENT_TYPE, "lang": "en-US",
        "featureCode": feature_code(),        # shu o'rnatmaga xos ([[identity]])
        "sessionId": str(tok.get("session_id")),
    })
    names = {}
    offset = 0
    for _ in range(40):
        r = sess.get(f"https://{api}/v3/userdevices/v1/devices/pagelist",
                     params={"filter": "CONNECTION", "groupId": -1, "limit": 50, "offset": offset},
                     timeout=25)
        d = r.json()
        di = d.get("deviceInfos") or []
        for x in di:
            names[x.get("deviceSerial")] = (x.get("name") or "").strip()
        page = d.get("page") or {}
        if not di or not page.get("hasNext"):
            break
        offset += len(di)
    return names


def list_cameras(client=None):
    """Haqiqiy kameralar ro'yxati: [(serial, channel, name)].
    Nom = "Qurilma nomi — Kanal nomi" (ilovadagi nom bilan moslash uchun).
    VTM resurslaridan olinadi — bo'sh NVR kanallari (kamera ulanmagan) ko'rsatilmaydi.

    `client` — pyezvizapi `EzvizClient` (token bilan). Yuqori darajali API o'zining
    `CloudClient` obyektini uzatardi, unda esa `_token` yo'q: natijada so'rov
    `https://none/...` ga ketib `NameResolutionError` berardi. Shuning uchun mos
    kelmaydigan obyekt uzatilsa token fayldan o'zimiz quramiz."""
    if client is None or not getattr(client, "_token", None):
        client = make_client()
    dev_names = _device_names(client)
    res = _cs.get_vtm_page_list(client).get("resourceInfos", []) or []
    cams = []
    for r in res:
        try:
            ch = int(r.get("localIndex"))
        except (TypeError, ValueError):
            continue
        if ch < 1:  # localIndex 0 = qurilma (NVR) o'zi, kamera emas
            continue
        serial = r.get("deviceSerial")
        chname = (r.get("resourceName") or "").strip()
        devname = dev_names.get(serial, "")
        if devname and chname and devname.lower() not in chname.lower():
            label = f"{devname} - {chname}"   # ASCII '-' (cv2 oynada '—' ko'rinmaydi)
        else:
            label = chname or devname or f"{serial} CH{ch}"
        cams.append((serial, ch, label))
    cams.sort(key=lambda x: (x[0], x[1]))
    return cams
