"""Jitter buferi — bulut burstini tekislab yozadi.

Codec'ga bog'liq emas: faqat Annex-B chegaralarini ko'rib kadrlarga bo'ladi.
"""
from __future__ import annotations

import threading
import time

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


