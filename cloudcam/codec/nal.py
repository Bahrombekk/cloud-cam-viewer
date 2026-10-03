"""NAL darajasidagi deshifrlash — SOF mantiq (tarmoq ham, bulut ham yo'q).

Kamera NAL body'sini AES-ECB bilan shifrlaydi (kalit = tasdiqlash kodi, 16
baytgacha nol bilan to'ldirilgan, har body'ning birinchi 4096 bayti). Qaysi
VARIANT ishlatilganini oqimning o'zidan aniqlaymiz — qurilma buni aytmaydi.

Bu modul `settings` dan boshqa hech narsaga bog'lanmaydi: shu sababli uni
bulutsiz, soketlarsiz, sintetik oqim bilan to'liq test qilish mumkin
(`tests/test_inter_encryption.py`, `test_decrypt_hardening.py`).
"""
from __future__ import annotations

from Crypto.Cipher import AES

from .enc_cache import load as _enc_cache_load
from .enc_cache import store as _enc_cache_store

HEVC_VIDEO_PT = 96                  # RTP payload type — video
NAL_ENCRYPTED_PREFIX = 4096         # har NAL ning birinchi shu qadar bayti shifrlangan
START = bytes((0, 0, 0, 1))        # Annex-B uzun start-kod  (escape'siz: fayl NUL bayt tutmasin)

# --- Inter (P/B) slice shifrini AVTO-ANIQLASH ---
# "Selektiv shifr" (faqat IRAP + param-set shifrlanadi, P/B toza) — bu BA'ZI
# kameralarda to'g'ri, ba'zilarida NOTO'G'RI. Xato taxmin qilinsa HAR P-freym
# buziladi: tasvir I-freymda tiklanib, orasida to'kiladi — aynan "telefonda
# silliq, bu yerda buziladi" shikoyati. O'lchov (40s oqim, bitta kamera):
# taxmin bilan 246 ta HEVC dekoder xatosi, hammasi deshifrlanganda 5 ta.
_INTER_SAMPLES = 8              # qaror uchun shuncha inter NAL namunasi
_INTER_MARGIN = 0.25            # "deshifr" shuncha tuzilishliroq bo'lsagina tanlanadi
_INTER_MAX_PENDING = 4 << 20    # inter NAL kelmasa shuncha baytdan keyin taslim

# --- IRAP darvozasi ---
# Bulut oqimi GOP O'RTASIDAN boshlanadi va Hik NVR param-setlarni DAVRIY
# yuboradi, ya'ni birinchi VPS/SPS/PPS dan keyin darhol IDR kelmaydi. O'sha
# joydan uzatsak dekoder ref kadrlarni topmaydi va tomoshabin boshida KUL
# RANG/buzilgan tasvir ko'radi. Shuning uchun birinchi IRAP (IDR/CRA) kelguncha
# slice larni CHIQARMAYMIZ; param-setlar esa o'tadi (dekoderga kerak).
# Shuncha slice dan keyin darvoza MAJBURAN ochiladi — IRAP belgilamaydigan
# kamerada tasvir umuman kelmay qolmasin.
_IRAP_MAX_WAIT = 400

# MPEG-PS demux buferlarining yuqori chegarasi. Start-kodsiz/buzuq kirishda
# bufer cheksiz o'sardi (jarayon xotirasi oqib ketardi) — chegaradan oshsa
# oxirgi start-kodga resync qilamiz.
_MAX_PS_BUF = 4 << 20



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
    # IRAP = tasodifiy kirish nuqtasi (dekoder shu yerdan TOZA boshlashi mumkin).
    _H264_IRAP = {5}                                         # IDR
    _HEVC_IRAP = {16, 17, 18, 19, 20, 21}                    # BLA/IDR/CRA
    # Param-set NAL lari IRAP dan OLDIN ham chiqariladi (dekoderga SPS/PPS kerak).
    _H264_PARAMSET = {7, 8}
    _HEVC_PARAMSET = {32, 33, 34}

    def __init__(self, key: str, *, cache_key: str | None = None):
        self.key = key.encode().ljust(16, b"\0")[:16]
        self._decrypt = None   # None=noma'lum, True=shifrli, False=toza
        self.key_error = False
        self.codec = None      # 'h264' | 'hevc' | None
        self._clear = 0        # boshidagi toza (dekodlanmaydigan) baytlar
        self._drop = 0         # boshidan tashlanadigan (toza tur-ko'rsatkich) baytlar
        # Inter (P/B) slice ham shifrlanganmi: None=noma'lum, True/False=qaror.
        # Qaror chiqquncha chiqish NAVBATDA ushlanadi — aks holda birinchi
        # kadrlar noto'g'ri o'qilib ketadi va dekoder xato toshqini beradi.
        # Kesh bo'lsa ([[_enc_cache_load]]) qaror tayyor keladi va buferlash
        # UMUMAN qilinmaydi — birinchi kadr darhol ketadi.
        self._cache_key = cache_key
        cached = _enc_cache_load(cache_key) if cache_key else None
        self._inter_enc = cached
        self._verifying = cached is not None   # keshdagi qaror tekshirilmoqda
        self._inter_clear = []   # namuna: toza body'ning 1-bayti
        self._inter_dec = []     # namuna: AES-deshifrlangan body'ning 1-bayti
        self._pending = None if self._verifying else []
        self._pending_bytes = 0
        self.inter_note = None   # diagnostika uchun qisqa izoh
        # IRAP darvozasi ([[_irap_gate]])
        self._seen_irap = False
        self._gated = 0          # tashlangan (IRAP dan oldingi) slice soni
        self.gate_note = None    # diagnostika uchun qisqa izoh

    @property
    def encrypted(self):
        """Oqim shifrlanganmi: None=hali aniqlanmagan, True/False=qaror.

        Ochiq nom — tashqi kod (`check_code.py`) ilgari `_decrypt` ni
        to'g'ridan-to'g'ri o'qirdi."""
        return self._decrypt

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

    def _irap_gate(self, ntype: int, hdr: int) -> bool:
        """Shu NAL chiqarilsinmi? (birinchi IRAP gacha slice lar tashlanadi)

        Param-set NAL lari doim o'tadi — dekoderga SPS/PPS/VPS kerak. IRAP
        kelgach darvoza butunlay ochiladi va boshqa hech narsa tashlanmaydi."""
        if self._seen_irap:
            return True
        irap = self._H264_IRAP if hdr == 1 else self._HEVC_IRAP
        pset = self._H264_PARAMSET if hdr == 1 else self._HEVC_PARAMSET
        if ntype in irap:
            self._seen_irap = True
            if self._gated:
                self.gate_note = f"IRAP gacha {self._gated} slice tashlandi"
            return True
        if ntype in pset:
            return True
        self._gated += 1
        if self._gated > _IRAP_MAX_WAIT:
            # Kamera GOP'i juda uzun yoki IRAP belgilanmagan — abadiy kutmaymiz.
            self._seen_irap = True
            self.gate_note = (f"IRAP kelmadi ({self._gated} slice) — darvoza "
                              f"majburan ochildi")
            return True
        return False

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
        """Inter slice — namunasi YUQORIDA olingan ([[_emit_nal]])."""
        if self._pending is None:       # qaror bor (yoki keshdan keldi)
            out += self._inter_bytes(nal, hdr)
            return
        self._pending.append((b"i", bytes(nal), hdr))
        self._pending_bytes += len(nal)
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
            self._decide_inter()

    def _decide_inter(self) -> None:
        """Namunalardan qaror chiqaradi (va keshga yozadi)."""
        n = len(self._inter_clear)
        d_clear = len(set(self._inter_clear)) / n
        d_dec = len(set(self._inter_dec)) / n
        # Teng bo'lsa TOZA qoldiramiz (xavfsizroq: toza P-freymni deshifrlash
        # uni buzadi), faqat aniq farq bo'lsa deshifrga o'tamiz.
        fresh = d_dec + _INTER_MARGIN < d_clear
        if self._verifying:
            # Kesh ishlatildi (buferlash o'tkazib yuborilgan). Endi tekshiramiz:
            # to'g'ri bo'lsa jimgina davom etamiz, xato bo'lsa TUZATAMIZ — bunda
            # boshidagi bir necha P-freym buzuq ketgan bo'ladi (~0.5s), keyin
            # toza. Kelishuv: har ochilishda ~0.7s tejash o'rniga kameraning
            # xatti-harakati O'ZGARGAN kamdan-kam holatda qisqa g'alizlik.
            self._verifying = False
            if fresh != self._inter_enc:
                self._inter_enc = fresh
                self.inter_note = (f"kesh XATO edi -> tuzatildi: "
                                   f"inter={'SHIFRLI' if fresh else 'TOZA'} "
                                   f"(toza={d_clear:.2f} deshifr={d_dec:.2f})")
                if self._cache_key:
                    _enc_cache_store(self._cache_key, fresh)
            return
        self._inter_enc = fresh
        self.inter_note = (f"inter={'SHIFRLI' if self._inter_enc else 'TOZA'} "
                           f"(toza={d_clear:.2f} deshifr={d_dec:.2f}, {n} namuna)")
        if self._cache_key:
            _enc_cache_store(self._cache_key, self._inter_enc)

    def _emit_nal(self, out, nal) -> None:
        if self._decrypt is None:
            return  # shifr aniqlanmaguncha chiqarmaymiz
        # Inter savoli FAQAT "A-body" variantida bor (header toza, body shifrli).
        # Toza oqim / B-hint / B-whole da savol yo'q -> buferlashni o'chiramiz.
        # Bu yerda navbat hali bo'sh (chiqish `_decrypt` aniqlangunga qadar yo'q).
        if self._pending is not None and not (self._decrypt and self._clear and not self._drop):
            self._pending = None
        if not self._decrypt:                       # toza oqim
            # Darvoza toza oqimda ham kerak — oqim baribir GOP o'rtasidan
            # boshlanadi ([[_irap_gate]]).
            h = self._clear or 2
            nt = (nal[0] & 0x1F) if h == 1 else ((nal[0] >> 1) & 0x3F)
            if not self._irap_gate(nt, h):
                return
            self._put(out, START + bytes(nal))
            return
        if self._drop:
            # DIQQAT: B-hint va B-whole da NAL header SHIFRLANGAN, ya'ni turini
            # oldindan o'qib bo'lmaydi -> IRAP darvozasi bu yerda QO'LLANMAYDI.
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
        # NAMUNA darvozadan QAT'IY NAZAR olinadi — shunda qaror IRAP kutish
        # paytida tayyor bo'ladi va darvoza ochilgach qo'shimcha kechikish yo'q.
        if ntype not in enc and (self._inter_enc is None or self._verifying):
            self._sample_inter(nal, hdr)
        if self._inter_enc is not None and self._pending is not None:
            self._flush(out)          # qaror yetib keldi — navbatni bo'shatamiz
        if not self._irap_gate(ntype, hdr):
            return
        if ntype in enc:
            self._put(out, START + bytes(nal[:hdr]) + self._decrypt_body(nal[hdr:]))
        else:
            self._put_inter(out, nal, hdr)              # inter (P/B) slice

    def _emit(self, out: bytearray, nal) -> None:  # HEVC RTP (2 baytli NAL header, VPS=tur 32)
        if len(nal) < 2:
            return
        self._classify(nal, 2, lambda b: ((b >> 1) & 0x3F) == 32, lambda x: x == 0x0C)
        self._emit_nal(out, nal)


