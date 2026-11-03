"""RTP depacketising (H.264 / HEVC, FU/AP) with sequence tracking."""

from __future__ import annotations



from cloudcam.codec.nal import (
    _NalDecryptBase, HEVC_VIDEO_PT,
)


class RtpError(ValueError):
    """Buzuq yoki qo'llanmaydigan RTP paket."""


def rtp_payload(data: bytes) -> bytes:
    """RTP paketining foydali yuki (fixed, CSRC, extension, padding olib tashlanadi)."""
    if len(data) < 12:
        raise RtpError("RTP packet is too short")
    if data[0] >> 6 != 2:
        raise RtpError("Unsupported RTP version")
    has_padding = bool(data[0] & 0x20)
    has_extension = bool(data[0] & 0x10)
    offset = 12 + (data[0] & 0x0F) * 4
    if len(data) < offset:
        raise RtpError("RTP CSRC header exceeds packet length")
    if has_extension:
        if len(data) < offset + 4:
            raise RtpError("RTP extension header exceeds packet length")
        offset += 4 + int.from_bytes(data[offset + 2:offset + 4], "big") * 4
        if len(data) < offset:
            raise RtpError("RTP extension payload exceeds packet length")
    payload = data[offset:]
    if has_padding:
        if not payload:
            raise RtpError("RTP padding set without payload")
        padding_len = payload[-1]
        if padding_len == 0 or padding_len > len(payload):
            raise RtpError("Invalid RTP padding length")
        payload = payload[:-padding_len]
    return payload


class _RtpSeqMixin:
    """RTP ketma-ketlik raqami nazorati.

    FU bo'lagi yo'qolsa, qolgan bo'laklar yopishtirilib NAL davom etardi. Shifrli
    body'da bu AES 16-bayt bloklarini SURADI — yo'qolgan joydan keyingi butun NAL
    axlatga aylanadi va dekoderga buzuq slice ketadi. Uzilish aniqlansa yig'ilayotgan
    NAL tashlanadi (dekoder bitta slice'siz yaxshiroq tiklanadi).

    Seq (SSRC, PT) JUFTI bo'yicha kuzatiladi. Real oqimda (Hik-Connect, o'lchandi)
    bitta SSRC ichida video (PT 96), metadata (PT 112) va audio (PT 0) keladi va
    HAR BIRINING O'Z seq hisoblagichi bor. Faqat SSRC bo'yicha sanash har
    PT almashinuvida soxta "uzilish" berardi (2318 paketda 706 ta)."""

    def _init_seq(self) -> None:
        self._last_seq = {}    # {(ssrc, pt): oxirgi seq}
        self.seq_gaps = 0      # diagnostika
        self.dup_packets = 0

    def _check_seq(self, pkt: bytes) -> bool:
        """False — paket takroriy (tashlanadi). Uzilishda joriy FU bekor qilinadi."""
        if len(pkt) < 12:
            return True
        seq = (pkt[2] << 8) | pkt[3]
        stream = (bytes(pkt[8:12]), pkt[1] & 0x7F)      # (SSRC, PT)
        last = self._last_seq.get(stream)
        self._last_seq[stream] = seq
        if last is None:
            return True
        if seq == last:
            self.dup_packets += 1
            return False
        # Faqat VIDEO oqimidagi uzilish yig'ilayotgan NAL'ga tegadi (audio
        # paketi yo'qolsa video FU'ni tashlash — keraksiz buzilish).
        if seq != (last + 1) & 0xFFFF and stream[1] == HEVC_VIDEO_PT:
            self.seq_gaps += 1
            self._cur = None   # yarim NAL — bo'lagi yo'q, tashlaymiz
        return True


class HevcRtpDecryptor(_RtpSeqMixin, _NalDecryptBase):
    """RTP/HEVC paketlarini Annex-B ga aylantirib, NAL body ni dekodlaydi."""

    def __init__(self, key: str, *, cache_key: str | None = None):
        super().__init__(key, cache_key=cache_key)
        self._cur = None       # joriy FU (fragment) yig'indisi
        self._init_seq()

    def feed(self, rtp_packet: bytes) -> bytes:
        """Bitta RTP paketdan Annex-B bayt qaytaradi (bo'sh bo'lishi mumkin)."""
        if not self._check_seq(rtp_packet):
            return b""
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


class H264RtpDecryptor(_RtpSeqMixin, _NalDecryptBase):
    """RTP/H.264 paketlarini Annex-B ga aylantirib, NAL body ni dekodlaydi.
    H.264 NAL header 1 bayt; FU-A=28, STAP-A=24, yagona NAL=1..23."""

    def __init__(self, key: str, *, cache_key: str | None = None):
        super().__init__(key, cache_key=cache_key)
        self._cur = None
        self._init_seq()

    def _emit(self, out: bytearray, nal):  # override: 1 baytli NAL header, SPS=tur 7
        if len(nal) < 1:
            return
        self._classify(nal, 1, lambda b: (b & 0x1F) == 7, lambda x: x in self._PROFILES)
        self._emit_nal(out, nal)

    def feed(self, rtp_packet: bytes) -> bytes:
        if not self._check_seq(rtp_packet):
            return b""
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
