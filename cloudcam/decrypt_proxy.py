"""
Shifrlangan EZVIZ HEVC kameralar uchun dekodlovchi proxy.

Bu kameralar videoni RTP/HEVC (H.265) tarzida uzatadi va NAL body ni
AES-ECB bilan shifrlaydi. Kalit = kamera "Tasdiqlash Kodu" (verification code),
16 baytgacha nol bilan to'ldirilgan. Har NAL body ning birinchi 4096 baytigacha
(to'liq 16-baytli bloklar) shifrlangan.

pyezvizapi ning standart "stream proxy" buni ocha olmaydi (u MPEG-PS kutadi).
Bu yerda biz o'zimiz:
  1) VTM dan RTP paketlarni o'qiymiz
  2) RTP/HEVC ni depaketlaymiz (VPS/SPS/PPS + FU/AP) -> Annex-B
  3) Har NAL body ni verification code bilan dekodlaymiz
  4) ichki ffmpeg orqali MPEG-TS qilib HTTP da uzatamiz (stream_manager o'qiydi)

Ishga tushirish (odatda app.py/stream_manager chaqiradi):
    python decrypt_proxy.py <SERIAL> <PORT> <CODE> [CHANNEL]
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import threading
from itertools import chain
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from Crypto.Cipher import AES

import requests as _rq
import pyezvizapi.cloud_stream as _cs
from pyezvizapi.client import EzvizClient
from pyezvizapi.cloud_stream import open_cloud_stream
from pyezvizapi.stream import rtp_payload

from . import vtm_cache
from .settings import get_active

# Platformaga qarab klient turi (pagelist to'liq natija qaytarishi uchun muhim)
_CLIENT_TYPE = "55" if get_active().platform == "hikconnect" else "1"


def _paged_vtm_page_list(client):
    """VTM pagelist'ni TO'LIQ (barcha sahifalar) yig'adi.
    Kutubxona faqat 1-sahifani oladi va clientType si boshqacha -> ko'p NVR
    hisobida kameralar topilmaydi. Shuning uchun to'g'ri header bilan xom so'rov."""
    tok = getattr(client, "_token", {}) or {}
    api = tok.get("api_url")
    sess = _rq.Session()
    sess.headers.update({
        "clientType": _CLIENT_TYPE, "lang": "en-US",
        "featureCode": "1fc28fa018178a1cd1c091b13b2f9f02",
        "sessionId": str(tok.get("session_id")),
    })
    base = None
    merged_res, merged_vtm = [], {}
    offset = 0
    for _ in range(40):
        r = sess.get(f"https://{api}/v3/userdevices/v1/resources/pagelist",
                     params={"filter": "VTM", "groupId": -1, "limit": 50, "offset": offset},
                     timeout=25)
        pl = r.json()
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
    if base is not None:
        base["resourceInfos"] = merged_res
        base["VTM"] = merged_vtm
    return base


# kutubxonaning bir-sahifali funksiyasini to'liq sahifalovchi bilan almashtiramiz
_cs.get_vtm_page_list = _paged_vtm_page_list
# ...va uning USTIGA hisob bo'yicha fayl keshini qo'yamiz ([[vtm_cache]]):
# pagelist + VTDU token har ochilishda ~6s bulutda ketardi, kesh bilan ~0.2s.
_CACHE_KEY = vtm_cache.install()

HEVC_VIDEO_PT = 96                  # RTP payload type — video
NAL_ENCRYPTED_PREFIX = 4096         # har NAL ning birinchi shu qadar bayti shifrlangan
START = b"\x00\x00\x00\x01"

# --- Inter (P/B) slice shifrini AVTO-ANIQLASH ---
# "Selektiv shifr" (faqat IRAP + param-set shifrlanadi, P/B toza) — bu BA'ZI
# kameralarda to'g'ri, ba'zilarida NOTO'G'RI. Xato taxmin qilinsa HAR P-freym
# buziladi: tasvir I-freymda tiklanib, orasida to'kiladi — aynan "telefonda
# silliq, bu yerda buziladi" shikoyati. O'lchov (40s oqim, bitta kamera):
# taxmin bilan 246 ta HEVC dekoder xatosi, hammasi deshifrlanganda 5 ta.
_INTER_SAMPLES = 8              # qaror uchun shuncha inter NAL namunasi
_INTER_MARGIN = 0.25            # "deshifr" shuncha tuzilishliroq bo'lsagina tanlanadi
_INTER_MAX_PENDING = 4 << 20    # inter NAL kelmasa shuncha baytdan keyin taslim


def _is_missing_resource(exc) -> bool:
    """Qurilma bulut ro'yxatida YO'Q (oflayn kamera ro'yxatdan tushadi).

    Bu ESKIRGAN metama'lumot EMAS — pagelist muvaffaqiyatli olingan, qurilmaning
    o'zi yo'q. Shuning uchun bu holatda VTM keshini bekor qilish zarar: kesh
    hisob bo'yicha, ya'ni bitta oflayn kamera butun hisobning keshini o'chirardi
    (bizda 151 kameradan 31 tasi oflayn edi — bu doim ishlab turardi)."""
    return "could not find vtm resource" in str(exc or "").lower()


def keyerror_flag_path(serial: str, channel: int = 1) -> str:
    """Kod xato bo'lganda stream_manager bilan aloqa uchun bayroq fayl yo'li."""
    return os.path.join(tempfile.gettempdir(), f"ezviz_keyerr_{serial}_{channel}.flag")


def offline_flag_path(serial: str, channel: int = 1) -> str:
    """Kamera offline (VTM ma'lumot bermayapti) bo'lganda bayroq fayl yo'li."""
    return os.path.join(tempfile.gettempdir(), f"ezviz_offline_{serial}_{channel}.flag")


class _NalDecryptBase:
    """NAL body ni AES-ECB bilan dekodlash + param-set (SPS/VPS) orqali shifr turini avtomatik
    aniqlash. UCH shifr varianti qo'llanadi (clear/drop offsetlari bilan):
      A)       header TOZA, body shifrli/toza                 -> clear=hdr, drop=0
      B-hint)  toza tur-ko'rsatkichi + shifrlangan TO'LIQ NAL  -> clear=0,   drop=hdr
      B-whole) butun NAL shifrli (ko'rsatkichsiz, PS uchun)    -> clear=0,   drop=0
    """

    _PROFILES = {66, 77, 88, 100, 110, 122, 244, 44, 83, 86, 118, 128}  # H.264 profile_idc
    # SELEKTIV shifr: kameralar faqat I-freym/IRAP + param-set NAL larini shifrlaydi,
    # inter (P/B) slice larni toza qoldiradi. Variant A (header toza) da turi shu ro'yxatda
    # bo'lsa deshifrlaymiz; B-hint da ko'rsatkich orqali aniqlanadi (ro'yxat kerak emas).
    _H264_ENC_TYPES = {5, 7, 8}                              # IDR, SPS, PPS
    _HEVC_ENC_TYPES = {16, 17, 18, 19, 20, 21, 32, 33, 34}   # IRAP slices + VPS/SPS/PPS

    def __init__(self, key: str):
        self.key = key.encode().ljust(16, b"\0")[:16]
        self._decrypt = None   # None=noma'lum, True=shifrli, False=toza
        self.key_error = False
        self.codec = None      # 'h264' | 'hevc' | None
        self._clear = 0        # boshidagi toza (dekodlanmaydigan) baytlar
        self._drop = 0         # boshidan tashlanadigan (toza tur-ko'rsatkich) baytlar
        # Inter (P/B) slice ham shifrlanganmi: None=noma'lum, True/False=qaror.
        # Qaror chiqquncha chiqish NAVBATDA ushlanadi — aks holda birinchi
        # kadrlar noto'g'ri o'qilib ketadi va dekoder xato toshqini beradi.
        self._inter_enc = None
        self._inter_clear = []   # namuna: toza body'ning 1-bayti
        self._inter_dec = []     # namuna: AES-deshifrlangan body'ning 1-bayti
        self._pending = []       # None = qaror qabul qilingan (buferlash yo'q)
        self._pending_bytes = 0
        self.inter_note = None   # diagnostika uchun qisqa izoh

    def _aes16(self, data) -> bytes:
        return AES.new(self.key, AES.MODE_ECB).decrypt(bytes(data[:16]))

    def _decrypt_body(self, body) -> bytes:
        if not self._decrypt:  # None yoki False -> dekod qilmaymiz
            return bytes(body)
        n = (min(len(body), NAL_ENCRYPTED_PREFIX) // 16) * 16
        if n == 0:
            return bytes(body)
        head = AES.new(self.key, AES.MODE_ECB).decrypt(bytes(body[:n]))
        return head + bytes(body[n:])

    def _classify(self, nal, hdr, is_paramset, body_marker) -> None:
        """Param-set NAL orqali shifr variantini aniqlaydi (clear/drop/decrypt ni o'rnatadi).
        is_paramset(b0)->bool: tur (SPS=7 / VPS=32) to'g'rimi; body_marker(byte)->bool:
        deshifrlangan/toza body boshi to'g'rimi (H.264 profile_idc / HEVC 0x0C)."""
        if self._decrypt is not None:
            return
        b0 = nal[0]
        ps = is_paramset(b0)
        # A) header toza
        if ps and len(nal) > hdr and body_marker(nal[hdr]):
            self._clear, self._drop, self._decrypt = hdr, 0, False
            return                                              # toza oqim
        if ps and len(nal) >= hdr + 16 and body_marker(self._aes16(nal[hdr:hdr + 16])[0]):
            self._clear, self._drop, self._decrypt = hdr, 0, True
            return                                              # header toza, body shifrli
        # B-hint) toza tur-ko'rsatkich + shifrlangan to'liq NAL (header ham shifr ichida)
        if len(nal) >= hdr + 16:
            dec = self._aes16(nal[hdr:hdr + 16])
            if is_paramset(dec[0]) and len(dec) > hdr and body_marker(dec[hdr]):
                self._clear, self._drop, self._decrypt = 0, hdr, True
                return
        # B-whole) butun NAL shifrli, ko'rsatkichsiz
        if len(nal) >= 16:
            dec = self._aes16(nal[:16])
            if is_paramset(dec[0]) and len(dec) > hdr and body_marker(dec[hdr]):
                self._clear, self._drop, self._decrypt = 0, 0, True
                return
        # toza header param-set ko'rinadi-yu hech variant mos kelmasa -> kod xato
        if ps and len(nal) >= hdr + 16:
            self.key_error = True

    # ---- chiqish: inter-qaror chiqquncha TARTIB bilan buferlanadi ----

    def _put(self, out, data: bytes) -> None:
        """Tayyor baytlarni chiqaradi (yoki qaror chiqmaguncha navbatga qo'yadi)."""
        if self._pending is None:
            out += data
            return
        self._pending.append((b"", data, 0))
        self._pending_bytes += len(data)
        self._guard_pending(out)

    def _inter_bytes(self, nal, hdr: int) -> bytes:
        """Inter slice — qarorga qarab deshifrlanadi yoki toza qoldiriladi."""
        if self._inter_enc:
            return START + bytes(nal[:hdr]) + self._decrypt_body(nal[hdr:])
        return START + bytes(nal)

    def _put_inter(self, out, nal, hdr: int) -> None:
        if self._pending is None:
            out += self._inter_bytes(nal, hdr)
            return
        self._pending.append((b"i", bytes(nal), hdr))
        self._pending_bytes += len(nal)
        self._sample_inter(nal, hdr)
        if self._inter_enc is not None:
            self._flush(out)        # qaror chiqdi — navbat TARTIB bilan chiqadi
        else:
            self._guard_pending(out)

    def _guard_pending(self, out) -> None:
        """Inter NAL umuman kelmasa (faqat I-freym) — kutib qotib qolmaymiz."""
        if self._pending is not None and self._pending_bytes > _INTER_MAX_PENDING:
            self._inter_enc = False
            self.inter_note = "namuna yetmadi -> TOZA deb qabul qilindi"
            self._flush(out)

    def _flush(self, out) -> None:
        pend, self._pending = self._pending, None
        self._pending_bytes = 0
        for kind, data, hdr in pend or ():
            out += data if kind == b"" else self._inter_bytes(data, hdr)

    def _sample_inter(self, nal, hdr: int) -> None:
        """TUZILISH testi: slice header baytlari TAKRORLANADI (bir xil PPS, bir xil
        tuzilish), shifrlangan baytlar esa TASODIFIY. Shuning uchun N namunadagi
        FARQLI qiymatlar sonini solishtiramiz — kichigi to'g'ri o'qish.
        Bitta bayt bo'yicha "yaroqlilik" testi ishlamaydi: tasodifiy bayt ham ~87%
        holatda yaroqli ue(v) beradi; farqli-qiymat nisbati esa keskin ajratadi
        (o'lchov: toza=1.00 vs deshifr=0.06)."""
        body = nal[hdr:]
        if len(body) < 16:
            return
        self._inter_clear.append(body[0])
        self._inter_dec.append(self._aes16(body)[0])
        if len(self._inter_clear) >= _INTER_SAMPLES:
            n = len(self._inter_clear)
            d_clear = len(set(self._inter_clear)) / n
            d_dec = len(set(self._inter_dec)) / n
            # Teng bo'lsa TOZA qoldiramiz (xavfsizroq: toza P-freymni deshifrlash
            # uni buzadi), faqat aniq farq bo'lsa deshifrga o'tamiz.
            self._inter_enc = d_dec + _INTER_MARGIN < d_clear
            self.inter_note = (f"inter={'SHIFRLI' if self._inter_enc else 'TOZA'} "
                               f"(toza={d_clear:.2f} deshifr={d_dec:.2f}, {n} namuna)")

    def _emit_nal(self, out, nal) -> None:
        if self._decrypt is None:
            return  # shifr aniqlanmaguncha chiqarmaymiz
        # Inter savoli FAQAT "A-body" variantida bor (header toza, body shifrli).
        # Toza oqim / B-hint / B-whole da savol yo'q -> buferlashni o'chiramiz.
        # Bu yerda navbat hali bo'sh (chiqish `_decrypt` aniqlangunga qadar yo'q).
        if self._pending is not None and not (self._decrypt and self._clear and not self._drop):
            self._pending = None
        if not self._decrypt:                       # toza oqim
            self._put(out, START + bytes(nal))
            return
        if self._drop:
            # B-hint + SELEKTIV shifr (har NAL alohida). I-freym/param-set NAL larida
            # toza tur-ko'rsatkich (dublikat header) bor: shifrli bo'lsa AES deshifr header'ga
            # mos keladi; juda qisqa (PPS) bo'lsa AESsiz dublikat header. P-freym ko'rsatkichsiz.
            hdr = self._drop
            if len(nal) >= hdr + 16 and self._aes16(nal[hdr:hdr + 16])[:hdr] == bytes(nal[:hdr]):
                self._put(out, START + self._decrypt_body(nal[hdr:]))  # shifrli -> to'liq NAL
            elif len(nal) >= 2 * hdr and bytes(nal[hdr:2 * hdr]) == bytes(nal[:hdr]):
                self._put(out, START + bytes(nal[hdr:]))        # toza, dublikat ko'rsatkich
            else:
                self._put(out, START + bytes(nal))              # oddiy toza NAL (P-freym)
            return
        # A (header toza, body shifrli) yoki B-whole (butun NAL shifrli)
        hdr = self._clear
        if hdr == 0:                                    # B-whole: butun NAL deshifr
            self._put(out, START + self._decrypt_body(nal))
            return
        if len(nal) <= hdr:
            self._put(out, START + bytes(nal))
            return
        # Variant A: IRAP + param-set HAR DOIM shifrli. Inter (P/B) slice esa
        # kameraga QARAB — shuning uchun taxmin qilmaymiz, oqimdan aniqlaymiz
        # ([[_sample_inter]]). Qaror chiqquncha chiqish navbatda ushlanadi.
        ntype = (nal[0] & 0x1F) if hdr == 1 else ((nal[0] >> 1) & 0x3F)
        enc = self._H264_ENC_TYPES if hdr == 1 else self._HEVC_ENC_TYPES
        if ntype in enc:
            self._put(out, START + bytes(nal[:hdr]) + self._decrypt_body(nal[hdr:]))
        else:
            self._put_inter(out, nal, hdr)              # inter (P/B) slice

    def _emit(self, out: bytearray, nal) -> None:  # HEVC RTP (2 baytli NAL header, VPS=tur 32)
        if len(nal) < 2:
            return
        self._classify(nal, 2, lambda b: ((b >> 1) & 0x3F) == 32, lambda x: x == 0x0C)
        self._emit_nal(out, nal)


class HevcRtpDecryptor(_NalDecryptBase):
    """RTP/HEVC paketlarini Annex-B ga aylantirib, NAL body ni dekodlaydi."""

    def __init__(self, key: str):
        super().__init__(key)
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

    def __init__(self, key: str):
        super().__init__(key)
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


class PsStreamDecryptor(_NalDecryptBase):
    """MPEG-PS oqimini demux qilib, ES dagi NAL body larni dekodlaydi.
    H.264 va HEVC ni avtomatik aniqlaydi. (Kutubxona PS dekodlovchisi cheksiz
    video PES'da ishlamaydi, shuning uchun o'zimiz.)"""

    _PROFILES = {66, 77, 88, 100, 110, 122, 244, 44, 83, 86, 118, 128}

    def __init__(self, key: str):
        super().__init__(key)
        self._buf = bytearray()   # demux qilinmagan PS qoldig'i
        self._es = bytearray()    # ajratilgan ES (NAL ga ajratilmagan qoldiq)
        self._enc_off = None      # shifr boshlanish offseti: 0=butun NAL, 1=H.264, 2=HEVC header'dan keyin
        self._pending = []        # aniqlanmaguncha NAL larni vaqtincha saqlaymiz

    def _emit(self, out: bytearray, nal) -> None:  # override: codec/variant-aware
        if not nal:
            return
        if self._decrypt is None:                 # codec/shifr hali aniqlanmagan
            self._pending.append(bytes(nal))
            res = self._decide(self._pending)
            if res == "keyerror":
                self.key_error = True
                self._pending = []
                return
            if res is None:
                return                            # aniqlanmaguncha garbage chiqarmaymiz
            self.codec, self._enc_off, self._decrypt = res
            for n in self._pending:               # yig'ilgan NAL larni chiqaramiz
                self._write_nal(out, n)
            self._pending = []
            return
        self._write_nal(out, nal)

    def _write_nal(self, out: bytearray, nal) -> None:
        off = self._enc_off
        if len(nal) <= off:
            out += START + bytes(nal)
            return
        # off=0 -> butun NAL shifrli (header ham); aks holda header toza, body shifrli
        out += START + bytes(nal[:off]) + self._decrypt_body(nal[off:])

    def _aes16(self, data) -> bytes:
        return AES.new(self.key, AES.MODE_ECB).decrypt(bytes(data[:16]))

    @staticmethod
    def _h264_valid(b: int) -> bool:
        return (b & 0x80) == 0 and (b & 0x1F) in (1, 5, 6, 7, 8, 9)

    @staticmethod
    def _hevc_valid(b: int) -> bool:
        return (b & 0x80) == 0 and ((b >> 1) & 0x3F) in (0, 1, 2, 4, 19, 20, 21, 32, 33, 34, 39, 40)

    def _corrob(self, big, decoded) -> bool:
        """Talqinni tasdiqlash: namunadagi NAL header (yoki deshifrlangan header) larining
        ko'pi shu codec uchun haqiqiymi (yagona soxta moslikdan himoya)."""
        hits = sum(decoded)
        return hits >= max(2, len(big) // 2)

    def _decide(self, nals):
        """Yig'ilgan NAL lardan codec + shifr offseti + shifrli/toza ni aniqlaydi. 4 talqin:
        header toza (A, off=hdr) yoki butun NAL shifrli (B, off=0), H.264 yoki HEVC.
        SPS(H.264)/VPS(HEVC) ni topib body markeri bilan tasdiqlaydi (NRI'dan qat'i nazar).
        Qaytaradi: (codec, enc_off, decrypt) | 'keyerror' | None."""
        big = [n for n in nals if len(n) >= 18]
        if len(big) < 3:
            return None
        PROF = self._PROFILES
        decB = [self._aes16(n) for n in big]      # variant B: butun NAL deshifrlangan boshi
        for i, n in enumerate(big):
            # --- A) header TOZA, body shifrli/toza ---
            if (n[0] & 0x80) == 0 and (n[0] & 0x1F) == 7:           # H.264 SPS
                if n[1] in PROF and self._corrob(big, [self._h264_valid(m[0]) for m in big]):
                    return ("h264", 1, False)                       # toza oqim
                if self._aes16(n[1:17])[0] in PROF and self._corrob(big, [self._h264_valid(m[0]) for m in big]):
                    return ("h264", 1, True)
            if (n[0] & 0x80) == 0 and ((n[0] >> 1) & 0x3F) == 32:   # HEVC VPS
                if n[2] == 0x0C and self._corrob(big, [self._hevc_valid(m[0]) for m in big]):
                    return ("hevc", 2, False)                       # toza oqim
                if self._aes16(n[2:18])[0] == 0x0C and self._corrob(big, [self._hevc_valid(m[0]) for m in big]):
                    return ("hevc", 2, True)
            # --- B) BUTUN NAL shifrli (header ham) ---
            d = decB[i]
            if (d[0] & 0x1F) == 7 and d[1] in PROF and self._corrob(big, [self._h264_valid(x[0]) for x in decB]):
                return ("h264", 0, True)
            if ((d[0] >> 1) & 0x3F) == 32 and d[2] == 0x0C and self._corrob(big, [self._hevc_valid(x[0]) for x in decB]):
                return ("hevc", 0, True)
        if len(big) >= 24:        # yetarli NAL bor, lekin hech narsa mos kelmadi -> kod xato
            return "keyerror"
        return None

    def _demux(self):
        """PS dan video PES (0xE0-EF) payload'larini ajratib _es ga qo'shadi."""
        b = self._buf
        n = len(b)
        pos = 0
        while True:
            sc = b.find(b"\x00\x00\x01", pos)
            if sc < 0 or sc + 4 > n:
                break
            code = b[sc + 3]
            if code == 0xBA:  # pack header
                if sc + 14 > n:
                    break
                end = sc + 14 + (b[sc + 13] & 0x07)  # + stuffing
                if end > n:
                    break
                pos = end
            elif code == 0xB9:  # MPEG end
                pos = sc + 4
            elif 0xE0 <= code <= 0xEF:  # video PES
                if sc + 9 > n:
                    break
                length = (b[sc + 4] << 8) | b[sc + 5]
                payload_start = sc + 9 + b[sc + 8]  # 9 = 6 + 2 flags + 1 hdrlen
                if payload_start > n:
                    break
                if length > 0:
                    end = sc + 6 + length
                    if end > n:
                        break
                    self._es += b[payload_start:end]
                    pos = end
                else:  # cheksiz: keyingi start kodgacha
                    nxt = b.find(b"\x00\x00\x01", payload_start)
                    if nxt < 0:
                        break  # to'liq emas — keyingi chunk'ni kutamiz
                    self._es += b[payload_start:nxt]
                    pos = nxt
            else:  # boshqa stream (audio/system/PSM) — uzunlik bo'yicha o'tkazamiz
                if sc + 6 > n:
                    break
                length = (b[sc + 4] << 8) | b[sc + 5]
                end = sc + 6 + length
                if end > n:
                    break
                pos = end
        self._buf = b[pos:]

    def _process_es(self) -> bytes:
        """_es dagi to'liq NAL'larni dekodlab Annex-B qaytaradi."""
        e = self._es
        starts = []
        i = e.find(b"\x00\x00\x01", 0)
        while i >= 0:
            starts.append(i)
            i = e.find(b"\x00\x00\x01", i + 3)
        if len(starts) < 2:
            return b""
        out = bytearray()
        for k in range(len(starts) - 1):
            nal = e[starts[k] + 3:starts[k + 1]]  # header(2) + body
            # keyingi start kod 4-baytli bo'lsa, oxirgi 0x00 ni kesamiz
            if nal and nal[-1:] == b"\x00":
                nal = nal[:-1]
            self._emit(out, nal)
        self._es = e[starts[-1]:]  # oxirgi to'liqsiz NAL qoladi
        return bytes(out)

    def feed(self, ps_chunk: bytes) -> bytes:
        self._buf += ps_chunk
        self._demux()
        return self._process_es()


class PacedWriter:
    """Bulut burstini TEKISLAB yozadi (jitter bufer) + bulut soketini bloklamaydi.

    IKKI muammoni hal qiladi.

    1) QOTISH. Bulut kadrlarni tekis emas, GOP'ma-GOP yuboradi. O'lchandi (bitta
       kamera, 500 kadr): **491 kadr <10 ms oralig'ida keldi, qolgan 9 oraliq
       ~2000 ms** — ya'ni butun GOP bir zumda, keyin 2s jimlik. Kadrni kelishi
       bilan uzatsak, ko'ruvchi 2s qotgan tasvirni ko'radi, keyin 50 kadr bir
       zumda "tez surat" bo'lib o'tadi. Telefondagi ilova silliq ko'rinadi,
       chunki u buferlaydi. Shu sababli kadrlar SHU YERDA — hali SIQILGAN
       holatda (GOP ~0.2 MB, xom kadrlarda esa 50 x 2.7 MB bo'lardi) —
       buferlanadi va o'lchangan tezlikda chiqariladi.

    2) BACKPRESSURE. Avval chiqish soketiga bulut o'quvchi THREAD'ning O'ZI
       yozardi: ffmpeg/ko'ruvchi sekinlashsa `write()` bloklanadi, bulut soketi
       o'qilmay qoladi va bulut paket tashlaydi -> tasvir buziladi. Endi yozuv
       alohida thread'da; navbat to'lsa KALIT KADR chegarasida tashlanadi
       (o'rtadan kesilmaydi), bulut o'quvchisi esa hech qachon kutmaydi.
    """

    MAX_BYTES = 8 << 20        # navbat cheki (siqilgan bayt)
    TARGET_S = 1.0             # maqsad bufer chuqurligi
    RATE_WINDOW_S = 4.0        # kelish tezligi shu oynada o'lchanadi
    MIN_STEP, MAX_STEP = 1 / 60.0, 1 / 4.0

    def __init__(self, wfile, codec: str | None, paced: bool = True):
        self._w = wfile
        self._paced = paced
        self._hevc = codec != "h264"
        self._q = []               # [(bytes, kadrmi, kalitmi)]
        self._bytes = 0
        self._rx = []              # kadr kelish vaqtlari
        self._cur = bytearray()    # yig'ilayotgan kadr
        self._cur_key = False
        self._cur_has_vcl = False
        self._lock = threading.Lock()
        self._ev = threading.Event()
        self.failed = False
        self.dropped = 0
        self._stop = False
        self._th = threading.Thread(target=self._run, daemon=True)

    # ---- Annex-B bo'lish: NAL'lardan KADR chegarasini topamiz ----
    def _nal_kind(self, b0: int, b1: int) -> tuple[bool, bool]:
        """(vcl_mi, kalitmi) — NAL turiga qarab."""
        if self._hevc:
            t = (b0 >> 1) & 0x3F
            return t <= 31, 16 <= t <= 21
        t = b0 & 0x1F
        return 1 <= t <= 5, t == 5

    def start(self):
        self._th.start()
        return self

    def feed(self, data: bytes) -> None:
        """Deshifrlangan baytlar — kadrlarga bo'lib navbatga qo'yamiz."""
        if not self._paced:
            self._push(data, frame=True, key=False)
            return
        pos = 0
        while True:
            i = data.find(b"\x00\x00\x01", pos)
            if i < 0:
                self._cur += data[pos:]
                break
            hdr = i + 3
            if hdr < len(data):
                vcl, key = self._nal_kind(data[hdr], data[hdr + 1] if hdr + 1 < len(data) else 0)
                # Yangi VCL NAL — oldingi kadr tugadi (bir kadr = bir slice deb
                # hisoblaymiz; ko'p slice bo'lsa chuqurlik qaytarmasi tuzatadi).
                if vcl and self._cur_has_vcl:
                    start = i - 1 if i > 0 and data[i - 1] == 0 else i
                    self._cur += data[pos:start]
                    self._flush_frame()
                    pos = start
                    continue
                if key:
                    self._cur_key = True
                if vcl:
                    self._cur_has_vcl = True
            self._cur += data[pos:hdr]
            pos = hdr
        if len(self._cur) > (2 << 20):     # himoya: chegara topilmadi
            self._flush_frame()

    def _flush_frame(self) -> None:
        if not self._cur:
            return
        self._push(bytes(self._cur), frame=True, key=self._cur_key)
        self._cur = bytearray()
        self._cur_key = False
        self._cur_has_vcl = False

    def _push(self, data: bytes, *, frame: bool, key: bool) -> None:
        with self._lock:
            self._q.append((data, frame, key))
            self._bytes += len(data)
            if frame:
                self._rx.append(time.monotonic())
            # To'lib ketdi — KEYINGI KALIT KADRGACHA butun kadrlarni tashlaymiz
            # (o'rtadan kesish dekoderga buzuq GOP beradi; bu esa toza sakrash).
            if self._bytes > self.MAX_BYTES:
                while len(self._q) > 1:
                    d, _f, _k = self._q.pop(0)
                    self._bytes -= len(d)
                    self.dropped += 1
                    if self._q[0][2]:          # oldinda kalit kadr — to'xtaymiz
                        break
        self._ev.set()

    def _step(self) -> float:
        now = time.monotonic()
        while self._rx and now - self._rx[0] > self.RATE_WINDOW_S:
            self._rx.pop(0)
        # DIQQAT: kelish tezligini burst ichida o'lchab bo'lmaydi (50 kadr bir
        # zumda keladi). Shuning uchun o'lchov kamida bir necha GOP'ni qamragan
        # bo'lsagina ishlatiladi; undan oldin 25 fps deb boshlaymiz va farqni
        # bufer chuqurligi qaytarmasi tuzatadi.
        span = now - self._rx[0] if self._rx else 0.0
        if len(self._rx) > 20 and span > 2.0:
            nominal = span / len(self._rx)
        else:
            nominal = 0.04
        nominal = max(self.MIN_STEP, min(self.MAX_STEP, nominal))
        depth = len(self._q) * nominal
        if depth > self.TARGET_S * 2:
            return nominal * 0.85          # orqada qoldik — biroz tezroq
        if depth < self.TARGET_S * 0.5:
            return nominal * 1.15          # bufer sayoz — biroz sekinroq
        return nominal

    def _run(self) -> None:
        due = time.monotonic()
        while True:
            with self._lock:
                item = self._q.pop(0) if self._q else None
                if item:
                    self._bytes -= len(item[0])
            if item is None:
                if self._stop:
                    return                 # navbat BO'SHAGANDAN keyin chiqamiz
                self._ev.wait(0.05)
                self._ev.clear()
                due = time.monotonic()
                continue
            try:
                self._w.write(item[0])
            except Exception:
                self.failed = True
                return
            if self._paced and item[1]:
                due += self._step()
                delay = due - time.monotonic()
                if delay > 0:
                    time.sleep(min(delay, 1.0))
                elif delay < -1.0:
                    due = time.monotonic()   # juda orqada — soatni tiklaymiz

    def close(self, drain: float = 3.0) -> None:
        """Navbatni chiqarib bo'lgach to'xtaydi (yopilishda oxirgi kadrlar
        yo'qolmasin), lekin `drain` soniyadan ko'p kutmaydi."""
        self._flush_frame()
        self._stop = True
        self._ev.set()
        if self._th.is_alive():
            self._th.join(timeout=drain)


_orig_build_vtm_url = None


def set_substream(enable: bool) -> None:
    """Bulutdan KICHIK (substream) oqimni so'raydi — VTM URL'idagi `stream=1`
    o'rniga `stream=2`.

    Nega kerak: grid'da har plitka ekranda ~250-500 px, kamera esa 2560x1440
    yuboradi — bu ekran ko'rsatolmaydigan ~30 barobar ortiqcha piksel. O'lchov
    (bir xil devor, 23 plitka): asosiy oqimda tasvir to'kilib qotgan, substream
    bilan har plitka **640x360, ~0.11 Mbit/s** (asosiysi ~4 Mbit/s — 36 barobar
    kam) va hammasi toza dekodlangan. Zaif uplink'da paket yo'qolishi ham
    kamayadi (yashil chiziqlar/buzilish).

    Hamma kamerada substream bo'lavermaydi (masalan batareyali modellar) —
    bunday holda bulut asosiy oqimni beradi, ya'ni zarari yo'q.
    """
    global _orig_build_vtm_url
    import re as _re
    if _orig_build_vtm_url is None:
        _orig_build_vtm_url = _cs.build_vtm_url          # ASL nusxa (bir marta)
    if enable:
        def _sub(*a, **kw):
            return _re.sub(r"(?<=[?&])stream=1(?=&|$)", "stream=2",
                           _orig_build_vtm_url(*a, **kw))
        _cs.build_vtm_url = _sub
    else:
        _cs.build_vtm_url = _orig_build_vtm_url


def _make_client():
    import re
    with open(get_active().token_file, encoding="utf-8") as f:
        token = json.load(f)
    client = EzvizClient(token.get("username"), None, token.get("api_url"), token=token)
    # Klient turi — busiz pagelist hamma resurslarni qaytarmaydi (platformaga qarab)
    try:
        client._session.headers.update({"clientType": _CLIENT_TYPE, "lang": "en-US"})
    except Exception:
        pass
    # Hik-Connect authAddr ni "https://null" qaytaradi -> regiondan derive qilamiz
    # (apiiSGP.hik-connect.com -> sgpauth.ezvizlife.com). EZVIZ uchun ham to'g'ri.
    su = (token.get("service_urls") or {})
    if not su.get("authAddr") or "null" in str(su.get("authAddr")).lower():
        m = re.match(r"apii([a-z]+)\.", str(token.get("api_url", "")))
        if m:
            client._token.setdefault("service_urls", {})["authAddr"] = \
                f"https://{m.group(1)}auth.ezvizlife.com"
    return client


def _device_names(client):
    """{serial: qurilma_nomi} — ilovadagi nomlar (NVR/kamera nomi)."""
    tok = getattr(client, "_token", {}) or {}
    api = tok.get("api_url")
    sess = _rq.Session()
    sess.headers.update({
        "clientType": _CLIENT_TYPE, "lang": "en-US",
        "featureCode": "1fc28fa018178a1cd1c091b13b2f9f02",
        "sessionId": str(tok.get("session_id")),
    })
    names = {}
    offset = 0
    for _ in range(40):
        r = sess.get(f"https://{api}/v3/userdevices/v1/devices/pagelist",
                     params={"filter": "CONNECTION", "groupId": -1, "limit": 50, "offset": offset},
                     timeout=25)
        d = r.json()
        di = d.get("deviceInfos") or []
        for x in di:
            names[x.get("deviceSerial")] = (x.get("name") or "").strip()
        page = d.get("page") or {}
        if not di or not page.get("hasNext"):
            break
        offset += len(di)
    return names


def list_cameras(client=None):
    """Haqiqiy kameralar ro'yxati: [(serial, channel, name)].
    Nom = "Qurilma nomi — Kanal nomi" (ilovadagi nom bilan moslash uchun).
    VTM resurslaridan olinadi — bo'sh NVR kanallari (kamera ulanmagan) ko'rsatilmaydi.

    `client` — pyezvizapi `EzvizClient` (token bilan). Yuqori darajali API o'zining
    `CloudClient` obyektini uzatardi, unda esa `_token` yo'q: natijada so'rov
    `https://none/...` ga ketib `NameResolutionError` berardi. Shuning uchun mos
    kelmaydigan obyekt uzatilsa token fayldan o'zimiz quramiz."""
    if client is None or not getattr(client, "_token", None):
        client = _make_client()
    dev_names = _device_names(client)
    res = _cs.get_vtm_page_list(client).get("resourceInfos", []) or []
    cams = []
    for r in res:
        try:
            ch = int(r.get("localIndex"))
        except (TypeError, ValueError):
            continue
        if ch < 1:  # localIndex 0 = qurilma (NVR) o'zi, kamera emas
            continue
        serial = r.get("deviceSerial")
        chname = (r.get("resourceName") or "").strip()
        devname = dev_names.get(serial, "")
        if devname and chname and devname.lower() not in chname.lower():
            label = f"{devname} - {chname}"   # ASCII '-' (cv2 oynada '—' ko'rinmaydi)
        else:
            label = chname or devname or f"{serial} CH{ch}"
        cams.append((serial, ch, label))
    cams.sort(key=lambda x: (x[0], x[1]))
    return cams


def serve(serial: str, port: int, key: str, channel: int = 1, substream: bool = False):
    client = _make_client()
    set_substream(substream)

    def _flag_keyerror():
        try:
            with open(keyerror_flag_path(serial, channel), "w") as fl:
                fl.write("1")
        except Exception:
            pass

    def _flag_offline():
        try:
            with open(offline_flag_path(serial, channel), "w") as fl:
                fl.write("1")
        except Exception:
            pass

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            # Xom oqimni dekodlab to'g'ridan-to'g'ri uzatamiz (ichki ffmpeg yo'q):
            #   RTP/PS -> Annex-B (H.264/HEVC), MPEG-TS -> o'zicha.
            # Tashqi ffmpeg (stream_manager) codec'ni auto-detect qilib, bardoshli dekodlaydi.
            # Bulut resurslarida shu KANAL bormi? NVR online bo'lsa ham, unda
            # yo'q kanal hech qachon oqim bermaydi — bunda 20s kutib o'tirmaymiz
            # va "offline" emas, ANIQ sabab qaytaramiz ([[vtm_cache.channel_missing]]).
            if vtm_cache.channel_missing(_CACHE_KEY, serial, channel):
                _flag_offline()
                self.send_error(404, "channel not in cloud device list")
                return
            got_packets = False          # kesh FAQAT paket kelmasa bekor qilinadi
            try:
                stream_cm = open_cloud_stream(client, serial, channel=channel,
                                              client_type=9, refresh_vtm=False, timeout=20.0)
            except Exception as e:
                if "offline" in str(e).lower() or "unreachable" in str(e).lower():
                    _flag_offline()
                elif _is_missing_resource(e):
                    # Qurilma bulut ro'yxatidan tushgan (oflayn) — bu ESKIRGAN
                    # kesh EMAS, shuning uchun keshni o'chirmaymiz: kesh HISOB
                    # bo'yicha, ya'ni bitta oflayn kamera tufayli o'sha hisobdagi
                    # hamma kamera qayta metama'lumot yuklardi.
                    _flag_offline()
                else:
                    vtm_cache.invalidate(_CACHE_KEY)   # metama'lumot eskirgan bo'lishi mumkin
                self.send_error(502)
                return
            try:
                with stream_cm as stream:
                    stream.start()
                    pkts = stream.iter_packets()
                    # Format (RTP/PS/TS) va codec (H.264/HEVC) ni aniqlash uchun
                    # boshlang'ich paketlarni o'qiymiz (keyin ularni ham uzatamiz)
                    buffered = []
                    is_rtp = None
                    codec = None
                    for pkt in pkts:
                        b = bytes(pkt.body)
                        buffered.append(b)
                        if is_rtp is None:
                            is_rtp = len(b) >= 1 and (b[0] >> 6) == 2
                        if not is_rtp:
                            break  # MPEG-PS yoki TS
                        if (b[1] & 0x7F) == HEVC_VIDEO_PT:
                            pl = rtp_payload(b)
                            if len(pl) >= 1:
                                codec = detect_rtp_codec(pl[0])
                                if codec:
                                    break
                        # keyframe/aniq marker kelguncha skanerlaymiz (uzun GOP uchun)
                        if len(buffered) >= 400:
                            break
                    if not buffered:
                        self.send_error(502)
                        return

                    annexb = True                            # Annex-B chiqadimi (pacing uchun)
                    if not is_rtp and buffered[0][:1] == b"\x47":
                        transform = lambda b: b              # MPEG-TS (shifrsiz)
                        keyerr = lambda: False
                        annexb = False
                    elif not is_rtp:
                        dec = PsStreamDecryptor(key)          # MPEG-PS (H.264/HEVC avto)
                        transform = dec.feed; keyerr = lambda: dec.key_error
                    elif codec == "h264":
                        dec = H264RtpDecryptor(key)           # RTP/H.264
                        transform = dec.feed; keyerr = lambda: dec.key_error
                    else:
                        dec = HevcRtpDecryptor(key)           # RTP/HEVC
                        transform = dec.feed; keyerr = lambda: dec.key_error

                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()

                    # Yozuv ALOHIDA thread'da va O'LCHOVLI ([[PacedWriter]]) —
                    # bulut o'quvchisi hech qachon bloklanmaydi va 2 soniyalik
                    # burstlar tekis oqimga yoyiladi.
                    writer = PacedWriter(self.wfile, codec, paced=annexb).start()
                    try:
                        for body in chain(buffered, (bytes(p.body) for p in pkts)):
                            data = transform(body)
                            if keyerr():
                                _flag_keyerror()
                                break
                            if data:
                                got_packets = True
                                writer.feed(data)
                            if writer.failed:
                                break          # mijoz uzildi
                    finally:
                        writer.close()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            except Exception as e:
                if "offline" in str(e).lower() or "unreachable" in str(e).lower():
                    _flag_offline()
                elif not got_packets and not _is_missing_resource(e):
                    # Bitta ham paket kelmadi -> keshdagi metama'lumot (VTDU
                    # tokeni) eskirgan bo'lishi mumkin. Paket kelgan bo'lsa
                    # kesh TO'G'RI edi — uzilish boshqa sababdan, tegmaymiz.
                    vtm_cache.invalidate(_CACHE_KEY)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Foydalanish: python -m cloudcam.decrypt_proxy "
              "<SERIAL> <PORT> <CODE> [CHANNEL] [--substream]")
        sys.exit(1)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    serve(args[0], int(args[1]), args[2],
          int(args[3]) if len(args) > 3 else 1,
          substream="--substream" in sys.argv)
