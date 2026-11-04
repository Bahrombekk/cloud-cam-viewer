"""Plain RTSP cameras and NVRs (Hikvision, Dahua, Uniview, Reolink, Axis, ...).

Cloud cameras need the EZVIZ / Hik-Connect relay and decryption; a camera on
your network usually speaks RTSP already. This package checks such a camera
and finds the channels of an NVR, so both kinds can sit side by side in the
viewer and behind ``cloudcam serve``.

    from cloudcam.sources.rtsp import probe_url, scan
    probe_url("rtsp://admin:...@192.168.1.64/Streaming/Channels/101").message
    for hit in scan("192.168.1.10", username="admin", password="..."):
        print(hit.channel, hit.path, hit.result.codec)

Adapted from kamera-xarita (same author, MIT).
"""
from cloudcam.sources.rtsp.probe import (
    DEFAULT_PORT,
    ProbeResult,
    build_rtsp_url,
    probe,
    probe_url,
    redact_url,
    split_rtsp_url,
)
from cloudcam.sources.rtsp.vendors import (
    VENDORS,
    ScanHit,
    channel_from_path,
    channel_path,
    detect_vendor,
    scan,
    sub_path_candidates,
)

__all__ = [
    "DEFAULT_PORT", "ProbeResult", "ScanHit", "VENDORS", "build_rtsp_url",
    "channel_from_path", "channel_path", "detect_vendor", "probe", "probe_url",
    "redact_url", "scan", "split_rtsp_url", "sub_path_candidates",
]
