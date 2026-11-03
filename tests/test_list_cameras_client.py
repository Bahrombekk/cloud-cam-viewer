"""`list_cameras()` qaysi klient bilan ishlashi.

Ilgari `list_cameras` pyezvizapi `EzvizClient` kutardi (undan `_token` o'qirdi),
`CloudCam.cameras()` esa o'zining `CloudClient` ini uzatardi — so'rov
`https://none/v3/...` ga ketib `NameResolutionError` berardi. Endi transport
o'zimizniki va `CloudClient` ni to'g'ridan-to'g'ri qabul qiladi.
"""
import pytest

from cloudcam import decrypt_proxy as dp


class FakeCloudClient:
    api_url = "apiisgp.ezvizlife.com"
    session_id = "abc"


class NoSession:
    """Login qilinmagan klient — sessiyasiz."""
    api_url = "apiisgp.ezvizlife.com"
    session_id = None


@pytest.fixture
def spy(monkeypatch):
    used = {}
    made = {"n": 0}

    def _made_client():
        made["n"] += 1
        return FakeCloudClient()

    monkeypatch.setattr(dp, "_make_client", _made_client)
    def _names(c):
        used["client"] = c
        return {}

    monkeypatch.setattr(dp, "_device_names", _names)
    monkeypatch.setattr(dp.vtm_cache, "pagelist", lambda c: {"resourceInfos": [
        {"deviceSerial": "S1", "localIndex": "0", "resourceName": "NVR"},
        {"deviceSerial": "S1", "localIndex": "2", "resourceName": "Gate"},
    ]})
    return used, made


def test_cloud_client_with_session_is_used_as_is(spy):
    used, made = spy
    c = FakeCloudClient()
    assert dp.list_cameras(c) == [("S1", 2, "Gate")]       # localIndex 0 = NVR o'zi
    assert made["n"] == 0, "yaroqli klient bo'lsa yangisi qurilmasligi kerak"
    assert used["client"] is c


def test_client_without_session_is_replaced(spy):
    used, made = spy
    dp.list_cameras(NoSession())
    assert made["n"] == 1
    assert isinstance(used["client"], FakeCloudClient)


def test_no_client_builds_one(spy):
    used, made = spy
    dp.list_cameras()
    assert made["n"] == 1
