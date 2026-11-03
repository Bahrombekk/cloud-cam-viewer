"""Hisob darajasidagi bulut metama'lumot keshi.

Keshsiz har kamera, har qayta ulanishda pagelist (~4.3s) + VTDU token (~1.8s)
uchun bulutga boradi. O'lchov: issiq kesh 0.24s, keshsiz 5.76s.

Eng nozik joy — keshni QACHON bekor qilish: kesh HISOB bo'yicha, shuning uchun
bitta kameraning uzilishi tufayli o'chirilsa, o'sha hisobdagi HAMMA kamera
qayta to'liq metama'lumot yuklaydi.
"""
import json
import re
from pathlib import Path

import pytest

import cloudcam
from cloudcam.sources.cloud import vtm_cache
from cloudcam.settings import Settings, set_active

PKG = Path(cloudcam.__file__).resolve().parent
SRC = PKG / "decrypt_proxy.py"


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path):
    set_active(Settings(token_file=str(tmp_path / "token.json")))
    yield
    set_active(Settings())


def test_store_and_load_roundtrip():
    vtm_cache.store("acc", "pagelist", {"resourceInfos": [{"deviceSerial": "A1"}]})
    assert vtm_cache.load("acc", "pagelist")["resourceInfos"][0]["deviceSerial"] == "A1"


def test_expired_entry_is_ignored(monkeypatch):
    vtm_cache.store("acc", "vtdu", {"t": 1})
    monkeypatch.setenv("CLOUDCAM_VTM_TTL", "0")
    assert vtm_cache.load("acc", "vtdu") is None


def test_invalidate_removes_it():
    vtm_cache.store("acc", "vtdu", {"t": 1})
    vtm_cache.invalidate("acc", "vtdu")
    assert vtm_cache.load("acc", "vtdu") is None


def test_corrupt_cache_file_is_not_fatal():
    p = Path(vtm_cache.cache_dir()) / "vtm-acc-pagelist.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{bu json emas", encoding="utf-8")
    assert vtm_cache.load("acc", "pagelist") is None


def test_channel_gate_finds_a_missing_channel():
    vtm_cache.store("acc", "pagelist", {"resourceInfos": [
        {"deviceSerial": "NVR1", "channelNo": 1},
        {"deviceSerial": "NVR1", "channelNo": 2},
    ]})
    assert vtm_cache.channel_missing("acc", "NVR1", 8) is True
    assert vtm_cache.channel_missing("acc", "NVR1", 2) is False


def test_channel_gate_stays_silent_when_unsure():
    """Kesh yo'q / seriya yo'q / bir kanalli qurilma -> HECH NARSA da'vo qilmaymiz."""
    assert vtm_cache.channel_missing("acc", "NVR1", 3) is False       # kesh yo'q
    vtm_cache.store("acc", "pagelist", {"resourceInfos": [{"deviceSerial": "CAM1"}]})
    assert vtm_cache.channel_missing("acc", "CAM1", 1) is False       # kanalsiz qurilma
    assert vtm_cache.channel_missing("acc", "BOSHQA", 1) is False     # seriya yo'q


def test_cache_is_refreshed_not_emptied_and_only_when_no_packets_arrived():
    """Manba matnini qo'riqlaymiz.

    * Proksi keshni HECH QACHON o'chirmaydi (`invalidate`) — bulut xato bersa
      bo'sh kesh hisobdagi hamma kamerani ~6s sekinlashtirardi; o'rniga
      `refresh` (kesh faqat muvaffaqiyatda almashadi).
    * `refresh` faqat paket kelmaganda: `not got_packets` sharti yoki `_fail_open`
      (oqim UMUMAN ochilmagan)."""
    text = SRC.read_text(encoding="utf-8")
    assert not re.search(r"vtm_cache\.invalidate\(", text), \
        "proksi keshni o'chirmasin — vtm_cache.refresh ishlating"
    calls = list(re.finditer(r"vtm_cache\.refresh\(", text))
    assert calls, "eskirgan metama'lumot umuman yangilanmayapti"
    safe = ("not got_packets", "def _fail_open")
    for m in calls:
        line_start = text.rfind("\n", 0, m.start())
        context = text[max(0, line_start - 900):m.start()]
        assert any(k in context for k in safe), \
            "refresh() himoyasiz joyda chaqirilgan"


# ---- o'ram: hisob MIJOZGA bog'langan, zanjirlanmaydi ----------------------

class _Client:
    pass


@pytest.fixture
def fake_cloud(monkeypatch):
    """Bulut so'rovlarini (`vtm.cloud.fetch_*`) soxtasi bilan almashtiradi."""
    from cloudcam.sources.cloud.vtm import cloud as _cloud

    state = {"pagelist": {}, "calls": 0, "fail": False}

    def pagelist(client):
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("504 Gateway Timeout")
        return state["pagelist"][client.account]

    def vtdu(client):
        return {"token": client.account}

    monkeypatch.setattr(_cloud, "fetch_vtm_page_list", pagelist)
    monkeypatch.setattr(_cloud, "fetch_vtdu_token", vtdu)
    return vtm_cache, state


def _client(account, tmp_path):
    c = _Client()
    c.account = account
    vtm_cache.tag(c, str(tmp_path / f"{account}.json"))
    return c


def test_two_accounts_in_one_process_do_not_chain(fake_cloud, tmp_path):
    """Ikkinchi hisob birinchisining qurilma ro'yxatini OLMASLIGI kerak
    (central-server'da 4 hisobdan 3 tasi bir xil ro'yxatni ko'rardi)."""
    vc, state = fake_cloud
    state["pagelist"] = {"A": {"res": ["camA"]}, "B": {"res": ["camB"]}}
    a, b = _client("A", tmp_path), _client("B", tmp_path)
    assert vc.pagelist(a) == {"res": ["camA"]}
    assert vc.pagelist(b) == {"res": ["camB"]}
    assert vtm_cache.load("B", "pagelist") == {"res": ["camB"]}
    assert vtm_cache.load("A", "pagelist") == {"res": ["camA"]}


def test_untagged_client_is_not_cached(fake_cloud):
    vc, state = fake_cloud
    state["pagelist"] = {"X": {"res": [1]}}
    c = _Client()
    c.account = "X"
    vc.pagelist(c)
    vc.pagelist(c)
    assert state["calls"] == 2                             # kesh yo'q — begona hisobga yozilmaydi


def test_refresh_keeps_old_cache_when_cloud_fails(fake_cloud, tmp_path):
    """Bulut xato bersa eski kesh JOYIDA qoladi (ilgari invalidate uni bo'shatardi)."""
    vc, state = fake_cloud
    state["pagelist"] = {"A": {"res": ["old"]}}
    a = _client("A", tmp_path)
    vc.pagelist(a)
    state["fail"] = True
    assert vtm_cache.refresh(a) is False
    assert vtm_cache.load("A", "pagelist") == {"res": ["old"]}
    state["fail"] = False
    state["pagelist"] = {"A": {"res": ["new"]}}
    assert vtm_cache.refresh(a) is True                    # bypass_read: keshni chetlab bulutga
    assert vtm_cache.load("A", "pagelist") == {"res": ["new"]}


def test_new_camera_found_by_one_pagelist_refresh(fake_cloud, tmp_path, monkeypatch):
    """Hisobga yangi qo'shilgan kamera eski keshda yo'q — bitta yangilash uni topadi,
    lekin yangi ro'yxat qayta-qayta so'ralmaydi (oflayn kameralar bulutni urmasin)."""
    vc, state = fake_cloud
    state["pagelist"] = {"A": {"res": ["cam1"]}}
    a = _client("A", tmp_path)
    vc.pagelist(a)
    state["pagelist"] = {"A": {"res": ["cam1", "cam2"]}}
    assert vtm_cache.refresh_pagelist(a, "A") is False     # ro'yxat juda yangi (< 30s)
    real_age = vtm_cache.age
    monkeypatch.setattr(vtm_cache, "age", lambda k, kind: 120.0)
    assert vtm_cache.refresh_pagelist(a, "A") is True
    monkeypatch.setattr(vtm_cache, "age", real_age)
    assert vtm_cache.load("A", "pagelist") == {"res": ["cam1", "cam2"]}


def test_warm_only_when_half_expired(fake_cloud, tmp_path, monkeypatch):
    vc, state = fake_cloud
    state["pagelist"] = {"A": {"res": [1]}}
    a = _client("A", tmp_path)
    vc.pagelist(a)
    calls = state["calls"]
    assert vtm_cache.warm(a, "A") is False                 # yangi kesh — tegmaydi
    assert state["calls"] == calls
    monkeypatch.setattr(vtm_cache, "age", lambda k, kind: vtm_cache._ttl())
    assert vtm_cache.warm(a, "A") is True
    assert state["calls"] == calls + 1


def test_open_failure_handler_never_runs_after_streaming_started():
    """`_fail_open` "paket kelmadi" deb faraz qiladi — shuning uchun u
    yozuvchi (`PacedWriter`) ishga tushgandan KEYIN chaqirilmasligi kerak,
    aks holda o'rtada uzilgan oqim butun hisobning keshini o'chirardi."""
    text = SRC.read_text(encoding="utf-8")
    writer_at = text.index("PacedWriter(self.wfile")
    for m in re.finditer(r"_fail_open\(", text):
        assert m.start() < writer_at, \
            "_fail_open() oqim boshlangandan keyin chaqirilmoqda"


def test_missing_resource_is_not_stale_metadata():
    from cloudcam.decrypt_proxy import _is_missing_resource

    assert _is_missing_resource(Exception("Could not find VTM resource for serial X")) is True
    assert _is_missing_resource(Exception("Device offline or unreachable")) is False
    assert _is_missing_resource(None) is False


def test_cache_file_is_atomic_json():
    vtm_cache.store("acc", "pagelist", {"a": 1})
    p = Path(vtm_cache.cache_dir()) / "vtm-acc-pagelist.json"
    blob = json.loads(p.read_text(encoding="utf-8"))
    assert "ts" in blob and blob["data"] == {"a": 1}
    assert not list(p.parent.glob("*.tmp")), "vaqtinchalik fayl qoldi"


def test_cloudcam_starts_and_stops_warmer(monkeypatch, tmp_path):
    """Login'dan keyin kesh fonda issiq tutiladi; `warm_cache=False` o'chiradi."""
    from cloudcam import api

    started = []

    class _W:
        def __init__(self, make_client, key, interval=None):
            self.key = key

        def start(self):
            started.append(self.key)
            return self

        def stop(self):
            started.append("stop")

    monkeypatch.setattr(vtm_cache, "Warmer", _W)
    cam = api.CloudCam(token_file=str(tmp_path / "token.json"))
    monkeypatch.setattr(cam.client, "connect", lambda tf: "resumed")
    cam.login()
    assert len(started) == 1
    cam.login()                                   # ikkinchi marta — yangi thread yo'q
    assert len(started) == 1
    cam.close()
    assert started[-1] == "stop"

    off = api.CloudCam(token_file=str(tmp_path / "token.json"), warm_cache=False)
    monkeypatch.setattr(off.client, "connect", lambda tf: "resumed")
    off.login()
    assert len(started) == 2                      # faqat oldingi stop qo'shilgan
