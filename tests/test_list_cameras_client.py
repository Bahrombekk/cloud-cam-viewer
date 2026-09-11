"""`list_cameras()` noto'g'ri turdagi klient uzatilsa ham ishlashi kerak.

`CloudCam.cameras()` o'zining `CloudClient` obyektini uzatardi, `list_cameras`
esa pyezvizapi `EzvizClient` kutadi (undan `_token` o'qiydi). Natijada so'rov
`https://none/v3/...` ga ketib `NameResolutionError` berardi — ya'ni yuqori
darajali API'ning kamera ro'yxati umuman ishlamasdi.
"""
import pytest

from cloudcam import decrypt_proxy as dp


class FakeCloudClient:
    """Yuqori darajali `CloudClient` kabi — `_token` yo'q."""
    api_url = "apiisgp.ezvizlife.com"
    session_id = "abc"


class FakeEzvizClient:
    def __init__(self):
        self._token = {"api_url": "apiisgp.ezvizlife.com", "session_id": "abc"}


@pytest.fixture
def spy(monkeypatch):
    used = {}
    made = {"n": 0}

    def _made_client():
        made["n"] += 1
        return FakeEzvizClient()

    monkeypatch.setattr(dp, "_make_client", _made_client)
    monkeypatch.setattr(dp, "_device_names", lambda c: used.setdefault("client", c) or {})
    monkeypatch.setattr(dp._cs, "get_vtm_page_list", lambda c: {"resourceInfos": []})
    return used, made


def test_wrong_client_type_is_replaced(spy):
    used, made = spy
    dp.list_cameras(FakeCloudClient())
    assert made["n"] == 1, "mos kelmaydigan klient almashtirilmadi"
    assert isinstance(used["client"], FakeEzvizClient)


def test_no_client_builds_one(spy):
    used, made = spy
    dp.list_cameras()
    assert made["n"] == 1


def test_valid_client_is_used_as_is(spy):
    used, made = spy
    c = FakeEzvizClient()
    dp.list_cameras(c)
    assert made["n"] == 0, "yaroqli klient bo'lsa yangisi qurilmasligi kerak"
    assert used["client"] is c
