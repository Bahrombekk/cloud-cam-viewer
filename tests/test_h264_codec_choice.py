"""RTP oqimda CODEC aniqlanishi — H.264 kameralar ham ishlashi kerak.

`check_code.py` RTP uchun DOIM `HevcRtpDecryptor` yaratardi. H.264 da SPS
bayti 0x67 bo'lib, HEVC qoidasida bu tur 51 (VPS=32 emas) — ya'ni param-set
hech qachon topilmasdi, `key_error` ham qo'yilmasdi. Natija: H.264 kamerada
KOD TO'G'RI bo'lsa ham doim "keyframe kelmadi" chiqardi. `decrypt_proxy.serve`
esa codec'ni to'g'ri aniqlardi — ya'ni ikki yo'l bir-biriga zid edi.
"""
from pathlib import Path

from Crypto.Cipher import AES

from cloudcam.decrypt_proxy import (
    START, H264RtpDecryptor, HevcRtpDecryptor, detect_rtp_codec,
)

ROOT = Path(__file__).resolve().parents[1]
KEY = "VERIFYCODE1"
_AES_KEY = KEY.encode().ljust(16, b"\0")[:16]


def _enc(data: bytes) -> bytes:
    n = (len(data) // 16) * 16
    return AES.new(_AES_KEY, AES.MODE_ECB).encrypt(data[:n]) + data[n:] if n else data


def _sps() -> bytes:
    """H.264 SPS: header toza (0x67), body shifrli. Toza body'ning 1-bayti —
    profile_idc (100 = High), `_classify` shu belgidan variantni aniqlaydi."""
    body = bytes([100, 0x00, 0x1F]) + bytes(range(3, 34))
    return bytes([0x67]) + _enc(body)


def test_codec_detection_separates_h264_from_hevc():
    assert detect_rtp_codec(0x67) == "h264"          # SPS
    assert detect_rtp_codec(0x68) == "h264"          # PPS
    assert detect_rtp_codec(0x65) == "h264"          # IDR
    assert detect_rtp_codec(28) == "h264"            # FU-A
    assert detect_rtp_codec(0x40) == "hevc"          # VPS
    assert detect_rtp_codec(49 << 1) == "hevc"       # FU
    assert detect_rtp_codec(0x41) is None            # oddiy slice — noaniq


def _inter_clear(i: int) -> bytes:
    """TOZA P-slice: slice header baytlari takrorlanadi (bir xil PPS)."""
    return bytes([0x41]) + bytes([0x02, 0x01, i]) + bytes(range(3, 35))


def test_h264_decryptor_detects_the_encrypted_variant():
    dec = H264RtpDecryptor(KEY)
    out = bytearray()
    dec._emit(out, bytearray(_sps()))
    assert dec.encrypted is True, "H.264 SPS shifrli deb tanilmadi"
    assert (dec._clear, dec._drop) == (1, 0)         # header toza, body shifrli
    assert dec.key_error is False
    # Chiqish inter-qaror chiqquncha navbatda ushlanadi ([[_put_inter]]) —
    # bir necha P-slice bergach deshifrlangan SPS chiqishi kerak.
    for i in range(10):
        dec._emit(out, bytearray(_inter_clear(i)))
    assert dec._inter_enc is False, dec.inter_note
    assert START + bytes([0x67]) + bytes([100, 0x00, 0x1F]) in bytes(out)


def test_hevc_decryptor_is_blind_to_h264_and_reports_nothing():
    """Aynan shu — eski xatoning sababi: noto'g'ri dekodlovchi XATO ham
    bermaydi, shunchaki jim qoladi va chaqiruvchi "timeout" deb o'ylaydi."""
    dec = HevcRtpDecryptor(KEY)
    out = bytearray()
    for _ in range(20):
        dec._emit(out, bytearray(_sps()))
    assert dec.encrypted is None and dec.key_error is False
    assert bytes(out) == b"", "noto'g'ri codec'da chiqish bo'lmasligi kerak"


def test_encrypted_property_is_public():
    """`check_code.py` ilgari `dec._decrypt` ni o'qirdi — ichki nom."""
    dec = H264RtpDecryptor(KEY)
    assert dec.encrypted is None
    dec._decrypt = False
    assert dec.encrypted is False


def test_check_code_script_picks_the_codec():
    """`check_code.py` ni import qilib bo'lmaydi (u `config` ni talab qiladi),
    shuning uchun manba matni qo'riqlanadi."""
    text = (ROOT / "check_code.py").read_text(encoding="utf-8")
    assert "detect_rtp_codec" in text, "codec aniqlanmayapti"
    assert "H264RtpDecryptor" in text, "H.264 dekodlovchi ishlatilmayapti"
    assert "._decrypt" not in text, "ichki atribut o'qilmoqda — `encrypted` ishlating"
