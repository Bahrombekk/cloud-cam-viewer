"""MPEG-PS demux — Hik-Connect NVR'lari shu formatda uzatadi.

Kutubxonaning PS dekodlovchisi cheksiz (uzunligi 0) video PES'da ishlamaydi —
kadr o'rtasidan kesadi va dekoder "Could not find ref with POC" beradi.
Shuning uchun demux shu yerda.
"""
from __future__ import annotations

from Crypto.Cipher import AES

from .nal import START, _MAX_PS_BUF, _NalDecryptBase

class PsStreamDecryptor(_NalDecryptBase):
    """MPEG-PS oqimini demux qilib, ES dagi NAL body larni dekodlaydi.
    H.264 va HEVC ni avtomatik aniqlaydi. (Kutubxona PS dekodlovchisi cheksiz
    video PES'da ishlamaydi, shuning uchun o'zimiz.)"""

    _PROFILES = {66, 77, 88, 100, 110, 122, 244, 44, 83, 86, 118, 128}

    def __init__(self, key: str, *, cache_key: str | None = None):
        super().__init__(key, cache_key=cache_key)
        self._buf = bytearray()   # demux qilinmagan PS qoldig'i
        self._es = bytearray()    # ajratilgan ES (NAL ga ajratilmagan qoldiq)
        self._enc_off = None      # shifr boshlanish offseti: 0=butun NAL, 1=H.264, 2=HEVC header'dan keyin
        self._pending = []        # aniqlanmaguncha NAL larni vaqtincha saqlaymiz
        self.buf_resyncs = 0      # bufer chegarasidan oshib resync qilingan marta

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

    def _cap(self) -> None:
        """Buferlar chegaradan oshsa — oxirgi start-kodga RESYNC qilamiz.

        Normal oqimda buferlar har `feed` da bo'shaydi. Lekin start-kodsiz yoki
        buzuq kirishda `_demux` hech nimani ajrata olmaydi va `_buf`/`_es`
        cheksiz o'sardi — uzoq ishlaydigan jarayonda bu xotira oqishi."""
        for name in ("_buf", "_es"):
            b = getattr(self, name)
            if len(b) > _MAX_PS_BUF:
                i = b.rfind(b"\x00\x00\x01")
                setattr(self, name, b[i:] if i > 0 else bytearray())
                self.buf_resyncs += 1

    def feed(self, ps_chunk: bytes) -> bytes:
        self._buf += ps_chunk
        self._demux()
        out = self._process_es()
        self._cap()
        return out


