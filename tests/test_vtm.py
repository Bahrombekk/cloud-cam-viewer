"""`cloudcam.vtm` — pyezvizapi'dan ko'chirilgan bulut transporti.

Ko'chirish paytida natijalar upstream (1.0.5.0) bilan baytma-bayt solishtirilgan;
bu testlar o'sha xatti-harakatni tarmoqsiz qo'riqlaydi.
"""
import base64
import json
import socket

import pytest

from cloudcam.vtm import cloud, protocol as P
from cloudcam.vtm import DeviceOffline, VtmError


# ---- freymlash / protobuf ------------------------------------------------

def test_packet_roundtrip():
    pkt = P.encode_vtm_packet(b"abc", channel=P.VtmChannel.STREAM,
                              message_code=P.VtmMessageCode.KEEPALIVE_REQ, sequence=0x1_0005)
    assert pkt[:8] == bytes([0x24, 0x01, 0, 3, 0, 5, 0x01, 0x32])
    d = P.decode_vtm_packet(pkt)
    assert (d.channel, d.length, d.sequence, d.message_code, d.body) == (1, 3, 5, 0x132, b"abc")


def test_bad_header_is_rejected():
    with pytest.raises(VtmError):
        P.decode_vtm_header(b"\x25" + b"\0" * 7)
    with pytest.raises(VtmError):
        P.decode_vtm_header(b"\x24\x07" + b"\0" * 6)          # noma'lum kanal


def _rsp(**f):
    parts = []
    for num, val in f.items():
        n = int(num[1:])
        parts.append(P._proto_varint(n, val) if isinstance(val, int) else P._proto_string(n, val))
    return b"".join(parts)


def test_stream_info_response_fields():
    r = P.parse_stream_info_response(_rsp(f1=0, f4="SSN", f7="ysproto://h:1/live"))
    assert r.result == 0 and r.streamssn == "SSN" and r.streamurl == "ysproto://h:1/live"


# ---- VtmStreamClient soxta soket bilan -----------------------------------

class FakeSock:
    def __init__(self, data: bytes, *, then_timeout=False):
        self._buf = bytearray(data)
        self._then_timeout = then_timeout
        self.sent = []
        self.closed = False

    def settimeout(self, t):
        pass

    def sendall(self, b):
        self.sent.append(P.decode_vtm_packet(b))

    def recv(self, n):
        if not self._buf:
            if self._then_timeout:
                raise socket.timeout("timed out")
            return b""
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def close(self):
        self.closed = True


def _msg(code, body=b"", ch=P.VtmChannel.MESSAGE):
    return P.encode_vtm_packet(body, channel=ch, message_code=code)


URL = "ysproto://1.2.3.4:8554/live?dev=S"


def test_start_follows_redirect_then_streams_and_answers_keepalive():
    first = FakeSock(_msg(P.VtmMessageCode.STREAMINFO_RSP,
                          _rsp(f1=1, f5="VK", f7="ysproto://5.6.7.8:9/live?dev=S")))
    second = FakeSock(
        _msg(P.VtmMessageCode.STREAMINFO_RSP, _rsp(f1=0, f4="SSN"))
        + _msg(P.VtmMessageCode.KEEPALIVE_REQ)
        + _msg(0, b"VIDEO", ch=P.VtmChannel.STREAM))
    socks = iter([first, second])
    addrs = []

    def factory(addr, timeout):
        addrs.append(addr)
        return next(socks)

    c = P.VtmStreamClient(URL, socket_factory=factory)
    with c:
        info = c.start()
        assert info.streamssn == "SSN"
        assert [p.body for p in c.iter_packets(max_packets=1)] == [b"VIDEO"]
    assert addrs == [("1.2.3.4", 8554), ("5.6.7.8", 9)]
    assert first.closed and second.closed
    assert second.sent[-1].message_code == P.VtmMessageCode.KEEPALIVE_RSP


def test_silent_socket_is_reported_as_offline():
    """Matn muhim: decrypt_proxy "offline"/"unreachable" bo'yicha bayroq qo'yadi."""
    c = P.VtmStreamClient(URL, socket_factory=lambda a, t: FakeSock(b"", then_timeout=True))
    with c, pytest.raises(DeviceOffline, match="offline or unreachable"):
        c.start()


# ---- RTP ------------------------------------------------------------------

def test_rtp_payload_strips_csrc_extension_and_padding():
    hdr = bytes([0xB1, 96, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0])   # v2, P, X, CC=1
    csrc = b"\0" * 4
    ext = b"\xbe\xde\x00\x01" + b"\0" * 4
    pkt = hdr + csrc + ext + b"DATA" + b"\0\0\x03"
    assert P.rtp_payload(pkt) == b"DATA"


def test_rtp_payload_rejects_short_packets():
    with pytest.raises(VtmError):
        P.rtp_payload(b"\x80\x60")


# ---- bulut: metama'lumotdan URL -----------------------------------------

PL = {"resourceInfos": [{"deviceSerial": "S", "localIndex": "2", "resourceId": "R",
                         "streamBizUrl": "?biz=1"}],
      "VTM": {"R": {"externalIp": "9.9.9.9", "port": "8554"}}}


def test_stream_info_builds_url():
    url = cloud.get_cloud_stream_info(PL, {"tokens": ["T"]}, "S", channel=2,
                                      stream_type=P.STREAM_SUB)["stream_url"]
    host, port, _, q = P.parse_vtm_url(url)
    assert (host, port) == ("9.9.9.9", 8554)
    assert q["ssn"] == "T" and q["chn"] == "2" and q["stream"] == "2" and q["biz"] == "1"


def test_missing_resource_message_is_stable():
    """decrypt_proxy._is_missing_resource shu matnni qidiradi."""
    with pytest.raises(VtmError, match="Could not find VTM resource"):
        cloud.get_cloud_stream_info(PL, {"tokens": ["T"]}, "S", channel=3)


# ---- bulut: HTTP ----------------------------------------------------------

class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def json(self):
        return self._d


class FakeSession:
    def __init__(self, responses):
        self._r = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return self._r.pop(0)


def _jwt(sign="SIGN"):
    claims = base64.urlsafe_b64encode(json.dumps({"s": sign}).encode()).decode().rstrip("=")
    return f"h.{claims}.x"


class FakeClient:
    def __init__(self, responses, api_url="apiisgp.hik-connect.com"):
        self.api_url = api_url
        self.session_id = _jwt()
        self.session = FakeSession(responses)
        self.refreshed = 0
        self.service_urls = {}

    def refresh_session(self, allow_login=True):
        assert allow_login is False, "bola-jarayon parol bilan login qilmasligi kerak"
        self.refreshed += 1
        self.session_id = _jwt("NEW")

    def get_service_urls(self):
        return self.service_urls


def test_pagelist_is_fetched_across_all_pages():
    ok = {"code": 200}
    c = FakeClient([
        Resp({"meta": ok, "resourceInfos": [1, 2], "VTM": {"a": 1}, "page": {"hasNext": True}}),
        Resp({"meta": ok, "resourceInfos": [3], "VTM": {"b": 2}, "page": {"hasNext": False}}),
    ])
    pl = cloud.fetch_vtm_page_list(c)
    assert pl["resourceInfos"] == [1, 2, 3] and pl["VTM"] == {"a": 1, "b": 2}
    assert [p["offset"] for _, p in c.session.calls] == [0, 2]


def test_pagelist_error_is_not_returned_as_an_empty_list():
    """Ilgari meta tekshirilmasdi: eskirgan sessiyadagi bo'sh ro'yxat keshga
    yozilib, hisobdagi hamma kamera soatgacha "topilmadi" bo'lardi."""
    c = FakeClient([Resp({"meta": {"code": 401}})])
    with pytest.raises(VtmError, match="meta.code 401"):
        cloud.fetch_vtm_page_list(c)


def test_vtdu_token_refreshes_the_session_on_401():
    c = FakeClient([Resp({}, 401), Resp({"retcode": 0, "tokens": ["T"]})])
    assert cloud.fetch_vtdu_token(c)["tokens"] == ["T"]
    assert c.refreshed == 1
    (url1, p1), (url2, p2) = c.session.calls
    assert url1 == "https://sgpauth.ezvizlife.com/vtdutoken2"
    assert p1["sign"] == "SIGN" and p2["sign"] == "NEW"


def test_auth_address_from_service_urls_must_be_a_cloud_domain():
    """`sessionId` auth manziliga yuboriladi — begona domen rad etiladi."""
    c = FakeClient([], api_url="api.hik-connect.com")       # regiondan chiqarib bo'lmaydi
    c.service_urls = {"authAddr": "https://evil.example.com"}
    with pytest.raises(VtmError, match="not allowed"):
        cloud.auth_base_url(c)
    c2 = FakeClient([], api_url="api.hik-connect.com")
    c2.service_urls = {"authAddr": "sgpauth.ezvizlife.com"}
    assert cloud.auth_base_url(c2) == "https://sgpauth.ezvizlife.com"


def test_null_auth_address_is_an_error():
    c = FakeClient([], api_url="api.hik-connect.com")
    c.service_urls = {"authAddr": "https://null"}
    with pytest.raises(VtmError, match="Missing authAddr"):
        cloud.auth_base_url(c)
