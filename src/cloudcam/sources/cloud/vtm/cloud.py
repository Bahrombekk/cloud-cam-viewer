"""Bulutdan jonli oqim metama'lumotini olish: VTM pagelist, VTDU token, oqim URL.

`pyezvizapi` 1.0.5.0 (`pyezvizapi/cloud_stream.py`) asosida (Apache-2.0,
(c) Renier Moorcroft; litsenziya — `LICENSE-pyezvizapi`). O'zgarishlar:
  * `EzvizClient` o'rniga o'zimizning [[cloudcam.sources.cloud.client.CloudClient]] —
    pyezvizapi'ning yashirin metodlariga (`_request_json`, `_api_get_pagelist`,
    `_token`) bog'liqlik yo'q;
  * pagelist TO'LIQ sahifalab olinadi (kutubxona faqat 1-sahifani olardi —
    ko'p NVR'li hisobda kameralar topilmasdi);
  * HTTP so'rovlar (`fetch_*`) va URL yig'ish ([[get_cloud_stream_info]]) ajratilgan:
    keshlash chaqiruvchining ishi ([[cloudcam.sources.cloud.vtm_cache]]), modul global
    funksiyalarini almashtirish (monkeypatch) shart emas;
  * `authAddr` domeni tekshiriladi — `sessionId` shu manzilga yuboriladi.

Klientdan kutiladigan interfeys: `api_url`, `session_id`, `session`
(`requests.Session`, sarlavhalar bilan), `get_service_urls()`,
`refresh_session(allow_login=False)`.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import urlparse

from cloudcam.sources.cloud.client import UnsafeDomain, safe_domain
from cloudcam.sources.cloud.vtm.protocol import (
    STREAM_MAIN,
    SocketFactory,
    VtmError,
    VtmStreamClient,
    build_vtm_url,
)

JsonDict = dict[str, Any]

PAGELIST_PATH = "/v3/userdevices/v1/resources/pagelist"
VTDU_TOKEN_PATH = "/vtdutoken2"
PAGE_LIMIT = 50
MAX_PAGES = 40              # 40 x 50 = 2000 resurs — cheksiz tsikldan himoya
MAX_RETRIES = 3
HTTP_TIMEOUT = 25

_SERVICE_URLS_ATTR = "_vtm_service_urls"


# ---- HTTP: pagelist --------------------------------------------------------

def fetch_vtm_page_list(client: Any) -> JsonDict:
    """VTM resurslari pagelist'i — BARCHA sahifalar birlashtirilgan.

    Javobda `meta.code` 200 bo'lmasa xato: ilgari tekshirilmasdi va eskirgan
    sessiyadagi bo'sh ro'yxat keshga yozilib, butun hisob kameralari soatgacha
    "topilmadi" ko'rinardi."""
    base: JsonDict | None = None
    merged_res: list[Any] = []
    merged_vtm: JsonDict = {}
    offset = 0
    for _ in range(MAX_PAGES):
        r = client.session.get(
            f"https://{client.api_url}{PAGELIST_PATH}",
            params={"filter": "VTM", "groupId": -1, "limit": PAGE_LIMIT, "offset": offset},
            timeout=HTTP_TIMEOUT,
        )
        pl = _json(r)
        code = (pl.get("meta") or {}).get("code")
        if code is not None and str(code) != "200":
            raise VtmError(f"VTM pagelist failed: meta.code {code}")
        if base is None:
            base = pl
        res = pl.get("resourceInfos") or []
        merged_res.extend(res)
        vtm = pl.get("VTM")
        if isinstance(vtm, dict):
            merged_vtm.update(vtm)
        page = pl.get("page") or {}
        if not res or not page.get("hasNext"):
            break
        offset += len(res)
    out = base or {}
    out["resourceInfos"] = merged_res
    out["VTM"] = merged_vtm
    return out


# ---- HTTP: VTDU token ------------------------------------------------------

def fetch_vtdu_token(client: Any) -> JsonDict:
    """VTDU oqim tokenlari (auth xizmatidan). 401 da sessiya yangilanib qayta so'raladi."""
    for attempt in range(MAX_RETRIES + 1):
        r = client.session.get(
            f"{auth_base_url(client)}{VTDU_TOKEN_PATH}",
            params={"ssid": client.session_id, "sign": session_sign(client.session_id)},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 401 and attempt < MAX_RETRIES:
            client.refresh_session(allow_login=False)
            continue
        if r.status_code >= 400:
            raise VtmError(f"Could not get VTDU token: HTTP {r.status_code}")
        out = _json(r)
        if out.get("retcode") not in (0, "0"):
            raise VtmError(f"Could not get VTDU token: retcode {out.get('retcode')}")
        tokens = out.get("tokens")
        if not isinstance(tokens, list) or not tokens:
            raise VtmError("Could not get VTDU token: empty token list")
        return out
    raise VtmError("Could not get VTDU token. Max retries exceeded.")


def session_sign(session_id: Any) -> str:
    """VTDU so'rovining `sign` i — sessiya JWT'sidagi `s` claim."""
    if not session_id:
        raise VtmError("No Login token present!")
    parts = str(session_id).split(".")
    if len(parts) < 2:
        raise VtmError("Current session token is not a JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except (ValueError, UnicodeDecodeError) as err:
        raise VtmError("Could not decode current session token claims") from err
    sign = claims.get("s") if isinstance(claims, dict) else None
    if not isinstance(sign, str) or not sign:
        raise VtmError("Current session token does not contain VTDU sign claim")
    return sign


def derive_auth_addr(api_url: str) -> str | None:
    """`apiisgp.hik-connect.com` / `apiisgp.ezvizlife.com` -> `https://sgpauth.ezvizlife.com`.

    Hik-Connect `authAddr` ni "https://null" qaytaradi — region API domenidan
    olinadi. Mos kelmasa None (unda `get_service_urls()` so'raladi)."""
    m = re.match(r"apii([a-z]+)\.", str(api_url or "").strip().lower())
    return f"https://{m.group(1)}auth.ezvizlife.com" if m else None


def auth_base_url(client: Any) -> str:
    """VTDU auth xizmati manzili (domen tekshirilgan, sxema bilan, oxirida `/` siz)."""
    addr = derive_auth_addr(client.api_url)
    if addr is None:
        urls = getattr(client, _SERVICE_URLS_ATTR, None)
        if urls is None:
            urls = client.get_service_urls() or {}
            try:
                setattr(client, _SERVICE_URLS_ATTR, urls)
            except Exception:  # noqa: BLE001 — keshsiz ishlaymiz
                pass
        addr = str(urls.get("authAddr") or "").strip()
        if _missing_auth_addr(addr):
            raise VtmError("Missing authAddr in service URLs")
    if not addr.startswith(("http://", "https://")):
        addr = f"https://{addr}"
    host = urlparse(addr).hostname or ""
    try:
        safe_domain(host)
    except UnsafeDomain as err:
        raise VtmError(f"authAddr domain not allowed: {host!r}") from err
    return f"https://{host}"


def _missing_auth_addr(value: str) -> bool:
    if not value or value.lower() in {"none", "null"}:
        return True
    cand = value if value.startswith(("http://", "https://")) else f"https://{value}"
    return (urlparse(cand).hostname or "").lower() in {"", "none", "null"}


def _json(resp: Any) -> JsonDict:
    try:
        data = resp.json()
    except ValueError as err:
        raise VtmError(f"Impossible to decode response (HTTP {resp.status_code})") from err
    if not isinstance(data, dict):
        raise VtmError("Unexpected response shape")
    return data


# ---- metama'lumotdan oqim URL'i (tarmoqsiz) --------------------------------

def find_vtm_resource(resources: list[Any], serial: str, *, channel: int | None) -> JsonDict | None:
    own = [x for x in resources if isinstance(x, dict) and x.get("deviceSerial") == serial]
    if channel is None:
        return own[0] if own else None
    return next((x for x in own if str(x.get("localIndex")) == str(channel)), None)


def get_cloud_stream_info(
    pagelist: JsonDict,
    vtdu: JsonDict,
    serial: str,
    *,
    channel: int | None = None,
    client_type: int = 9,
    stream_type: int = STREAM_MAIN,
    token_index: int = 0,
) -> JsonDict:
    """Pagelist + VTDU tokenidan kamera uchun `ysproto://` URL yig'adi.

    Tarmoqqa chiqmaydi — metama'lumotni chaqiruvchi beradi (odatda keshdan)."""
    resources = pagelist.get("resourceInfos") or []
    vtms = pagelist.get("VTM") or {}
    if not isinstance(resources, list) or not isinstance(vtms, dict):
        raise VtmError("VTM pagelist response is missing resource metadata")

    resource = find_vtm_resource(resources, serial, channel=channel)
    if resource is None:
        ch = f" channel {channel}" if channel is not None else ""
        # Matn muhim: decrypt_proxy._is_missing_resource shuni qidiradi.
        raise VtmError(f"Could not find VTM resource for serial {serial}{ch}")

    resource_id = resource.get("resourceId")
    tokens = vtdu.get("tokens") or []
    try:
        vtdu_token = tokens[token_index]
    except IndexError as err:
        raise VtmError(f"VTDU token index out of range: {token_index}") from err
    if not isinstance(vtdu_token, str):
        raise VtmError(f"Invalid VTDU token at index {token_index}")

    stream_channel = channel
    if stream_channel is None:
        li = str(resource.get("localIndex"))
        stream_channel = int(li) if li.isdigit() else 1

    vtm = vtms.get(resource_id)
    if not isinstance(vtm, dict):
        raise VtmError(f"Could not find VTM server for resource {resource_id}")
    host = vtm.get("externalIp") or vtm.get("domain") or vtm.get("internalIp")
    if not isinstance(host, str) or not host.strip():
        raise VtmError(f"Could not find VTM endpoint for resource {resource_id}")
    port_value = vtm.get("port")
    if not str(port_value).isdigit() or not 1 <= int(port_value) <= 65535:
        raise VtmError(f"Could not find VTM port for resource {resource_id}")

    stream_url = build_vtm_url(
        host.strip(), int(port_value), serial,
        str(resource.get("streamBizUrl") or ""), vtdu_token,
        channel=stream_channel, client_type=client_type, stream_type=stream_type,
    )
    return {"resource": resource, "vtm": vtm, "vtdu_token": vtdu_token,
            "stream_url": stream_url}


def open_cloud_stream(
    pagelist: JsonDict,
    vtdu: JsonDict,
    serial: str,
    *,
    channel: int | None = None,
    client_type: int = 9,
    stream_type: int = STREAM_MAIN,
    token_index: int = 0,
    timeout: float | None = 10.0,
    socket_factory: SocketFactory | None = None,
) -> VtmStreamClient:
    """Ishga tushirilmagan [[VtmStreamClient]]: `with` + `start()` bilan ishlatiladi."""
    info = get_cloud_stream_info(pagelist, vtdu, serial, channel=channel,
                                 client_type=client_type, stream_type=stream_type,
                                 token_index=token_index)
    if socket_factory is None:
        return VtmStreamClient(info["stream_url"], timeout=timeout)
    return VtmStreamClient(info["stream_url"], timeout=timeout, socket_factory=socket_factory)
