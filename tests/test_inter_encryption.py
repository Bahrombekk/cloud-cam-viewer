"""Inter (P/B) slice shifri TAXMIN QILINMAYDI — oqimdan aniqlanadi.

Muammo: ba'zi kameralar faqat IRAP + param-set NAL'larini shifrlaydi (inter
toza), boshqalari HAMMASINI shifrlaydi. Noto'g'ri taxmin qilinsa har P-freym
buziladi — tasvir I-freymda tiklanib, orasida to'kiladi ("telefonda silliq,
bu yerda buzuq" shikoyati). O'lchov (40s oqim): taxmin bilan 246 ta HEVC
dekoder xatosi, to'g'ri qaror bilan 5 ta.

Bu yerda ikkala kamera turi sun'iy oqim bilan modellanadi va detektor to'g'ri
qaror qabul qilishi tekshiriladi.
"""
from Crypto.Cipher import AES

from cloudcam.decrypt_proxy import START, HevcRtpDecryptor

KEY = "VERIFYCODE1"
_AES_KEY = KEY.encode().ljust(16, b"\0")[:16]


def _enc(data: bytes) -> bytes:
    """NAL body'ni kamera qanday shifrlasa — shunday (ECB, 16 bayt bloklar)."""
    n = (len(data) // 16) * 16
    if n == 0:
        return data
    return AES.new(_AES_KEY, AES.MODE_ECB).encrypt(data[:n]) + data[n:]


def _hevc_hdr(nal_type: int) -> bytes:
    return bytes([(nal_type << 1) & 0xFF, 0x01])


def _vps() -> bytes:
    """VPS: header TOZA, body SHIFRLI (variant "A-body"). Toza body 0x0C bilan
    boshlanadi — `_classify` shu belgidan variantni aniqlaydi."""
    body = bytes([0x0C]) + bytes(range(1, 32))
    return _hevc_hdr(32) + _enc(body)


def _irap(i: int) -> bytes:
    body = bytes([0x26, i]) + bytes(range(2, 34))
    return _hevc_hdr(19) + _enc(body)


def _inter_clear(i: int) -> bytes:
    """TOZA inter slice: slice header baytlari TAKRORLANADI (bir xil PPS)."""
    body = bytes([0x02, 0x01, i]) + bytes(range(3, 35))
    return _hevc_hdr(1) + body


def _inter_encrypted(i: int) -> bytes:
    """SHIFRLANGAN inter slice — o'sha tuzilishli body, lekin shifrlangan."""
    body = bytes([0x02, 0x01, i]) + bytes(range(3, 35))
    return _hevc_hdr(1) + _enc(body)


def _run(nals) -> tuple[HevcRtpDecryptor, bytes]:
    dec = HevcRtpDecryptor(KEY)
    out = bytearray()
    for nal in nals:
        dec._emit(out, bytearray(nal))
    return dec, bytes(out)


def _stream(inter_fn, n: int = 12):
    return [_vps(), _irap(0)] + [inter_fn(i) for i in range(n)]


def test_variant_detected_as_encrypted_body():
    dec, _ = _run([_vps()])
    assert dec._decrypt is True
    assert (dec._clear, dec._drop) == (2, 0)   # "A-body": header toza, body shifr
    assert dec.key_error is False


def test_clear_inter_slices_are_left_alone():
    dec, out = _run(_stream(_inter_clear))
    assert dec._inter_enc is False, dec.inter_note
    # Toza inter slice O'ZGARMASDAN chiqadi (deshifrlansa buzilardi).
    assert START + _inter_clear(3) in out


def test_encrypted_inter_slices_are_decrypted():
    dec, out = _run(_stream(_inter_encrypted))
    assert dec._inter_enc is True, dec.inter_note
    # Deshifrlangan body tuzilishli bo'lib chiqadi (0x02 0x01 <i>).
    assert START + _hevc_hdr(1) + bytes([0x02, 0x01, 3]) in out


def test_output_order_is_preserved_across_the_decision():
    """Qaror chiqquncha chiqish buferlanadi — lekin TARTIB buzilmasligi shart,
    aks holda dekoder param-setlarni slice'lardan keyin oladi."""
    dec, out = _run(_stream(_inter_clear))
    assert dec._inter_enc is not None
    # Param-set va IRAP DESHIFRLANGAN holda chiqadi (header toza + toza body).
    i_vps = out.find(START + _hevc_hdr(32) + bytes([0x0C]))
    i_irap = out.find(START + _hevc_hdr(19) + bytes([0x26, 0]))
    i_first_inter = out.find(START + _inter_clear(0))
    assert 0 <= i_vps < i_irap < i_first_inter


def test_no_output_is_lost_while_buffering():
    dec, out = _run(_stream(_inter_clear, n=12))
    for i in range(12):
        assert START + _inter_clear(i) in out, f"inter {i} yo'qoldi"


def test_decision_needs_only_a_few_samples():
    """Qaror bir necha namunadan keyin chiqadi — tasvir kechikmasin."""
    dec, _ = _run(_stream(_inter_encrypted, n=8))
    assert dec._inter_enc is True
    assert len(dec._inter_clear) <= 8


def test_clear_stream_does_not_buffer_at_all():
    """Shifrsiz kamerada inter savoli yo'q — buferlash umuman yoqilmaydi."""
    clear_vps = _hevc_hdr(32) + bytes([0x0C]) + bytes(range(1, 32))
    dec = HevcRtpDecryptor(KEY)
    out = bytearray()
    dec._emit(out, bytearray(clear_vps))
    assert dec._decrypt is False
    assert dec._pending is None
    assert bytes(out) == START + clear_vps
