"""List the real cameras (device + NVR channel) of an account."""

from __future__ import annotations



from cloudcam.sources.cloud import vtm_cache
from cloudcam.sources.cloud.stream import _make_client


def _device_names(client):
    """{serial: qurilma_nomi} — ilovadagi nomlar (NVR/kamera nomi)."""
    names = {}
    offset = 0
    for _ in range(40):
        r = client.session.get(
            f"https://{client.api_url}/v3/userdevices/v1/devices/pagelist",
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

    `client` — sessiyasi bor [[CloudClient]]. Berilmasa yoki sessiyasiz bo'lsa
    token fayldan quriladi ([[_make_client]])."""
    if client is None or not getattr(client, "session_id", None):
        client = _make_client()
    dev_names = _device_names(client)
    res = vtm_cache.pagelist(client).get("resourceInfos", []) or []
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
