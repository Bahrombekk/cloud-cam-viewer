"""check_code.py — RTP oqimda codec'ga mos dekoder tanlanadi.

Avval har qanday RTP oqim HEVC dekoderiga berilardi: H.264 kamerada to'g'ri
kod ham "timeout"/"wrong" chiqardi.
"""
import itertools
import sys
import types

from Crypto.Cipher import AES

KEY = "ABCDEF"
PROFILE_HIGH = 100


def _import_check_code():
    sys.modules.setdefault("config", types.ModuleType("config"))
    import check_code
    return check_code


_seq = itertools.count()


def _rtp(payload: bytes) -> bytes:
    """12 baytli RTP header, PT=96, seq ketma-ket (takroriy deb tashlanmasin)."""
    seq = next(_seq) & 0xFFFF
    return bytes([0x80, 96, seq >> 8, seq & 0xFF]) + b"\x00" * 8 + payload


def _h264_sps(encrypt_with=None) -> bytes:
    body = bytes([PROFILE_HIGH]) + bytes(range(1, 32))  # 32 bayt body
    if encrypt_with:
        k = encrypt_with.encode().ljust(16, b"\0")[:16]
        body = AES.new(k, AES.MODE_ECB).encrypt(body)
    return b"\x67" + body                                # header toza (variant A)


class _Stream:
    def __init__(self, packets):
        self._p = packets

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def start(self):
        pass

    def iter_packets(self, max_packets=None):
        for b in self._p:
            yield types.SimpleNamespace(body=b)


def _run(monkeypatch, packets, code):
    cc = _import_check_code()
    monkeypatch.setattr(cc, "rtp_payload", lambda b: b[12:])
    monkeypatch.setattr(cc, "open_cloud_stream", lambda *a, **k: _Stream(packets))
    return cc.check(None, "SER1", code)


def test_clear_h264_rtp_is_detected(monkeypatch):
    assert _run(monkeypatch, [_rtp(_h264_sps())], KEY) == "clear"


def test_encrypted_h264_rtp_with_right_code(monkeypatch):
    assert _run(monkeypatch, [_rtp(_h264_sps(encrypt_with=KEY))], KEY) == "correct"


def test_encrypted_h264_rtp_with_wrong_code(monkeypatch):
    assert _run(monkeypatch, [_rtp(_h264_sps(encrypt_with=KEY))], "ZZZZZZ") == "wrong"


def test_packets_before_codec_marker_are_not_lost(monkeypatch):
    """Noaniq paketlar buferlanadi va codec aniqlangach dekoderga beriladi."""
    noise = _rtp(b"\x01" + b"\x00" * 20)              # oddiy slice — codec noaniq
    assert _run(monkeypatch, [noise, noise, _rtp(_h264_sps())], KEY) == "clear"
