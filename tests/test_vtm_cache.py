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

from cloudcam import vtm_cache
from cloudcam.settings import Settings, set_active

SRC = Path(__file__).resolve().parents[1] / "cloudcam" / "decrypt_proxy.py"


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


def test_cache_is_only_invalidated_when_no_packets_arrived():
    """Manba matnini qo'riqlaymiz: `invalidate` chaqiruvi `got_packets`
    shartisiz qolsa, bitta kameraning uzilishi butun hisobni sekinlashtiradi.

    Ruxsat etilgan ikki kontekst:
      * `not got_packets` sharti — oqim ochilgan, lekin paket kelmagan;
      * `_fail_open` — oqim UMUMAN ochilmagan, ya'ni paket bo'lishi mumkin emas.
    """
    text = SRC.read_text(encoding="utf-8")
    calls = [m for m in re.finditer(r"vtm_cache\.invalidate\(", text)]
    assert calls, "kesh umuman bekor qilinmayapti"
    safe = ("not got_packets", "except Exception as e", "def _fail_open")
    for m in calls:
        line_start = text.rfind("\n", 0, m.start())
        context = text[max(0, line_start - 600):m.start()]
        assert any(k in context for k in safe), \
            "invalidate() himoyasiz joyda chaqirilgan"


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
