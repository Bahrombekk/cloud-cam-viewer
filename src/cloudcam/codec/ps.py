"""MPEG-PS demux + decryption (H.264 / HEVC auto-detected)."""

from __future__ import annotations


from Crypto.Cipher import AES

from cloudcam.codec.nal import _NalDecryptBase



# MPEG-PS demux buferlarining yuqori chegarasi. Start-kodsiz/buzuq kirishda
# bufer cheksiz o'sardi (jarayon xotirasi oqib ketardi) — chegaradan oshsa
# oxirgi start-kodga resync qilamiz.
_MAX_PS_BUF = 4 << 20
# Codec aniqlanmaguncha saqlanadigan NAL'lar soni. Oqim GOP o'rtasidan
# boshlansa keyingi VPS/SPS gacha yuzlab P-slice kelishi mumkin — ular IRAP'siz
# baribir dekodlanmaydi, shuning uchun eng eskilari tashlanadi.
_MAX_UNDECIDED = 300


def _next_ps_start(b, start: int) -> int:
    """PS darajasidagi keyingi start kod (pack/end/PES), NAL start kodlari emas.

    Cheksiz (length=0) video PES ichida NAL'lar ham `00 00 01` bilan
    boshlanadi — oddiy `find` birinchi NAL'da to'xtab, uning header baytini
    stream id deb o'qirdi."""
    i = b.find(b"\x00\x00\x01", start)
    while i >= 0:
        if i + 3 >= len(b):
            return -1
        if b[i + 3] >= 0xB9:
            return i
        i = b.find(b"\x00\x00\x01", i + 3)
    return -1



class PsStreamDecryptor(_NalDecryptBase):
    """MPEG-PS oqimini demux qilib, ES dagi NAL body larni dekodlaydi.
    H.264 va HEVC ni avtomatik aniqlaydi. (Kutubxona PS dekodlovchisi cheksiz
    video PES'da ishlamaydi, shuning uchun o'zimiz.)"""

    _PROFILES = {66, 77, 88, 100, 110, 122, 244, 44, 83, 86, 118, 128}

    def __init__(self, key: str, *, cache_key: str | None = None):
        super().__init__(key, cache_key=cache_key)
        self._buf = bytearray()   # demux qilinmagan PS qoldig'i
        self._es = bytearray()    # ajratilgan ES (NAL ga ajratilmagan qoldiq)
        # shifr boshlanish offseti: 0=butun NAL, 1=H.264, 2=HEVC header'dan keyin
        self._enc_off = None
        self._undecided = []      # codec/shifr aniqlanmaguncha NAL larni vaqtincha saqlaymiz
        # (`_pending` nomi BAND — u ota klassning inter-qaror navbati)
        self.buf_resyncs = 0      # bufer chegarasidan oshib resync qilingan marta

    def _emit(self, out: bytearray, nal) -> None:  # override: codec/variant-aware
        if not nal:
            return
        if self._decrypt is None:                 # codec/shifr hali aniqlanmagan
            self._undecided.append(bytes(nal))
            if len(self._undecided) > _MAX_UNDECIDED:
                del self._undecided[:-_MAX_UNDECIDED]
            res = self._decide(self._undecided)
            if res == "keyerror":
                self.key_error = True
                self._undecided = []
                return
            if res is None:
                return                            # aniqlanmaguncha garbage chiqarmaymiz
            self.codec, self._enc_off, self._decrypt = res
            # Ota klass atamalarida: off>0 = variant A (header toza), off=0 = B-whole.
            self._clear, self._drop = self._enc_off, 0
            for n in self._undecided:             # yig'ilgan NAL larni chiqaramiz
                self._emit_nal(out, n)
            self._undecided = []
            return
        self._emit_nal(out, nal)

    # Chiqish ota klassning `_emit_nal` i orqali — RTP bilan BIR XIL mantiq:
    # variant A da IRAP + param-set doim deshifrlanadi, inter (P/B) slice esa
    # oqimdan aniqlanadi ([[_sample_inter]]). Avval PS yo'li HAMMA NAL ni
    # deshifrlardi — selektiv shifrli kamerada har P-freym buzilardi.

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

        def ok264(src):
            return self._corrob(big, [self._h264_valid(m[0]) for m in src])

        def ok265(src):
            return self._corrob(big, [self._hevc_valid(m[0]) for m in src])

        for i, n in enumerate(big):
            # --- A) header TOZA, body shifrli/toza ---
            if (n[0] & 0x80) == 0 and (n[0] & 0x1F) == 7:           # H.264 SPS
                if n[1] in PROF and ok264(big):
                    return ("h264", 1, False)                       # toza oqim
                if self._aes16(n[1:17])[0] in PROF and ok264(big):
                    return ("h264", 1, True)
            if (n[0] & 0x80) == 0 and ((n[0] >> 1) & 0x3F) == 32:   # HEVC VPS
                if n[2] == 0x0C and ok265(big):
                    return ("hevc", 2, False)                       # toza oqim
                if self._aes16(n[2:18])[0] == 0x0C and ok265(big):
                    return ("hevc", 2, True)
            # --- B) BUTUN NAL shifrli (header ham) ---
            d = decB[i]
            if (d[0] & 0x1F) == 7 and d[1] in PROF and ok264(decB):
                return ("h264", 0, True)
            if ((d[0] >> 1) & 0x3F) == 32 and d[2] == 0x0C and ok265(decB):
                return ("hevc", 0, True)
        if len(big) >= 24:        # yetarli NAL bor, lekin hech narsa mos kelmadi
            # Oqim GOP o'rtasidan boshlangan bo'lsa (bulut shunday qiladi) hali
            # VPS/SPS kelmagan — headerlar toza va haqiqiy bo'lsa KUTAMIZ.
            # "Kod xato" faqat param-set ko'rilib, kalit uni ocholmaganda yoki
            # headerlarning o'zi ma'nosiz bo'lganda (butun NAL shifrli).
            saw_param_set = any(
                (n[0] & 0x80) == 0 and ((n[0] & 0x1F) == 7 or ((n[0] >> 1) & 0x3F) == 32)
                for n in big)
            if not saw_param_set and (ok264(big) or ok265(big)):
                return None
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
            elif code < 0xB9:  # PS emas (NAL start kodi / resync qoldig'i) — o'tamiz
                pos = sc + 3
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
                else:  # cheksiz: keyingi PS start kodgacha (NAL start kodlari emas)
                    nxt = _next_ps_start(b, payload_start)
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
