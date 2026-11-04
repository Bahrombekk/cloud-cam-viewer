"""Check an RTSP camera step by step: network, RTSP, credentials, stream.

Adapted from kamera-xarita (``backend/camera/probe/rtsp_probe.py``, same
author, MIT), where it vets every camera before it is saved — ~155 cameras of
Hikvision, Dahua and Holowits in production.

No third-party library: RTSP is spoken directly over TCP. Most cameras use
Digest auth, so both Basic and Digest (RFC 2617 with ``qop`` and the older
RFC 2069) are supported.

Each stage has its own error text, so the user sees *what* is wrong:

    network   port closed, DNS, host unreachable
    rtsp      not an RTSP service, path not found, connection dropped
    auth      credentials missing or wrong
    stream    SDP has no video, or SETUP was refused
    ready     all good

The SETUP stage matters: some NVRs answer DESCRIBE for any channel number and
only refuse at SETUP, so without it a scan would report phantom channels.

Limit: a successful SETUP does not guarantee frames will arrive (an NVR may
advertise a substream and never send packets); that needs a real pull.

    probe("192.168.1.64", 554, "/Streaming/Channels/101", "admin", "...")
    probe_url("rtsp://admin:...@192.168.1.64/Streaming/Channels/101")
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import secrets
import socket
import urllib.parse
from dataclasses import asdict, dataclass

from cloudcam.i18n import t

TIMEOUT = 6.0
USER_AGENT = "cloudcam"
DEFAULT_PORT = 554

STAGES = ("network", "rtsp", "auth", "stream", "ready")


@dataclass
class ProbeResult:
    ok: bool
    stage: str                  # one of STAGES
    message: str
    codec: str = ""             # "H264", "H265", ... ("" if the SDP does not say)
    resolution: str = ""        # "1920x1080" or ""
    fps: float = 0.0            # 0 when the camera does not advertise it
    audio: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---------- URLs ----------

def build_rtsp_url(host: str, port: int = DEFAULT_PORT, path: str = "",
                   username: str = "", password: str = "") -> str:
    """Full RTSP URL; credentials are percent-encoded, IPv6 hosts bracketed."""
    path = "/" + (path or "").lstrip("/")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if username:
        cred = urllib.parse.quote(username, safe="")
        if password:
            cred += ":" + urllib.parse.quote(password, safe="")
        return f"rtsp://{cred}@{host}:{port}{path}"
    return f"rtsp://{host}:{port}{path}"


def split_rtsp_url(url: str) -> tuple[str, int, str, str, str]:
    """``rtsp://user:pass@host:port/path?q`` -> (host, port, path, user, password)."""
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme.lower() != "rtsp" or not u.hostname:
        raise ValueError(t(f"not an rtsp:// URL: {redact_url(url)}",
                           f"rtsp:// manzil emas: {redact_url(url)}"))
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return (u.hostname, u.port or DEFAULT_PORT, path,
            urllib.parse.unquote(u.username or ""),
            urllib.parse.unquote(u.password or ""))


def redact_url(url: str) -> str:
    """The URL with the password replaced by ``***`` — safe to print or log."""
    return re.sub(r"(rtsp://[^:/@\s]*:)[^@\s]*@", r"\1***@", url, flags=re.IGNORECASE)


# ---------- RTSP over a plain socket ----------

def _digest_header(username: str, password: str, method: str, uri: str,
                   challenge: str, nc: int = 1, cnonce: str | None = None) -> str:
    """Digest ``Authorization`` header.

    With ``qop`` (Axis, many ONVIF devices, some Dahua firmware) the RFC 2617
    formula MD5(HA1:nonce:nc:cnonce:qop:HA2) is used, otherwise RFC 2069. A
    second request under the same nonce (SETUP) must increment ``nc``.
    """
    fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = fields.get("realm", "")
    nonce = fields.get("nonce", "")
    qop_raw = fields.get("qop", "")
    if not qop_raw:                           # qop=auth without quotes
        m = re.search(r'qop=([^,\s"]+)', challenge)
        qop_raw = m.group(1) if m else ""
    qop = "auth" if "auth" in [q.strip() for q in qop_raw.split(",")] else ""

    def md5(s: str) -> str:
        return hashlib.md5(s.encode()).hexdigest()  # noqa: S324 — mandated by RFC 2617

    ha1 = md5(f"{username}:{realm}:{password}")
    ha2 = md5(f"{method}:{uri}")
    nc_value = f"{nc:08x}"
    if qop:
        cnonce = cnonce or secrets.token_hex(8)
        response = md5(f"{ha1}:{nonce}:{nc_value}:{cnonce}:{qop}:{ha2}")
    else:
        response = md5(f"{ha1}:{nonce}:{ha2}")
    header = (f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
              f'uri="{uri}", response="{response}"')
    if qop:
        header += f', qop={qop}, nc={nc_value}, cnonce="{cnonce}"'
    if "opaque" in fields:
        header += f', opaque="{fields["opaque"]}"'
    return header


def _basic_header(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def _request(sock: socket.socket, method: str, uri: str, cseq: int,
             auth: str = "", extra: list[str] | None = None) -> str:
    lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {cseq}", f"User-Agent: {USER_AGENT}"]
    if method == "DESCRIBE":
        lines.append("Accept: application/sdp")
    lines.extend(extra or [])
    if auth:
        lines.append(f"Authorization: {auth}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

    data = b""
    while b"\r\n\r\n" not in data and len(data) <= 65536:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, sep, body = data.partition(b"\r\n\r\n")
    # The SDP body may arrive after the headers: read up to Content-Length.
    m = re.search(rb"(?im)^content-length:\s*(\d+)", head)
    if sep and m:
        want = min(int(m.group(1)), 65536)
        while len(body) < want:
            chunk = sock.recv(4096)
            if not chunk:
                break
            body += chunk
    return (head + sep + body).decode("utf-8", "replace")


def _status(response: str) -> int:
    m = re.match(r"RTSP/\d\.\d (\d+)", response)
    return int(m.group(1)) if m else 0


def _challenge(response: str) -> str:
    """The strongest WWW-Authenticate challenge (Digest preferred over Basic)."""
    found = [line.split(":", 1)[1].strip() for line in response.split("\r\n")
             if line.lower().startswith("www-authenticate:")]
    for c in found:
        if c.lower().startswith("digest"):
            return c
    return found[0] if found else ""


# ---------- SDP ----------

_VIDEO_CODECS = ("H264", "H265", "HEVC", "MP4V-ES", "JPEG", "AV1", "VP8", "VP9")


def sdp_codec(describe: str) -> str:
    """Video codec named by the SDP (``H264`` / ``H265`` / ...)."""
    for codec in re.findall(r"a=rtpmap:\d+ ([A-Za-z0-9\-]+)/", describe):
        upper = codec.upper()
        if upper in _VIDEO_CODECS:
            return "H265" if upper == "HEVC" else upper
    return ""


def sdp_resolution(describe: str) -> str:
    """Frame size from the SDP (``"1920x1080"`` or ``""``).

    Hikvision sends ``a=x-dimensions:1920,1080``, others ``a=framesize:96
    1920-1080``; Dahua sends neither, so the size is read from the SPS.
    """
    m = (re.search(r"a=x-dimensions:\s*(\d+)\s*,\s*(\d+)", describe)
         or re.search(r"a=framesize:\d+\s+(\d+)-(\d+)", describe))
    if m:
        return f"{m.group(1)}x{m.group(2)}"
    return sps_resolution(describe)


class _Bits:
    """Bit reader over an SPS with emulation-prevention bytes removed."""

    def __init__(self, nal: bytes) -> None:
        clean, zeros = bytearray(), 0
        for byte in nal:
            if zeros >= 2 and byte == 3:
                zeros = 0
                continue
            clean.append(byte)
            zeros = zeros + 1 if byte == 0 else 0
        self.data, self.pos = bytes(clean), 0

    def u(self, n: int) -> int:
        value = 0
        for _ in range(n):
            if self.pos >= len(self.data) * 8:
                raise ValueError("SPS ended")
            value = (value << 1) | ((self.data[self.pos // 8] >> (7 - self.pos % 8)) & 1)
            self.pos += 1
        return value

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("bad Exp-Golomb")
        return (1 << zeros) - 1 + self.u(zeros)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k % 2 else -(k // 2)


def _h264_size(sps: bytes) -> tuple[int, int]:
    b = _Bits(sps[1:])                      # 1-byte NAL header
    profile = b.u(8)
    b.u(16)                                 # constraint flags + level
    b.ue()                                  # sps id
    chroma = 1
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = b.ue()
        if chroma == 3:
            b.u(1)
        b.ue(), b.ue()                      # bit depths
        b.u(1)
        if b.u(1):                          # scaling matrix
            for i in range(8 if chroma != 3 else 12):
                if b.u(1):
                    last = nxt = 8
                    for _ in range(16 if i < 6 else 64):
                        if nxt:
                            nxt = (last + b.se()) % 256
                        last = nxt or last
    b.ue()                                  # log2_max_frame_num
    poc = b.ue()
    if poc == 0:
        b.ue()
    elif poc == 1:
        b.u(1), b.se(), b.se()
        for _ in range(b.ue()):
            b.se()
    b.ue(), b.u(1)                          # ref frames, gaps
    w_mbs, h_map = b.ue() + 1, b.ue() + 1
    frame_mbs_only = b.u(1)
    if not frame_mbs_only:
        b.u(1)
    b.u(1)
    width, height = w_mbs * 16, (2 - frame_mbs_only) * h_map * 16
    if b.u(1):                              # cropping
        left, right, top, bottom = b.ue(), b.ue(), b.ue(), b.ue()
        cx = 1 if chroma == 0 else 2 if chroma in (1, 2) else 1
        cy = (1 if chroma in (0, 2, 3) else 2) * (2 - frame_mbs_only)
        width -= (left + right) * cx
        height -= (top + bottom) * cy
    return width, height


def _h265_size(sps: bytes) -> tuple[int, int]:
    b = _Bits(sps[2:])                      # 2-byte NAL header
    b.u(4)
    max_sub = b.u(3)
    b.u(1)
    b.u(96 - 8)                             # general profile_tier_level
    b.u(8)                                  # general_level_idc
    flags = [(b.u(1), b.u(1)) for _ in range(max_sub)]
    if max_sub:
        b.u(2 * (8 - max_sub))
    for profile_present, level_present in flags:
        if profile_present:
            b.u(88)
        if level_present:
            b.u(8)
    b.ue()                                  # sps id
    chroma = b.ue()
    if chroma == 3:
        b.u(1)
    width, height = b.ue(), b.ue()
    if b.u(1):                              # conformance window
        left, right, top, bottom = b.ue(), b.ue(), b.ue(), b.ue()
        cx = 2 if chroma in (1, 2) else 1
        cy = 2 if chroma == 1 else 1
        width -= (left + right) * cx
        height -= (top + bottom) * cy
    return width, height


def sps_resolution(describe: str) -> str:
    """Frame size from the SPS in ``a=fmtp`` (``sprop-sps`` / ``sprop-parameter-sets``)."""
    found = re.search(r"sprop-sps=([A-Za-z0-9+/=]+)", describe)
    parse = _h265_size
    if not found:
        found = re.search(r"sprop-parameter-sets=([A-Za-z0-9+/=]+)", describe)
        parse = _h264_size
    if not found:
        return ""
    try:
        raw = base64.b64decode(found.group(1) + "=" * (-len(found.group(1)) % 4))
        width, height = parse(raw)
    except (ValueError, IndexError, binascii.Error):
        return ""
    if not (16 <= width <= 16384 and 16 <= height <= 16384):
        return ""
    return f"{width}x{height}"


def sdp_fps(describe: str) -> float:
    m = re.search(r"a=(?:x-)?framerate:\s*([\d.]+)", describe)
    try:
        return float(m.group(1)) if m else 0.0
    except ValueError:
        return 0.0


def sdp_has_audio(describe: str) -> bool:
    return "m=audio" in describe


def sdp_video_control(describe: str, request_uri: str) -> str:
    """SETUP URL of the video track: absolute, relative to Content-Base, or the base."""
    base = request_uri
    m = re.search(r"^Content-Base:\s*(\S+)", describe, re.IGNORECASE | re.MULTILINE)
    if m:
        base = m.group(1)
    control, in_video = "", False
    for line in describe.splitlines():
        if line.startswith("m="):
            in_video = line.startswith("m=video")
        elif in_video and line.startswith("a=control:"):
            control = line.split(":", 1)[1].strip()
            break
    if not control or control == "*":
        return base
    if control.lower().startswith("rtsp://"):
        return control
    return base.rstrip("/") + "/" + control.lstrip("/")


# ---------- probe ----------

def probe(host: str, port: int = DEFAULT_PORT, path: str = "", username: str = "",
          password: str = "", timeout: float = TIMEOUT) -> ProbeResult:
    """Check the camera stage by stage; never raises for camera-side failures."""
    def fail(stage: str, message: str) -> ProbeResult:
        return ProbeResult(False, stage, message)

    if not host:
        return fail("network", t("no host given", "manzil ko'rsatilmagan"))
    where = f"{host}:{port}"

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except socket.gaierror:
        return fail("network", t(f"{host}: host not found (DNS)",
                                 f"{host} manzili topilmadi (DNS)"))
    except socket.timeout:
        return fail("network", t(f"{where} did not answer: camera off or on another network",
                                 f"{where} javob bermadi: kamera o'chiq yoki boshqa tarmoqda"))
    except OSError as exc:
        reason = exc.strerror or exc
        return fail("network", t(f"cannot connect to {where} ({reason})",
                                 f"{where} ga ulanib bo'lmadi ({reason})"))

    uri = build_rtsp_url(host, port, path)
    try:
        sock.settimeout(timeout)
        try:
            options = _request(sock, "OPTIONS", uri, 1)
        except (socket.timeout, OSError):
            return fail("rtsp", t(f"{where} is open but sends no RTSP reply: check the port",
                                  f"{where} ochiq, lekin RTSP javobi yo'q: portni tekshiring"))
        if not options.startswith("RTSP/"):
            return fail("rtsp", t(f"{where} is not an RTSP service",
                                  f"{where} RTSP xizmati emas"))

        describe = _request(sock, "DESCRIBE", uri, 2)
        code = _status(describe)
        challenge = ""
        if code == 401:
            if not username:
                return fail("auth", t("the camera wants a username and password",
                                      "kamera login va parol so'rayapti"))
            challenge = _challenge(describe)
            auth = (_digest_header(username, password, "DESCRIBE", uri, challenge)
                    if challenge.lower().startswith("digest")
                    else _basic_header(username, password))
            describe = _request(sock, "DESCRIBE", uri, 3, auth)
            code = _status(describe)
            if code == 401:
                return fail("auth", t("wrong username or password",
                                      "login yoki parol noto'g'ri"))
        if code == 404:
            return fail("rtsp", t(f"RTSP path not found: {path or '/'}",
                                  f"RTSP yo'li topilmadi: {path or '/'}"))
        if code >= 400:
            return fail("rtsp", t(f"the camera refused with RTSP {code}",
                                  f"kamera RTSP {code} bilan rad etdi"))
        if code == 0:
            return fail("rtsp", t("unreadable reply from the camera",
                                  "kameradan tushunarsiz javob"))

        # Some devices answer 200 for any path with a video-less SDP.
        if "m=video" not in describe:
            return fail("stream", t("the camera answered, but there is no video on this path",
                                    "kamera javob berdi, lekin bu yo'lda video yo'q"))

        setup_uri = sdp_video_control(describe, uri)
        setup_auth = ""
        if challenge.lower().startswith("digest"):
            setup_auth = _digest_header(username, password, "SETUP", setup_uri, challenge, nc=2)
        elif challenge:
            setup_auth = _basic_header(username, password)
        try:
            setup = _request(sock, "SETUP", setup_uri, 4, setup_auth,
                             ["Transport: RTP/AVP/TCP;unicast;interleaved=0-1"])
            setup_code = _status(setup)
            if setup_code == 461:          # no TCP transport: try UDP
                setup = _request(sock, "SETUP", setup_uri, 5, setup_auth,
                                 ["Transport: RTP/AVP;unicast;client_port=45678-45679"])
                setup_code = _status(setup)
        except (socket.timeout, OSError):
            return fail("stream", t("the camera did not answer SETUP",
                                    "kamera SETUP so'roviga javob bermadi"))
        if setup_code >= 400:
            return fail("stream", t(
                f"the camera answers but will not stream (SETUP {setup_code}): "
                "the stream/channel may be off or busy",
                f"kamera javob beradi, lekin oqim bermayapti (SETUP {setup_code}): "
                "oqim/kanal o'chiq yoki band bo'lishi mumkin"))

        res = ProbeResult(True, "ready", "", sdp_codec(describe), sdp_resolution(describe),
                          sdp_fps(describe), sdp_has_audio(describe))
        parts = [t("connected", "ulandi")]
        if res.codec:
            parts.append(res.codec)
        if res.resolution:
            parts.append(res.resolution)
        if res.fps:
            parts.append(f"{res.fps:g} fps")
        if res.audio:
            parts.append(t("audio", "audio bor"))
        res.message = " · ".join(parts)
        return res
    except (socket.timeout, OSError) as exc:
        # Some NVRs reset the connection when a missing channel is requested.
        return fail("rtsp", t(
            f"the camera dropped the connection ({exc.__class__.__name__}): "
            "this path/channel may not exist",
            f"kamera ulanishni uzdi ({exc.__class__.__name__}): "
            "bu yo'l/kanal mavjud bo'lmasligi mumkin"))
    finally:
        try:
            sock.close()
        except OSError:
            pass


def probe_url(url: str, timeout: float = TIMEOUT) -> ProbeResult:
    """:func:`probe` for a full ``rtsp://user:pass@host:port/path`` URL."""
    host, port, path, user, password = split_rtsp_url(url)
    return probe(host, port, path, user, password, timeout=timeout)
