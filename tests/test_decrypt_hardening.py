"""Dekodlovchining uchta qattiqlashtirilgan joyi.

Uchalasi ham `central-server` loyihasidagi o'sha dekoderdan ko'chirildi — u
yerda real kameralarda o'lchangan:

  1. IRAP DARVOZASI — bulut oqimi GOP O'RTASIDAN boshlanadi, Hik NVR esa
     param-setlarni davriy yuboradi. Ya'ni birinchi VPS/SPS/PPS dan keyin
     darhol IDR kelmaydi va o'sha joydan uzatsak dekoder ref kadrlarni
     topmaydi — tomoshabin boshida kul rang/buzilgan tasvir ko'radi.
  2. INTER-SHIFR QARORINING KESHI — bu kameraning O'ZGARMAS xususiyati, lekin
     har ochilishda qaytadan aniqlanardi va qaror chiqquncha NAL lar buferda
     ushlanardi. O'lchandi: birinchi kadrgacha 0.65-0.85s aynan shu kutish.
  3. PS BUFER CHEGARASI — start-kodsiz/buzuq oqimda demux buferi hech qachon
     bo'shamasdi va jarayon xotirasi oqardi.
"""
import json

import pytest
from Crypto.Cipher import AES

from cloudcam import decrypt_proxy as dp
from cloudcam.decrypt_proxy import START, HevcRtpDecryptor, PsStreamDecryptor
from cloudcam.settings import Settings, set_active

KEY = "VERIFYCODE1"
_AES_KEY = KEY.encode().ljust(16, b"\0")[:16]


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path):
    """Kesh fayllari testning o'z papkasiga tushsin."""
    set_active(Settings(token_file=str(tmp_path / "token.json")))
    yield tmp_path
    set_active(Settings())


def _enc(data: bytes) -> bytes:
    n = (len(data) // 16) * 16
    return AES.new(_AES_KEY, AES.MODE_ECB).encrypt(data[:n]) + data[n:] if n else data


def _hdr(nal_type: int) -> bytes:
    return bytes([(nal_type << 1) & 0xFF, 0x01])


def _vps() -> bytes:
    """VPS (tur 32): header toza, body shifrli -> variant "A-body"."""
    return _hdr(32) + _enc(bytes([0x0C]) + bytes(range(1, 32)))


def _clear_vps() -> bytes:
    """Shifrlanmagan VPS -> dekodlovchi "toza oqim" deb qaror qiladi."""
    return _hdr(32) + bytes([0x0C]) + bytes(range(1, 32))


def _irap(i: int = 0) -> bytes:
    return _hdr(19) + _enc(bytes([0x26, i]) + bytes(range(2, 34)))


def _inter(i: int) -> bytes:
    """TOZA inter slice (tur 1) — slice header baytlari takrorlanadi."""
    return _hdr(1) + bytes([0x02, 0x01, i]) + bytes(range(3, 35))


def _run(nals, *, cache_key=None):
    dec = HevcRtpDecryptor(KEY, cache_key=cache_key)
    out = bytearray()
    for nal in nals:
        dec._emit(out, bytearray(nal))
    return dec, bytes(out)


# ── 1. IRAP darvozasi ────────────────────────────────────────────────

def test_slices_before_the_first_irap_are_dropped():
    """Aks holda dekoder ref kadrsiz boshlaydi -> kul rang/buzuq tasvir."""
    dec, out = _run([_vps()] + [_inter(i) for i in range(10)])
    assert dec._seen_irap is False
    assert dec._gated == 10, "IRAP dan oldingi slice lar tashlanmadi"
    for i in range(10):
        assert START + _inter(i) not in out, f"inter {i} chiqib ketdi"


def test_parameter_sets_pass_the_gate():
    """SPS/PPS/VPS IRAP dan OLDIN ham kerak — dekoder ularsiz boshlay olmaydi."""
    dec, out = _run([_vps()] + [_inter(i) for i in range(10)])
    assert START + _hdr(32) + bytes([0x0C]) in out, "VPS darvozada ushlanib qoldi"


def test_the_gate_opens_on_the_first_irap_and_stays_open():
    dec, out = _run([_vps(), _irap(0)] + [_inter(i) for i in range(10)])
    assert dec._seen_irap is True
    assert START + _hdr(19) + bytes([0x26, 0]) in out
    for i in range(10):
        assert START + _inter(i) in out, f"IRAP dan keyin inter {i} yo'qoldi"


def test_the_gate_force_opens_so_a_camera_without_irap_still_shows():
    """IRAP belgilamaydigan kamerada tasvir umuman kelmay qolmasligi kerak."""
    n = dp._IRAP_MAX_WAIT + 5
    dec, out = _run([_vps()] + [_inter(i % 200) for i in range(n)])
    assert dec._seen_irap is True, "darvoza majburan ochilmadi"
    assert dec.gate_note and "majburan" in dec.gate_note
    assert dp._IRAP_MAX_WAIT <= 1000, "kutish juda uzun — ochilish sekinlashadi"


def test_the_gate_also_applies_to_a_clear_stream():
    """Toza oqim ham GOP o'rtasidan boshlanadi — darvoza o'sha yerda ham kerak."""
    clear_inter = _hdr(1) + bytes([0x02, 0x01, 7]) + bytes(range(3, 35))
    dec, out = _run([_clear_vps(), clear_inter])
    assert dec.encrypted is False
    assert START + clear_inter not in out
    assert dec._gated == 1


# ── 2. Inter-shifr qarorining keshi ──────────────────────────────────

def test_the_decision_is_cached_for_next_time(_cache_dir):
    dec, _ = _run([_vps(), _irap(0)] + [_inter(i) for i in range(10)],
                  cache_key="CAM1-1")
    assert dec._inter_enc is False
    blob = json.loads((_cache_dir / "enc-CAM1-1.json").read_text(encoding="utf-8"))
    assert blob == {"inter_enc": False}


def test_a_cached_decision_removes_the_startup_buffering(_cache_dir):
    """Kesh borligida birinchi inter slice DARHOL chiqadi — 8 namuna kutilmaydi.

    Aynan shu kutish o'lchovda birinchi kadrga 0.65-0.85s qo'shardi."""
    dp._enc_cache_store("CAM2-1", False)
    dec = HevcRtpDecryptor(KEY, cache_key="CAM2-1")
    assert dec._pending is None, "kesh bor, lekin baribir buferlanmoqda"
    out = bytearray()
    for nal in (_vps(), _irap(0), _inter(0)):
        dec._emit(out, bytearray(nal))
    assert START + _inter(0) in bytes(out), "birinchi inter slice ushlab qolindi"


def test_a_wrong_cached_decision_corrects_itself(_cache_dir):
    """Kamera proshivkasi o'zgarsa kesh eskiradi — o'zini tuzatishi shart."""
    dp._enc_cache_store("CAM3-1", True)          # noto'g'ri: aslida TOZA
    dec, _ = _run([_vps(), _irap(0)] + [_inter(i) for i in range(10)],
                  cache_key="CAM3-1")
    assert dec._inter_enc is False, "kesh xato bo'lsa ham tuzatilmadi"
    assert dec.inter_note and "kesh XATO" in dec.inter_note
    blob = json.loads((_cache_dir / "enc-CAM3-1.json").read_text(encoding="utf-8"))
    assert blob == {"inter_enc": False}, "kesh yangilanmadi"


def test_a_correct_cached_decision_is_left_alone(_cache_dir):
    dp._enc_cache_store("CAM4-1", False)
    dec, _ = _run([_vps(), _irap(0)] + [_inter(i) for i in range(10)],
                  cache_key="CAM4-1")
    assert dec._inter_enc is False
    assert dec._verifying is False, "tekshiruv tugamadi"
    assert dec.inter_note is None or "kesh XATO" not in dec.inter_note


def test_a_broken_cache_file_is_ignored(_cache_dir):
    (_cache_dir / "enc-CAM5-1.json").write_text("{buzuq", encoding="utf-8")
    assert dp._enc_cache_load("CAM5-1") is None
    dec = HevcRtpDecryptor(KEY, cache_key="CAM5-1")
    assert dec._inter_enc is None and dec._pending == []


def test_no_cache_key_means_no_cache_file(_cache_dir):
    """Kutubxona sifatida ishlatilganda kalit berilmasligi mumkin."""
    dec, _ = _run([_vps(), _irap(0)] + [_inter(i) for i in range(10)])
    assert dec._inter_enc is False
    assert not list(_cache_dir.glob("enc-*.json"))


# ── 3. MPEG-PS bufer chegarasi ───────────────────────────────────────

def test_a_startcodeless_stream_does_not_grow_the_buffer_forever():
    """Buzuq/start-kodsiz oqimda bufer cheksiz o'sardi — xotira oqishi."""
    dec = PsStreamDecryptor(KEY)
    for _ in range(6):
        dec.feed(b"\xff" * (1 << 20))
    assert len(dec._buf) <= dp._MAX_PS_BUF, "PS buferi chegaradan oshdi"
    assert dec.buf_resyncs > 0, "resync umuman bo'lmadi"


def test_resync_keeps_the_last_start_code():
    """Resync oxirgi start-kodni SAQLAYDI — keyingi NAL yo'qolmasin."""
    dec = PsStreamDecryptor(KEY)
    dec.feed(b"\xff" * (dp._MAX_PS_BUF + 16) + b"\x00\x00\x01\xe0")
    assert dec._buf.startswith(b"\x00\x00\x01"), "start-kod resyncda yo'qoldi"


def test_a_healthy_stream_never_resyncs():
    """Normal oqimda chegara hech qachon ishlamasligi kerak."""
    dec = PsStreamDecryptor(KEY)
    for _ in range(50):
        dec.feed(b"\x00\x00\x01\xba" + b"\x00" * 10 + b"\x00\x00\x01\xe0" + b"\x00" * 20)
    assert dec.buf_resyncs == 0
    assert len(dec._buf) < (1 << 20)
