"""EZVIZ / Hik-Connect bulut jonli oqimi (VTM/VTDU) — transport qatlami.

Bu paket ilgari tashqi `pyezvizapi` kutubxonasidan olinardi va uning ICHKI
funksiyalari almashtirilardi (monkeypatch). Endi kerakli qismi shu yerga
ko'chirilgan: upstream relizi loyihani jimgina buza olmaydi, o'zgarishlarni
o'zimiz diff qarab olamiz.

Manba: https://github.com/RenierM26/pyEzvizApi — versiya 1.0.5.0,
Apache License 2.0 (to'liq matn: `LICENSE-pyezvizapi`). Har modulning
boshida nima o'zgartirilgani yozilgan.

  * [[protocol]] — TCP freymlash, StreamInfo/keepalive protobuf, `ysproto://` URL, RTP;
  * [[cloud]]    — pagelist / VTDU token (HTTP) va ulardan oqim URL'ini yig'ish.
"""

from cloudcam.sources.cloud.vtm.cloud import (
    fetch_vtdu_token,
    fetch_vtm_page_list,
    get_cloud_stream_info,
    open_cloud_stream,
)
from cloudcam.sources.cloud.vtm.protocol import (
    STREAM_MAIN,
    STREAM_SUB,
    DeviceOffline,
    VtmError,
    VtmPacket,
    VtmStreamClient,
    build_vtm_url,
    rtp_payload,
)

UPSTREAM = "pyezvizapi"
UPSTREAM_VERSION = "1.0.5.0"

__all__ = [
    "STREAM_MAIN", "STREAM_SUB", "DeviceOffline", "VtmError", "VtmPacket",
    "VtmStreamClient", "build_vtm_url", "fetch_vtdu_token", "fetch_vtm_page_list",
    "get_cloud_stream_info", "open_cloud_stream", "rtp_payload",
]
