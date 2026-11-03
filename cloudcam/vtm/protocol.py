"""VTM/VTDU bulut oqimi protokoli — TCP freymlash, protobuf so'rov/javoblar, RTP.

`pyezvizapi` 1.0.5.0 (`pyezvizapi/stream.py`) dan ko'chirilgan va qisqartirilgan
(Apache-2.0, (c) Renier Moorcroft; litsenziya — `LICENSE-pyezvizapi`).
O'zgarishlar:
  * faqat jonli oqim uchun kerakli qism qoldirildi (lokal oqim, cloud replay,
    trace, VTDU peer/stop so'rovlari va PS deshifrlash olib tashlandi —
    deshifrlash `cloudcam.decrypt_proxy` da o'zimizniki);
  * [[build_vtm_url]] ga `stream_type` qo'shildi (substream uchun). Ilgari
    biz tayyor URL'dagi `stream=1` ni regex bilan almashtirardik — upstream
    format o'zgarsa bu JIMGINA ishlamay qolardi;
  * xatolar `VtmError` / `DeviceOffline` (xabar matnlari saqlangan:
    `decrypt_proxy` "offline"/"unreachable" ni qidiradi).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import IntEnum
from ipaddress import IPv6Address, ip_address
import socket
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

VTM_MAGIC = 0x24
VTM_HEADER_SIZE = 8
CLIENT_VERSION = "v3.6.3.20221124"


class VtmError(Exception):
    """VTM/VTDU protokoli yoki bulut metama'lumoti xatosi."""


class DeviceOffline(VtmError):
    """Qurilma javob bermayapti (soket jim qoldi)."""


class VtmChannel(IntEnum):
    MESSAGE = 0x00
    STREAM = 0x01
    ENCRYPTED_MESSAGE = 0x0A
    ENCRYPTED_STREAM = 0x0B


class VtmMessageCode(IntEnum):
    KEEPALIVE_REQ = 0x132
    KEEPALIVE_RSP = 0x133
    STREAMINFO_REQ = 0x13B
    STREAMINFO_RSP = 0x13C
    STREAMINFO_NOTIFY = 0x13D
    STREAM_VTMSTREAM_ECDH_NOTIFY = 0x14A


_KNOWN_CHANNELS = frozenset(int(c) for c in VtmChannel)


@dataclass(frozen=True)
class VtmPacket:
    channel: int
    length: int
    sequence: int
    message_code: int
    body: bytes = b""

    @property
    def encrypted(self) -> bool:
        return self.channel in (VtmChannel.ENCRYPTED_MESSAGE, VtmChannel.ENCRYPTED_STREAM)


@dataclass(frozen=True)
class StreamInfoResponse:
    """StreamInfoRsp protobuf'idan ma'lum maydonlar."""

    result: int | None = None
    datakey: int | None = None
    streamhead: str | None = None
    streamssn: str | None = None
    vtmstreamkey: str | None = None
    serverinfo: str | None = None
    streamurl: str | None = None
    srvinfo: str | None = None
    aesmd5: str | None = None
    udptransinfo: str | None = None
    peerpbkey: str | None = None
    pdslist: tuple[bytes, ...] = ()
    srvipv6_addr: str | None = None


SocketFactory = Callable[[tuple[str, int], "float | None"], Any]


class VtmStreamClient:
    """VTM TCP oqimining sinxron klienti.

    TCP ulanish -> xabar kanalida `StreamInfoReq` -> `StreamInfoRsp` (kerak
    bo'lsa boshqa serverga yo'naltirish) -> oqim kanalidagi paketlar.
    `with` bilan ishlatiladi, o'qishdan oldin [[start]] chaqiriladi.
    """

    def __init__(
        self,
        stream_url: str,
        *,
        timeout: float | None = 10.0,
        client_version: str = CLIENT_VERSION,
        socket_factory: SocketFactory = socket.create_connection,
    ) -> None:
        self.stream_url = stream_url
        self.timeout = timeout
        self.client_version = client_version
        self._socket_factory = socket_factory
        self._socket: Any | None = None
        self._sequence = 0
        self.stream_info: StreamInfoResponse | None = None

    def __enter__(self) -> VtmStreamClient:
        return self.connect()

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def connect(self) -> VtmStreamClient:
        if self._socket is not None:
            return self
        host, port, _path, _params = parse_vtm_url(self.stream_url)
        sock = self._socket_factory((host, port), self.timeout)
        if self.timeout is not None:
            sock.settimeout(self.timeout)
        self._socket = sock
        return self

    def close(self) -> None:
        sock = self._socket
        self._socket = None
        if sock is not None:
            sock.close()

    def send_packet(
        self,
        body: bytes,
        *,
        channel: int = VtmChannel.MESSAGE,
        message_code: int = VtmMessageCode.STREAMINFO_REQ,
    ) -> int:
        sock = self._require_socket()
        sequence = self._sequence
        sock.sendall(encode_vtm_packet(body, channel=channel,
                                       message_code=message_code, sequence=sequence))
        self._sequence = (self._sequence + 1) & 0xFFFF
        return sequence

    def read_packet(self) -> VtmPacket:
        header = decode_vtm_header(self._recv_exact(VTM_HEADER_SIZE))
        body = self._recv_exact(header.length)
        return VtmPacket(channel=header.channel, length=header.length,
                         sequence=header.sequence, message_code=header.message_code,
                         body=body)

    def start(
        self,
        *,
        vtm_stream_key: str | None = None,
        max_control_packets: int = 20,
        max_redirects: int = 3,
    ) -> StreamInfoResponse:
        """Oqim ma'lumotini so'raydi va `StreamInfoRsp` ni qaytaradi."""
        redirect_count = 0
        while True:
            self.connect()
            self.send_packet(build_stream_info_request(
                self.stream_url, vtm_stream_key=vtm_stream_key,
                client_version=self.client_version))
            for _ in range(max_control_packets):
                packet = self.read_packet()
                if packet.message_code != VtmMessageCode.STREAMINFO_RSP:
                    continue
                self.stream_info = parse_stream_info_response(packet.body)
                redirect_url = self.stream_info.streamurl
                redirect_key = self.stream_info.vtmstreamkey
                if (self.stream_info.result and redirect_url and redirect_key
                        and redirect_count < max_redirects):
                    self.close()
                    self.stream_url = redirect_url
                    vtm_stream_key = redirect_key
                    redirect_count += 1
                    break
                return self.stream_info
            else:
                raise VtmError("Timed out waiting for VTM stream info response")

    def send_keepalive(
        self,
        stream_ssn: str | None = None,
        *,
        message_code: int = VtmMessageCode.KEEPALIVE_REQ,
    ) -> int:
        stream_ssn = stream_ssn or (
            self.stream_info.streamssn if self.stream_info is not None else None)
        if not stream_ssn:
            raise VtmError("Cannot send keepalive without a stream session")
        return self.send_packet(build_stream_keepalive_request(stream_ssn),
                                message_code=message_code)

    def iter_packets(
        self,
        *,
        max_packets: int | None = None,
        include_control: bool = False,
        keepalive_interval: float | None = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> Iterator[VtmPacket]:
        """Oqim paketlari. Keepalive ichkarida javoblanadi."""
        seen = 0
        last_keepalive = monotonic()
        while max_packets is None or seen < max_packets:
            if (keepalive_interval is not None
                    and self.stream_info is not None
                    and self.stream_info.streamssn
                    and monotonic() - last_keepalive >= keepalive_interval):
                self.send_keepalive()
                last_keepalive = monotonic()

            packet = self.read_packet()
            if packet.message_code == VtmMessageCode.KEEPALIVE_REQ:
                self.send_keepalive(message_code=VtmMessageCode.KEEPALIVE_RSP)
                last_keepalive = monotonic()
                if include_control:
                    seen += 1
                    yield packet
                continue
            if packet.channel in (VtmChannel.STREAM, VtmChannel.ENCRYPTED_STREAM):
                seen += 1
                yield packet
                continue
            if include_control:
                seen += 1
                yield packet

    def iter_payloads(self, *, max_packets: int | None = None) -> Iterator[bytes]:
        for packet in self.iter_packets(max_packets=max_packets):
            yield packet.body

    def _require_socket(self) -> Any:
        if self._socket is None:
            raise VtmError("VTM socket is not connected")
        return self._socket

    def _recv_exact(self, length: int) -> bytes:
        sock = self._require_socket()
        chunks: list[bytes] = []
        remaining = length
        while remaining:
            try:
                chunk = sock.recv(remaining)
            except TimeoutError as err:          # socket.timeout ham (3.10+)
                raise DeviceOffline(
                    "Device offline or unreachable: timed out waiting for VTM stream data"
                ) from err
            if not chunk:
                raise VtmError("VTM socket closed while reading packet")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


# ---- freymlash ----------------------------------------------------------

def encode_vtm_packet(
    body: bytes,
    *,
    channel: int = VtmChannel.MESSAGE,
    message_code: int = VtmMessageCode.STREAMINFO_REQ,
    sequence: int = 0,
) -> bytes:
    """8 baytli sarlavha + tana: magic, kanal, uzunlik, ketma-ketlik, xabar kodi."""
    if len(body) > 0xFFFF:
        raise VtmError("VTM packet body is too large")
    header = bytes((
        VTM_MAGIC,
        int(channel) & 0xFF,
        *len(body).to_bytes(2, "big"),
        *(sequence & 0xFFFF).to_bytes(2, "big"),
        *int(message_code).to_bytes(2, "big"),
    ))
    return header + body


def decode_vtm_header(header: bytes) -> VtmPacket:
    if len(header) != VTM_HEADER_SIZE:
        raise VtmError("VTM header must be exactly 8 bytes")
    if header[0] != VTM_MAGIC:
        raise VtmError("VTM magic byte not found")
    channel = header[1]
    if channel not in _KNOWN_CHANNELS:
        raise VtmError(f"Unknown VTM channel: 0x{channel:02x}")
    return VtmPacket(
        channel=channel,
        length=int.from_bytes(header[2:4], "big"),
        sequence=int.from_bytes(header[4:6], "big"),
        message_code=int.from_bytes(header[6:8], "big"),
    )


def decode_vtm_packet(packet: bytes) -> VtmPacket:
    header = decode_vtm_header(packet[:VTM_HEADER_SIZE])
    body = packet[VTM_HEADER_SIZE:]
    if len(body) != header.length:
        raise VtmError(f"VTM packet length mismatch: header={header.length} body={len(body)}")
    return VtmPacket(channel=header.channel, length=header.length, sequence=header.sequence,
                     message_code=header.message_code, body=body)


# ---- URL ----------------------------------------------------------------

STREAM_MAIN = 1
STREAM_SUB = 2


def build_vtm_url(
    host: str,
    port: int,
    serial: str,
    stream_biz_url: str,
    vtdu_token: str,
    *,
    channel: int = 1,
    client_type: int = 9,
    stream_type: int = STREAM_MAIN,
    timestamp_ms: int | None = None,
) -> str:
    """Jonli oqim uchun `ysproto://` URL.

    `stream_type`: 1 — asosiy oqim, 2 — kichik oqim (substream). Substream'i
    yo'q kamerada bulut asosiysini beradi yoki jim qoladi."""
    if stream_type not in (STREAM_MAIN, STREAM_SUB):
        raise VtmError(f"Unknown stream type: {stream_type}")
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    params = {
        **_parse_stream_biz_params(stream_biz_url),
        "dev": serial,
        "chn": str(channel),
        "stream": str(stream_type),
        "cln": str(client_type),
        "isp": "0",
        "auth": "1",
        "ssn": vtdu_token,
        "vip": "0",
        "timestamp": str(timestamp_ms),
    }
    return f"ysproto://{_format_url_host(host)}:{port}/live?{urlencode(params)}"


def parse_vtm_url(url: str) -> tuple[str, int, str, dict[str, str]]:
    try:
        parsed = urlparse(url)
    except ValueError as err:
        raise VtmError("Invalid VTM URL host or port") from err
    if parsed.scheme != "ysproto":
        raise VtmError("Invalid VTM URL scheme")
    try:
        port = parsed.port
    except ValueError as err:
        raise VtmError("Invalid VTM URL host or port") from err
    if not parsed.hostname or port is None:
        raise VtmError("Invalid VTM URL host or port")
    return parsed.hostname, port, parsed.path, dict(parse_qsl(parsed.query,
                                                              keep_blank_values=True))


def _format_url_host(host: str) -> str:
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        return host
    try:
        address = ip_address(host)
    except ValueError:
        return host
    return f"[{host}]" if isinstance(address, IPv6Address) else host


def _parse_stream_biz_params(stream_biz_url: str) -> dict[str, str]:
    parsed = urlparse(stream_biz_url)
    query = parsed.query or stream_biz_url.lstrip("?")
    return dict(parse_qsl(query, keep_blank_values=True))


# ---- protobuf so'rov/javoblar -------------------------------------------

def build_stream_info_request(
    stream_url: str,
    *,
    vtm_stream_key: str | None = None,
    client_version: str = CLIENT_VERSION,
) -> bytes:
    parts = [_proto_string(1, stream_url)]
    if vtm_stream_key:
        parts.append(_proto_string(2, vtm_stream_key))
    parts.extend((
        _proto_string(3, client_version),
        _proto_varint(4, 0),
        _proto_string(6, client_version),
    ))
    return b"".join(parts)


def build_stream_keepalive_request(stream_ssn: str) -> bytes:
    return _proto_bytes(1, stream_ssn.encode())


def parse_stream_info_response(data: bytes) -> StreamInfoResponse:
    f = _read_proto_fields(data, "StreamInfoRsp")
    return StreamInfoResponse(
        result=_proto_last_int(f, 1),
        datakey=_proto_last_int(f, 2),
        streamhead=_proto_last_str(f, 3),
        streamssn=_proto_last_str(f, 4),
        vtmstreamkey=_proto_last_str(f, 5),
        serverinfo=_proto_last_str(f, 6),
        streamurl=_proto_last_str(f, 7),
        srvinfo=_proto_last_str(f, 8),
        aesmd5=_proto_last_str(f, 9),
        udptransinfo=_proto_last_str(f, 10),
        peerpbkey=_proto_last_str(f, 11),
        pdslist=tuple(v for v in f.get(12, []) if isinstance(v, bytes)),
        srvipv6_addr=_proto_last_str(f, 13),
    )


ProtoValue = int | bytes


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise VtmError("Negative protobuf varints are not supported")
    out = bytearray()
    while True:
        to_write = value & 0x7F
        value >>= 7
        if value:
            out.append(to_write | 0x80)
        else:
            out.append(to_write)
            return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift >= 64:
            break
    raise VtmError("Malformed protobuf varint")


def _proto_key(field: int, wire_type: int) -> bytes:
    return _encode_varint((field << 3) | wire_type)


def _proto_varint(field: int, value: int) -> bytes:
    return _proto_key(field, 0) + _encode_varint(value)


def _proto_bytes(field: int, value: bytes) -> bytes:
    return _proto_key(field, 2) + _encode_varint(len(value)) + value


def _proto_string(field: int, value: str) -> bytes:
    return _proto_bytes(field, value.encode())


def _read_proto_fields(data: bytes, message_name: str) -> dict[int, list[ProtoValue]]:
    values: dict[int, list[ProtoValue]] = {}
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        field, wire_type = key >> 3, key & 0x07
        if wire_type == 0:
            value, pos = _read_varint(data, pos)
            values.setdefault(field, []).append(value)
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise VtmError(f"{message_name} length-delimited field exceeds payload")
            values.setdefault(field, []).append(data[pos:pos + length])
            pos += length
        else:
            raise VtmError(f"Unsupported {message_name} wire type: {wire_type}")
    return values


def _proto_last_int(fields: dict[int, list[ProtoValue]], field: int) -> int | None:
    values = fields.get(field)
    if not values:
        return None
    return values[-1] if isinstance(values[-1], int) else None


def _proto_last_str(fields: dict[int, list[ProtoValue]], field: int) -> str | None:
    values = fields.get(field)
    if not values or isinstance(values[-1], int):
        return None
    return values[-1].decode("utf-8", errors="replace")


# ---- RTP ----------------------------------------------------------------

def rtp_payload(data: bytes) -> bytes:
    """RTP paketining foydali yuki (fixed, CSRC, extension, padding olib tashlanadi)."""
    if len(data) < 12:
        raise VtmError("RTP packet is too short")
    if data[0] >> 6 != 2:
        raise VtmError("Unsupported RTP version")
    has_padding = bool(data[0] & 0x20)
    has_extension = bool(data[0] & 0x10)
    offset = 12 + (data[0] & 0x0F) * 4
    if len(data) < offset:
        raise VtmError("RTP CSRC header exceeds packet length")
    if has_extension:
        if len(data) < offset + 4:
            raise VtmError("RTP extension header exceeds packet length")
        offset += 4 + int.from_bytes(data[offset + 2:offset + 4], "big") * 4
        if len(data) < offset:
            raise VtmError("RTP extension payload exceeds packet length")
    payload = data[offset:]
    if has_padding:
        if not payload:
            raise VtmError("RTP padding set without payload")
        padding_len = payload[-1]
        if padding_len == 0 or padding_len > len(payload):
            raise VtmError("Invalid RTP padding length")
        payload = payload[:-padding_len]
    return payload
