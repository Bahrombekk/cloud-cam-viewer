"""RTSP probe and NVR scan against a fake RTSP server (no camera needed)."""
from __future__ import annotations

import hashlib
import re
import socket
import threading

import pytest

from cloudcam.sources.rtsp import (
    build_rtsp_url,
    channel_from_path,
    channel_path,
    probe,
    probe_url,
    redact_url,
    scan,
    split_rtsp_url,
    sub_path_candidates,
)
from cloudcam.sources.rtsp.probe import _digest_header, sdp_resolution, sdp_video_control

SDP = ("v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H265/90000\r\n"
       "a=x-dimensions:2560,1440\r\na=framerate:20\r\na=control:trackID=1\r\n"
       "m=audio 0 RTP/AVP 0\r\na=control:trackID=2\r\n")
REALM, NONCE = "IP Camera", "abc123"


class FakeCamera:
    """Minimal RTSP server: OPTIONS / DESCRIBE / SETUP with optional auth."""

    def __init__(self, paths, auth="digest", qop=True, user="admin", password="p@ss:w/rd",
                 setup_code=200, sdp_split=False):
        self.paths, self.auth, self.qop = paths, auth, qop
        self.user, self.password, self.setup_code = user, password, setup_code
        self.sdp_split = sdp_split
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.sessions = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self.sock.close()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.sessions += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _authorized(self, method, uri, header):
        if self.auth == "none":
            return True
        if not header:
            return False
        if self.auth == "basic":
            import base64
            want = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            return header == f"Basic {want}"
        f = dict(re.findall(r'(\w+)="?([^",]*)"?', header[len("Digest "):]))
        md5 = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
        ha1 = md5(f"{self.user}:{REALM}:{self.password}")
        ha2 = md5(f"{method}:{f.get('uri')}")
        if self.qop:
            want = md5(f"{ha1}:{NONCE}:{f.get('nc')}:{f.get('cnonce')}:auth:{ha2}")
        else:
            want = md5(f"{ha1}:{NONCE}:{ha2}")
        return f.get("username") == self.user and f.get("response") == want

    def _handle(self, conn):
        with conn:
            buf = b""
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                buf += data
                while b"\r\n\r\n" in buf:
                    req, buf = buf.split(b"\r\n\r\n", 1)
                    self._reply(conn, req.decode())

    def _reply(self, conn, req):
        first = req.split("\r\n")[0]
        method, uri = first.split()[:2]
        cseq = re.search(r"CSeq: (\d+)", req).group(1)
        auth = re.search(r"(?m)^Authorization: (.*)$", req)
        auth = auth.group(1).strip() if auth else ""
        path = re.sub(r"^rtsp://[^/]+", "", uri)
        head = f"CSeq: {cseq}\r\n"
        if method == "OPTIONS":
            conn.sendall(f"RTSP/1.0 200 OK\r\n{head}Public: DESCRIBE, SETUP\r\n\r\n".encode())
            return
        if not self._authorized(method, uri, auth):
            if self.auth == "digest":
                qop = ', qop="auth"' if self.qop else ""
                chal = f'Digest realm="{REALM}", nonce="{NONCE}"{qop}'
            else:
                chal = f'Basic realm="{REALM}"'
            conn.sendall(f"RTSP/1.0 401 Unauthorized\r\n{head}"
                         f"WWW-Authenticate: {chal}\r\n\r\n".encode())
            return
        if method == "DESCRIBE":
            if path not in self.paths:
                conn.sendall(f"RTSP/1.0 404 Not Found\r\n{head}\r\n".encode())
                return
            body = self.paths[path].encode()
            msg = (f"RTSP/1.0 200 OK\r\n{head}Content-Type: application/sdp\r\n"
                   f"Content-Base: {uri}/\r\nContent-Length: {len(body)}\r\n\r\n").encode()
            if self.sdp_split:            # headers and body in separate segments
                conn.sendall(msg)
                import time
                time.sleep(0.05)
                conn.sendall(body)
            else:
                conn.sendall(msg + body)
            return
        if method == "SETUP":
            code = self.setup_code
            conn.sendall(f"RTSP/1.0 {code} X\r\n{head}Session: 1\r\n\r\n".encode())


@pytest.fixture
def cam():
    made = []

    def make(*a, **kw):
        c = FakeCamera(*a, **kw)
        made.append(c)
        return c
    yield make
    for c in made:
        c.close()


def test_digest_with_qop_full_success(cam):
    c = cam({"/Streaming/Channels/101": SDP})
    r = probe("127.0.0.1", c.port, "/Streaming/Channels/101", "admin", "p@ss:w/rd")
    assert r.ok and r.stage == "ready"
    assert (r.codec, r.resolution, r.fps, r.audio) == ("H265", "2560x1440", 20.0, True)


def test_digest_without_qop_and_basic(cam):
    c1 = cam({"/a": SDP}, qop=False)
    assert probe("127.0.0.1", c1.port, "/a", "admin", "p@ss:w/rd").ok
    c2 = cam({"/a": SDP}, auth="basic")
    assert probe("127.0.0.1", c2.port, "/a", "admin", "p@ss:w/rd").ok


def test_sdp_body_arriving_after_headers_is_read(cam):
    c = cam({"/a": SDP}, sdp_split=True)
    assert probe("127.0.0.1", c.port, "/a", "admin", "p@ss:w/rd").codec == "H265"


def test_stage_errors(cam):
    c = cam({"/a": SDP})
    assert probe("127.0.0.1", c.port, "/a").stage == "auth"                  # no creds
    assert probe("127.0.0.1", c.port, "/a", "admin", "bad").stage == "auth"  # wrong
    r = probe("127.0.0.1", c.port, "/missing", "admin", "p@ss:w/rd")
    assert (r.ok, r.stage) == (False, "rtsp") and "/missing" in r.message
    novideo = cam({"/a": "v=0\r\nm=audio 0 RTP/AVP 0\r\n"}, auth="none")
    assert probe("127.0.0.1", novideo.port, "/a").stage == "stream"
    refused = cam({"/a": SDP}, auth="none", setup_code=455)
    r = probe("127.0.0.1", refused.port, "/a")
    assert r.stage == "stream" and "455" in r.message


def test_closed_port_is_a_network_error():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert probe("127.0.0.1", port, "/a", timeout=1).stage == "network"


def test_not_rtsp_service():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def http():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        conn.close()
    threading.Thread(target=http, daemon=True).start()
    r = probe("127.0.0.1", srv.getsockname()[1], "/a", timeout=2)
    srv.close()
    assert r.stage == "rtsp" and not r.ok


def test_probe_url_decodes_credentials(cam):
    c = cam({"/cam/realmonitor?channel=1&subtype=0": SDP})
    url = build_rtsp_url("127.0.0.1", c.port, "/cam/realmonitor?channel=1&subtype=0",
                         "admin", "p@ss:w/rd")
    assert "p%40ss%3Aw%2Frd" in url
    assert probe_url(url).ok


def test_url_helpers():
    assert split_rtsp_url("rtsp://u:p%40x@[::1]:8554/a?b=1") == ("::1", 8554, "/a?b=1", "u", "p@x")
    assert split_rtsp_url("rtsp://h/x")[1] == 554
    assert build_rtsp_url("::1", 554, "a") == "rtsp://[::1]:554/a"
    assert redact_url("rtsp://admin:secret@h:554/x") == "rtsp://admin:***@h:554/x"
    assert redact_url("rtsp://h/x") == "rtsp://h/x"
    with pytest.raises(ValueError):
        split_rtsp_url("http://h/x")


def test_digest_nc_increments():
    h1 = _digest_header("u", "p", "SETUP", "/x", 'Digest realm="r", nonce="n", qop="auth"', nc=2)
    assert "nc=00000002" in h1
    h2 = _digest_header("u", "p", "DESCRIBE", "/x", 'Digest realm="r", nonce="n"')
    assert "qop" not in h2


def test_sdp_helpers():
    assert sdp_resolution("a=framesize:96 1280-720") == "1280x720"
    assert sdp_resolution("nothing") == ""
    d = "Content-Base: rtsp://h/x/\r\nm=video 0\r\na=control:trackID=1\r\n"
    assert sdp_video_control(d, "rtsp://h/x") == "rtsp://h/x/trackID=1"
    assert sdp_video_control("m=video 0\r\na=control:*\r\n", "rtsp://h/x") == "rtsp://h/x"


def test_vendor_paths_and_channels():
    assert channel_path("hikvision", 3) == "/Streaming/Channels/301"
    assert channel_path("hikvision", 12, "sub") == "/Streaming/Channels/1202"
    assert channel_path("dahua", 2, "sub") == "/cam/realmonitor?channel=2&subtype=1"
    assert channel_path("reolink", 1) == "/h264Preview_01_main"
    assert channel_from_path("/Streaming/Channels/1602") == 16
    assert channel_from_path("/unicast/c3/s1/live") == 3
    assert channel_from_path("/stream1") == 1
    assert sub_path_candidates("/Streaming/Channels/101") == ["/Streaming/Channels/102"]
    assert sub_path_candidates("/cam/realmonitor?channel=1&subtype=0") == [
        "/cam/realmonitor?channel=1&subtype=1"]
    assert sub_path_candidates("/LiveMedia/ch1/Media1") == ["/LiveMedia/ch1/Media2"]
    assert sub_path_candidates("") == []


def test_scan_detects_vendor_and_skips_phantom_channels(cam):
    paths = {channel_path("dahua", ch): SDP for ch in (1, 2, 4)}
    paths[channel_path("dahua", 1, "sub")] = SDP
    paths[channel_path("dahua", 3)] = SDP           # advertised ...
    c = cam(paths)
    hits = scan("127.0.0.1", c.port, "admin", "p@ss:w/rd", channels=8, timeout=2)
    assert [h.channel for h in hits] == [1, 2, 3, 4]
    assert hits[0].sub_path.endswith("subtype=1") and hits[1].sub_path == ""

    refused = cam({channel_path("hikvision", 1): SDP}, setup_code=455)
    assert scan("127.0.0.1", refused.port, "admin", "p@ss:w/rd", channels=4, timeout=2) == []


def test_scan_stops_on_wrong_password(cam):
    c = cam({channel_path("hikvision", 1): SDP})
    assert scan("127.0.0.1", c.port, "admin", "wrong", timeout=2) == []
    assert c.sessions == 1          # detection stopped at the auth failure


def test_generic_path_scans_once(cam):
    c = cam({"/stream1": SDP}, auth="none")
    hits = scan("127.0.0.1", c.port, vendor="generic", timeout=2)
    assert [h.path for h in hits] == ["/stream1"]
