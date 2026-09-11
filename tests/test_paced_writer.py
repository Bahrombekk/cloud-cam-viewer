"""Bulut burstini tekislash + bulut soketini bloklamaslik.

O'lchov (haqiqiy kamera, 500 kadr): 491 kadr <10 ms oralig'ida keldi, qolgan
9 oraliq ~2000 ms — ya'ni butun GOP bir zumda keladi. Kadrni kelishi bilan
uzatsak tasvir 2s qotib, keyin sakrab o'tadi. `PacedWriter` kadrlarni siqilgan
holda buferlab, o'lchangan tezlikda chiqaradi.
"""
import threading
import time

from cloudcam.decrypt_proxy import START, PacedWriter


class FakeWfile:
    """Yozuvlarni vaqti bilan yozib boruvchi soxta soket."""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.times = []
        self.data = bytearray()
        self._lock = threading.Lock()

    def write(self, b: bytes) -> None:
        if self.delay:
            time.sleep(self.delay)      # sekin iste'molchi (ffmpeg/ko'ruvchi)
        with self._lock:
            self.times.append(time.monotonic())
            self.data += b


def _hevc_frame(key: bool, i: int, size: int = 64) -> bytes:
    """Bitta kadr: bitta VCL NAL (IRAP yoki inter)."""
    t = 19 if key else 1
    return START + bytes([(t << 1) & 0xFF, 0x01, i & 0xFF]) + bytes(size)


def test_burst_is_spread_out_over_time():
    w = FakeWfile()
    pw = PacedWriter(w, "hevc").start()
    burst = b"".join(_hevc_frame(i == 0, i) for i in range(12))
    pw.feed(burst)                       # HAMMASI bir zumda keldi (GOP bursti)
    time.sleep(0.75)
    pw.close()
    assert len(w.times) >= 8, "kadrlar chiqmadi"
    span = w.times[-1] - w.times[0]
    assert span > 0.2, f"burst tekislanmadi (hammasi {span:.3f}s ichida chiqdi)"


def test_slow_consumer_never_blocks_the_feeder():
    """Iste'molchi sekin bo'lsa ham `feed()` (bulut o'quvchi thread'i) kutmaydi —
    aks holda bulut soketi o'qilmay qoladi va paket yo'qoladi."""
    w = FakeWfile(delay=0.05)            # har yozuv 50 ms
    pw = PacedWriter(w, "hevc").start()
    t0 = time.monotonic()
    for i in range(40):
        pw.feed(_hevc_frame(i == 0, i))
    elapsed = time.monotonic() - t0
    pw.close()
    assert elapsed < 0.3, f"feed() bloklandi: {elapsed:.2f}s"


def test_overflow_drops_whole_frames_at_a_keyframe():
    w = FakeWfile()
    pw = PacedWriter(w, "hevc")           # thread ishga tushirilmaydi -> navbat to'ladi
    size = 512
    gop = 5                               # kalit kadr har 5 tada (~2.6 KB)
    pw.MAX_BYTES = 8192                   # ~3 GOP sig'adi
    for i in range(60):
        pw.feed(_hevc_frame(i % gop == 0, i, size))
    with pw._lock:
        assert pw.dropped > 0, "to'lib ketgan navbat tozalanmadi"
        # Navbat boshida KALIT kadr turishi kerak (GOP o'rtasidan kesilmagan).
        assert pw._q and pw._q[0][2] is True
        # Bufer chegara atrofida ushlanadi (bitta GOP zaxirasi bilan).
        assert pw._bytes <= pw.MAX_BYTES + gop * (size + 8)
    pw.close(drain=0.1)


def test_mpegts_passthrough_is_not_paced():
    """MPEG-TS (Annex-B emas) — kadr chegarasi yo'q, kechiktirmasdan o'tadi."""
    w = FakeWfile()
    pw = PacedWriter(w, None, paced=False).start()
    pw.feed(b"\x47" + bytes(187))
    pw.feed(b"\x47" + bytes(187))
    time.sleep(0.2)
    pw.close()
    assert len(w.data) == 2 * 188


def test_nothing_is_lost():
    w = FakeWfile()
    pw = PacedWriter(w, "hevc").start()
    frames = [_hevc_frame(i == 0, i) for i in range(10)]
    for f in frames:
        pw.feed(f)
    time.sleep(0.8)
    pw.close()
    assert bytes(w.data) == b"".join(frames)
