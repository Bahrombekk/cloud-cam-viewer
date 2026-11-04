"""RTSP path templates per vendor, channel numbers, substream guesses, NVR scan.

Adapted from kamera-xarita (``camera/probe/detect.py`` and
``camera/media/fast_start.py``, same author, MIT).

    channel_path("hikvision", 3)           -> "/Streaming/Channels/301"
    channel_path("dahua", 3, "sub")        -> "/cam/realmonitor?channel=3&subtype=1"
    channel_from_path("/Streaming/Channels/1602")   -> 16
    sub_path_candidates("/Streaming/Channels/101")  -> ["/Streaming/Channels/102"]
    scan("192.168.1.10", username="admin", password="...")  -> [ScanHit, ...]

A scan probes every channel up to SETUP (see :mod:`.probe`), so channels an
NVR advertises but cannot stream are not reported.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from cloudcam.sources.rtsp.probe import DEFAULT_PORT, ProbeResult, probe

VENDORS = ("hikvision", "dahua", "amcrest", "uniview", "reolink", "axis", "holowits",
           "generic")

# The order a scan tries when the vendor is unknown: most common first.
DETECT_ORDER = ("hikvision", "dahua", "uniview", "reolink", "holowits", "axis", "generic")


def channel_path(vendor: str, channel: int = 1, stream: str = "main") -> str:
    """RTSP path of ``channel`` (1-based) on a ``vendor`` device; stream main|sub."""
    sub = stream == "sub"
    vendor = (vendor or "generic").lower()
    if vendor == "hikvision":
        return f"/Streaming/Channels/{channel}0{2 if sub else 1}"   # 101 = ch1 main
    if vendor in ("dahua", "amcrest"):
        return f"/cam/realmonitor?channel={channel}&subtype={1 if sub else 0}"
    if vendor == "uniview":
        return f"/unicast/c{channel}/s{2 if sub else 1}/live"
    if vendor == "reolink":
        return f"/h264Preview_{channel:02d}_{'sub' if sub else 'main'}"
    if vendor == "axis":
        return f"/axis-media/media.amp?camera={channel}"
    if vendor == "holowits":
        return f"/LiveMedia/ch{channel}/Media{2 if sub else 1}"
    return f"/stream{2 if sub else 1}"


_CHANNEL_PATTERNS = (
    r"/Channels/(\d+)",      # hikvision: /Streaming/Channels/101 -> 1
    r"channel=(\d+)",        # dahua/amcrest
    r"Preview_(\d+)_",       # reolink: /h264Preview_01_main -> 1
    r"camera=(\d+)",         # axis
    r"/c(\d+)/",             # uniview: /unicast/c3/s1/live -> 3
    r"/ch(\d+)/",            # holowits: /LiveMedia/ch2/Media1 -> 2
)


def channel_marked(path: str) -> int | None:
    """The channel the path names explicitly, or None (``/stream1`` names none)."""
    for pattern in _CHANNEL_PATTERNS:
        m = re.search(pattern, path or "", re.IGNORECASE)
        if m:
            n = int(m.group(1))
            if pattern is _CHANNEL_PATTERNS[0] and n >= 100:
                n //= 100        # 101 -> 1, 1602 -> 16
            return max(1, n)
    return None


def channel_from_path(path: str) -> int:
    marked = channel_marked(path)
    return marked if marked is not None else 1


def sub_path_candidates(path: str) -> list[str]:
    """Guesses for the substream of a main-stream path; each must be probed.

    Measured in kamera-xarita: of 8 cameras saved without a substream, 4 had
    one running all along.
    """
    p = (path or "").strip()
    if not p:
        return []
    out: list[str] = []
    if "subtype=0" in p:                                     # dahua
        out.append(p.replace("subtype=0", "subtype=1"))
    m = re.search(r"(?i)(/streaming/channels/)(\d+)", p)     # hikvision 101 -> 102
    if m and m.group(2).endswith("1"):
        out.append(p[:m.start(2)] + m.group(2)[:-1] + "2" + p[m.end(2):])
    m = re.search(r"(?i)(stream)0*1\b", p)                   # .../stream1
    if m:
        out.append(p[:m.start()] + m.group(1) + "2" + p[m.end():])
    if re.search(r"(?i)/main\b", p):                         # .../main
        out.append(re.sub(r"(?i)/main\b", "/sub", p))
    m = re.search(r"(?i)/Media1\b", p)                       # holowits
    if m:
        out.append(p[:m.start()] + "/Media2" + p[m.end():])
    return [c for c in dict.fromkeys(out) if c and c != p]


@dataclass
class ScanHit:
    channel: int
    path: str
    result: ProbeResult
    sub_path: str = ""
    sub_result: ProbeResult | None = None


def detect_vendor(host: str, port: int = DEFAULT_PORT, username: str = "",
                  password: str = "", timeout: float = 4.0) -> tuple[str, ProbeResult | None]:
    """The first vendor whose channel-1 path streams, or ("", last failure).

    Stops early on a network/auth failure: those do not depend on the path.
    """
    last = None
    for vendor in DETECT_ORDER:
        r = probe(host, port, channel_path(vendor, 1), username, password, timeout=timeout)
        if r.ok:
            return vendor, r
        last = r
        if r.stage in ("network", "auth"):
            break
    return "", last


def scan(host: str, port: int = DEFAULT_PORT, username: str = "", password: str = "",
         vendor: str = "", channels: int = 32, substreams: bool = True,
         workers: int = 4, timeout: float = 4.0, stop_after_misses: int = 8) -> list[ScanHit]:
    """Find the channels of an NVR (or the one channel of a camera).

    ``vendor`` is detected when empty. Channels are probed in small parallel
    batches (NVRs dislike dozens of simultaneous sessions); the scan stops
    after ``stop_after_misses`` consecutive missing channels past the last hit.
    """
    if not vendor:
        vendor, _ = detect_vendor(host, port, username, password, timeout)
        if not vendor:
            return []
    if channel_path(vendor, 1) == channel_path(vendor, 2):
        channels = 1            # the path has no channel number (generic /stream1)
    hits: list[ScanHit] = []
    misses = 0

    def check(ch: int) -> ScanHit | None:
        path = channel_path(vendor, ch)
        r = probe(host, port, path, username, password, timeout=timeout)
        if not r.ok:
            return None
        hit = ScanHit(ch, path, r)
        if substreams:
            sub = channel_path(vendor, ch, "sub")
            if sub != path:
                sr = probe(host, port, sub, username, password, timeout=timeout)
                if sr.ok:
                    hit.sub_path, hit.sub_result = sub, sr
        return hit

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for start in range(1, channels + 1, max(1, workers)):
            batch = list(range(start, min(start + max(1, workers), channels + 1)))
            for hit in pool.map(check, batch):
                if hit:
                    hits.append(hit)
                    misses = 0
                else:
                    misses += 1
            if misses >= stop_after_misses:
                break
    return sorted(hits, key=lambda h: h.channel)
