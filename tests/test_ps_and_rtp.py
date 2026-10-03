"""PS selektiv shifr, PS bufer cheki va RTP ketma-ketlik nazorati."""
import itertools

from Crypto.Cipher import AES

from cloudcam import decrypt_proxy
from cloudcam.decrypt_proxy import HevcRtpDecryptor, PsStreamDecryptor

KEY = "ABCDEF"
_K = KEY.encode().ljust(16, b"\0")
START = b"\x00\x00\x00\x01"

VPS, SPS, PPS, IDR, TRAIL = (b"\x40\x01", b"\x42\x01", b"\x44\x01", b"\x26\x01", b"\x02\x01")


# ---- HEVC NAL yasash (variant A: header toza, body'ning 32 bayti shifrli) ----

def _body(first: int, i: int) -> bytes:
    # 32 bayt shifrlanadigan qism + 3 bayt toza dum; nol yo'q (start kod hosil bo'lmasin)
    return bytes([first]) + bytes((i * 7 + k) % 200 + 20 for k in range(34))


def _enc(body: bytes) -> bytes:
    return AES.new(_K, AES.MODE_ECB).encrypt(body[:32]) + body[32:]


def _nal(hdr: bytes, body: bytes, encrypt: bool) -> bytes:
    nal = hdr + (_enc(body) if encrypt else body)
    assert b"\x00\x00\x01" not in nal and nal[-1] != 0   # test ma'lumoti toza bo'lsin
    return nal


def _ps(nals) -> bytes:
    """Annex-B NAL'larni MPEG-PS ga o'raydi: pack header + bitta video PES har NAL."""
    out = bytearray()
    for n in nals:
        es = START + n
        out += b"\x00\x00\x01\xba" + b"\x44" + b"\x00" * 8 + b"\xf8"   # 14 bayt, stuffing=0
        out += b"\x00\x00\x01\xe0" + (3 + len(es)).to_bytes(2, "big") + b"\x80\x00\x00" + es
    return bytes(out)


def _stream(inter_encrypted: bool, n_inter: int = 10):
    head = [_nal(VPS, _body(0x0C, 0), True), _nal(SPS, _body(0x01, 1), True),
            _nal(PPS, _body(0xC1, 2), True), _nal(IDR, _body(0xAF, 3), True)]
    # P-slice body'ning 1-bayti TAKRORLANADI (slice header), qolgani farq qiladi
    inter = [_nal(TRAIL, _body(0xAF, 10 + i), inter_encrypted) for i in range(n_inter)]
    tail = [_nal(TRAIL, _body(0xAF, 99), inter_encrypted)]   # oxirgisi bufer'da qoladi
    return head, inter, tail


def test_ps_selective_encryption_keeps_clear_p_frames():
    """Avval PS yo'li HAMMA NAL'ni deshifrlardi -> toza P-freymlar buzilardi."""
    head, inter, tail = _stream(inter_encrypted=False)
    dec = PsStreamDecryptor(KEY)
    out = dec.feed(_ps(head + inter + tail))
    assert dec._inter_enc is False, dec.inter_note
    for n in inter:
        assert START + n in out                  # P-freym o'zgarmagan
    assert START + VPS + _body(0x0C, 0) in out   # IRAP/param-set deshifrlangan


def test_ps_full_encryption_still_decrypts_p_frames():
    head, inter, tail = _stream(inter_encrypted=True)
    dec = PsStreamDecryptor(KEY)
    out = dec.feed(_ps(head + inter + tail))
    assert dec._inter_enc is True, dec.inter_note
    for i in range(len(inter)):
        assert START + TRAIL + _body(0xAF, 10 + i) in out


def test_ps_buffer_does_not_grow_without_start_codes():
    dec = PsStreamDecryptor(KEY)
    junk = b"\xff" * (1 << 20)
    for _ in range(12):                          # 12 MB start kodsiz axlat
        dec.feed(junk)
    assert len(dec._buf) <= 2
    assert len(dec._es) <= decrypt_proxy._PS_MAX_BUF


# ---- RTP ----

_seq = itertools.count(100)


def _rtp(payload: bytes, seq=None) -> bytes:
    s = next(_seq) if seq is None else seq
    return bytes([0x80, 96, (s >> 8) & 0xFF, s & 0xFF]) + b"\x00" * 4 + b"\x11\x22\x33\x44" \
        + payload


def _clear_hevc_rtp():
    dec = HevcRtpDecryptor(KEY)
    dec.feed(_rtp(VPS + _body(0x0C, 0)))         # toza oqim deb aniqlanadi
    assert dec._decrypt is False
    return dec


def _fu(part: bytes, start=False, end=False) -> bytes:
    return b"\x62\x01" + bytes([(0x80 if start else 0) | (0x40 if end else 0) | 1]) + part


def test_rtp_fu_with_lost_fragment_is_dropped():
    dec = _clear_hevc_rtp()
    out = dec.feed(_rtp(_fu(b"A" * 20, start=True)))
    next(_seq)                                   # o'rtadagi bo'lak YO'QOLDI
    out += dec.feed(_rtp(_fu(b"C" * 20, end=True)))
    assert out == b"" and dec.seq_gaps == 1
    # keyingi butun NAL normal chiqadi
    out = dec.feed(_rtp(_fu(b"D" * 20, start=True))) + dec.feed(_rtp(_fu(b"E" * 20, end=True)))
    assert out == START + TRAIL + b"D" * 20 + b"E" * 20


def test_rtp_complete_fu_is_kept():
    dec = _clear_hevc_rtp()
    out = dec.feed(_rtp(_fu(b"A" * 20, start=True)))
    out += dec.feed(_rtp(_fu(b"B" * 20)))
    out += dec.feed(_rtp(_fu(b"C" * 20, end=True)))
    assert out == START + TRAIL + b"A" * 20 + b"B" * 20 + b"C" * 20
    assert dec.seq_gaps == 0


def test_rtp_duplicate_packet_is_ignored():
    dec = _clear_hevc_rtp()
    pkt = _rtp(TRAIL + _body(0xAF, 5))
    assert dec.feed(pkt) == START + TRAIL + _body(0xAF, 5)
    assert dec.feed(pkt) == b""
    assert dec.dup_packets == 1


def _pkt(pt, seq, payload=b"\x00" * 8):
    return bytes([0x80, pt, (seq >> 8) & 0xFF, seq & 0xFF]) + b"\x00" * 4 \
        + b"\x11\x22\x33\x44" + payload


def test_interleaved_audio_and_metadata_with_own_seq_is_not_a_gap():
    """Real Hik oqimi: bitta SSRC'da PT 96/112/0, HAR BIRI o'z seq'i bilan.
    FU o'rtasiga audio/metadata tushsa ham NAL saqlanishi kerak."""
    dec = HevcRtpDecryptor(KEY)
    dec.feed(_pkt(96, 1000, VPS + _body(0x0C, 0)))
    out = dec.feed(_pkt(96, 1001, _fu(b"A" * 20, start=True)))
    out += dec.feed(_pkt(0, 50))                 # audio — o'z hisoblagichi
    out += dec.feed(_pkt(112, 7000))             # metadata — o'z hisoblagichi
    out += dec.feed(_pkt(96, 1002, _fu(b"B" * 20)))
    out += dec.feed(_pkt(0, 51))
    out += dec.feed(_pkt(96, 1003, _fu(b"C" * 20, end=True)))
    assert out == START + TRAIL + b"A" * 20 + b"B" * 20 + b"C" * 20
    assert dec.seq_gaps == 0


def test_audio_gap_does_not_drop_video_nal():
    dec = HevcRtpDecryptor(KEY)
    dec.feed(_pkt(96, 10, VPS + _body(0x0C, 0)))
    dec.feed(_pkt(0, 100))
    out = dec.feed(_pkt(96, 11, _fu(b"A" * 20, start=True)))
    out += dec.feed(_pkt(0, 105))                # audio paketlari yo'qolgan
    out += dec.feed(_pkt(96, 12, _fu(b"C" * 20, end=True)))
    assert out == START + TRAIL + b"A" * 20 + b"C" * 20
    assert dec.seq_gaps == 0


def test_rtp_seq_wraparound_is_not_a_gap():
    dec = HevcRtpDecryptor(KEY)
    dec.feed(_rtp(VPS + _body(0x0C, 0), seq=0xFFFF))
    dec.feed(_rtp(TRAIL + _body(0xAF, 1), seq=0))
    assert dec.seq_gaps == 0
