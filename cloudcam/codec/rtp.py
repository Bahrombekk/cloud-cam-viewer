"""RTP depaketlash — HEVC va H.264.

RTP paketlaridan Annex-B NAL oqimini yig'adi (FU bo'laklari, AP/STAP
to'plamlari) va [[nal]] dagi deshifrlovchiga beradi.
"""
from __future__ import annotations

from pyezvizapi.stream import rtp_payload

from .nal import HEVC_VIDEO_PT, _NalDecryptBase

class HevcRtpDecryptor(_NalDecryptBase):
    """RTP/HEVC paketlarini Annex-B ga aylantirib, NAL body ni dekodlaydi."""

    def __init__(self, key: str, *, cache_key: str | None = None):
        super().__init__(key, cache_key=cache_key)
        self._cur = None       # joriy FU (fragment) yig'indisi

    def feed(self, rtp_packet: bytes) -> bytes:
        """Bitta RTP paketdan Annex-B bayt qaytaradi (bo'sh bo'lishi mumkin)."""
        if (rtp_packet[1] & 0x7F) != HEVC_VIDEO_PT:
            return b""
        pl = rtp_payload(rtp_packet)
        if len(pl) < 3:
            return b""
        out = bytearray()
        nal_type = (pl[0] >> 1) & 0x3F
        if nal_type == 49:  # FU — bo'lingan NAL
            fu = pl[2]
            start_bit = fu >> 7
            end_bit = (fu >> 6) & 1
            fu_type = fu & 0x3F
            if start_bit:
                nh0 = ((pl[0] & 0x81) | (fu_type << 1)) & 0xFF
                self._cur = bytearray([nh0, pl[1]])
                self._cur += pl[3:]
            elif self._cur is not None:
                self._cur += pl[3:]
            if end_bit and self._cur is not None:
                self._emit(out, self._cur)
                self._cur = None
        elif nal_type == 48:  # AP — bir paketda bir nechta NAL
            i = 2
            while i + 2 <= len(pl):
                sz = int.from_bytes(pl[i:i + 2], "big")
                i += 2
                self._emit(out, pl[i:i + sz])
                i += sz
        else:  # yagona NAL (VPS/SPS/PPS/slice)
            self._emit(out, pl)
        return bytes(out)


class H264RtpDecryptor(_NalDecryptBase):
    """RTP/H.264 paketlarini Annex-B ga aylantirib, NAL body ni dekodlaydi.
    H.264 NAL header 1 bayt; FU-A=28, STAP-A=24, yagona NAL=1..23."""

    def __init__(self, key: str, *, cache_key: str | None = None):
        super().__init__(key, cache_key=cache_key)
        self._cur = None

    def _emit(self, out: bytearray, nal):  # override: 1 baytli NAL header, SPS=tur 7
        if len(nal) < 1:
            return
        self._classify(nal, 1, lambda b: (b & 0x1F) == 7, lambda x: x in self._PROFILES)
        self._emit_nal(out, nal)

    def feed(self, rtp_packet: bytes) -> bytes:
        if (rtp_packet[1] & 0x7F) != HEVC_VIDEO_PT:
            return b""
        pl = rtp_payload(rtp_packet)
        if len(pl) < 1:
            return b""
        out = bytearray()
        t = pl[0] & 0x1F
        if t == 28:  # FU-A — bo'lingan NAL
            if len(pl) < 2:
                return b""
            fu = pl[1]
            start_bit = fu >> 7
            end_bit = (fu >> 6) & 1
            fu_type = fu & 0x1F
            if start_bit:
                self._cur = bytearray([(pl[0] & 0xE0) | fu_type])
                self._cur += pl[2:]
            elif self._cur is not None:
                self._cur += pl[2:]
            if end_bit and self._cur is not None:
                self._emit(out, self._cur)
                self._cur = None
        elif t == 24:  # STAP-A — bir paketda bir nechta NAL
            i = 1
            while i + 2 <= len(pl):
                sz = int.from_bytes(pl[i:i + 2], "big")
                i += 2
                self._emit(out, pl[i:i + sz])
                i += sz
        else:  # yagona NAL (SPS/PPS/slice)
            self._emit(out, pl)
        return bytes(out)


def detect_rtp_codec(payload0: int):
    """RTP video payload birinchi baytidan codec: 'h264' | 'hevc' | None.
    Faqat ANIQ markerlar (FU yoki param-set) bo'yicha; noaniq (oddiy slice)
    paketda None qaytaramiz -> chaqiruvchi keyingi paketni tekshiradi."""
    h264_t = payload0 & 0x1F
    hevc_t = (payload0 >> 1) & 0x3F
    # Aniq H.264: FU-A=28, STAP-A=24, SPS=0x67, PPS=0x68, IDR=0x65
    if h264_t in (28, 24) or payload0 in (0x67, 0x68, 0x65):
        return "h264"
    # Aniq HEVC: FU=49, AP=48, VPS=0x40, SPS=0x42, PPS=0x44
    # (HEVC IDR 0x26/0x28 ishlatilmaydi — H.264 SEI/PPS bilan to'qnashadi;
    #  HEVC IDR baribir katta -> FU=49 orqali aniqlanadi)
    if hevc_t in (49, 48) or payload0 in (0x40, 0x42, 0x44):
        return "hevc"
    return None  # noaniq


