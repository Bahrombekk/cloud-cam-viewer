"""Tasdiqlash kodlarini BULUTDAN olish.

Ilgari kodni faqat qo'lda kiritish mumkin edi (`set_code.py`): 150 kamerali
hisobda bu har bir qurilma yorlig'ini o'qib chiqish, devorga o'rnatilgan yoki
oflayn NVR uchun esa umuman imkonsiz. `pyezvizapi.get_cam_auth_code` ko'rsatdi
ki, bulutning o'zi kodni beradi — `GET /v3/devconfig/authcode/query/<serial>`.

Eng nozik joyi 2FA: bulut BIRINCHI so'rovda sessiyani "ko'tarishni" talab
qiladi (meta.code 80000). Kod emailga keladi va faqat BIR MARTA kerak — u
sessiyani ko'taradi, qolgan hamma kamera kodsiz o'tadi. Shu xatti-harakat
buzilsa, 150 kamerali hisob 150 ta email so'rardi.
"""
import json

import pytest

from cloudcam import keys
from cloudcam.client import MfaRequired
from cloudcam.settings import Settings, set_active


@pytest.fixture(autouse=True)
def _store(tmp_path):
    set_active(Settings(camkey_file=str(tmp_path / "cam_keys.json")))
    yield
    set_active(Settings())


class FakeClient:
    """Bulut o'rniga: birinchi so'rov 2FA talab qiladi, kod bilan ochiladi."""

    def __init__(self, *, needs_mfa=True, codes=None, fail=()):
        self.needs_mfa = needs_mfa
        self.codes = codes or {}
        self.fail = set(fail)
        self.calls = []              # (serial, mfa_code)

    def get_verification_code(self, serial, mfa_code=None):
        self.calls.append((serial, mfa_code))
        if self.needs_mfa and mfa_code is None:
            raise MfaRequired("2FA kerak")
        if mfa_code is not None:
            self.needs_mfa = False   # sessiya ko'tarildi
        if serial in self.fail:
            raise RuntimeError("Qurilma ulanmagan (2009)")
        return self.codes.get(serial, "CODE" + serial[-1])


def test_codes_are_fetched_and_saved():
    c = FakeClient(needs_mfa=False, codes={"A1": "abcdef"})
    res = keys.fetch(c, ["A1"])
    assert res.fetched == {"A1": "ABCDEF"} or res.fetched == {"A1": "abcdef"}
    assert keys.load()["A1"] == res.fetched["A1"]


def test_mfa_is_requested_once_and_then_reused_for_the_rest():
    """2FA kodi FAQAT birinchi so'rovga beriladi — u sessiyani ko'taradi.

    Agar har kameraga qayta yuborilsa, bulut kodni "ishlatilgan" deb rad
    etadi va ikkinchi kameradan boshlab hammasi yiqilardi."""
    c = FakeClient(needs_mfa=True)
    res = keys.fetch(c, ["A1", "A2", "A3"], mfa_code="123456")
    assert not res.needs_mfa
    assert len(res.fetched) == 3
    assert c.calls[0] == ("A1", "123456")
    assert [mfa for _s, mfa in c.calls[1:]] == [None, None]


def test_fetch_stops_immediately_when_2fa_is_required():
    """Kodsiz davom etish 150 ta befoyda so'rov degani."""
    c = FakeClient(needs_mfa=True)
    res = keys.fetch(c, ["A1", "A2", "A3"])
    assert res.needs_mfa is True
    assert res.fetched == {}
    assert len(c.calls) == 1, "2FA so'ralgach to'xtash kerak"


def test_existing_codes_are_not_refetched():
    keys.save({"A1": "KEEP"})
    c = FakeClient(needs_mfa=False)
    res = keys.fetch(c, ["A1", "A2"])
    assert res.skipped == ["A1"] and "A2" in res.fetched
    assert keys.load()["A1"] == "KEEP"
    assert [s for s, _ in c.calls] == ["A2"]


def test_auto_placeholder_counts_as_missing():
    """`app.py` kodi yo'q kameraga "AUTO" qo'yadi — u kod EMAS."""
    keys.save({"A1": "AUTO"})
    c = FakeClient(needs_mfa=False, codes={"A1": "REAL"})
    res = keys.fetch(c, ["A1"])
    assert res.fetched == {"A1": "REAL"}


def test_one_broken_camera_does_not_stop_the_others():
    c = FakeClient(needs_mfa=False, fail={"A2"})
    res = keys.fetch(c, ["A1", "A2", "A3"])
    assert set(res.fetched) == {"A1", "A3"}
    assert "2009" in res.failed["A2"]


def test_store_is_written_atomically(tmp_path):
    keys.save({"A1": "X"})
    p = tmp_path / "cam_keys.json"
    assert json.loads(p.read_text(encoding="utf-8")) == {"A1": "X"}
    assert not list(p.parent.glob("*.tmp")), "vaqtinchalik fayl qoldi"


def test_broken_store_does_not_crash():
    from cloudcam.settings import get_active
    open(get_active().camkey_file, "w", encoding="utf-8").write("{buzuq")
    assert keys.load() == {}
