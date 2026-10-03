"""Bola-jarayon -> ota-jarayon hodisalari ([[cloudcam.core.ipc]]).

Ilgari bayroq fayl yo'li IKKI joyda mustaqil qurilardi: dekodlovchi proxy'da
va `stream_manager` da. Ikki nusxa sezdirmay uzoqlashishi mumkin edi — nom
qolipi bir tomonda o'zgarsa, bayroq umuman ko'rinmay qolardi va "kod xato"
xabari hech qachon yetib bormasdi.
"""
import pathlib
import re

from cloudcam.core import ipc

ROOT = pathlib.Path(__file__).resolve().parents[1] / "cloudcam"


def test_raise_and_take(tmp_path, monkeypatch):
    monkeypatch.setattr(ipc.tempfile, "gettempdir", lambda: str(tmp_path))
    assert ipc.take_flag(ipc.KEY_ERROR, "S1", 1) is False
    ipc.raise_flag(ipc.KEY_ERROR, "S1", 1)
    assert ipc.take_flag(ipc.KEY_ERROR, "S1", 1) is True


def test_flags_are_per_camera_and_per_channel(tmp_path, monkeypatch):
    """NVR ning 2-kanali 1-kanalning xatosini meros qilib olmasligi kerak."""
    monkeypatch.setattr(ipc.tempfile, "gettempdir", lambda: str(tmp_path))
    ipc.raise_flag(ipc.OFFLINE, "S1", 1)
    assert ipc.take_flag(ipc.OFFLINE, "S1", 2) is False
    assert ipc.take_flag(ipc.OFFLINE, "S2", 1) is False


def test_kinds_do_not_collide(tmp_path, monkeypatch):
    monkeypatch.setattr(ipc.tempfile, "gettempdir", lambda: str(tmp_path))
    ipc.raise_flag(ipc.KEY_ERROR, "S1", 1)
    assert ipc.take_flag(ipc.OFFLINE, "S1", 1) is False


def test_clear_removes_both_kinds(tmp_path, monkeypatch):
    """Har yangi urinishdan oldin tozalanadi — eski xato yopishib qolmasin."""
    monkeypatch.setattr(ipc.tempfile, "gettempdir", lambda: str(tmp_path))
    ipc.raise_flag(ipc.KEY_ERROR, "S1", 1)
    ipc.raise_flag(ipc.OFFLINE, "S1", 1)
    ipc.clear_flags("S1", 1)
    assert not ipc.take_flag(ipc.KEY_ERROR, "S1", 1)
    assert not ipc.take_flag(ipc.OFFLINE, "S1", 1)


def test_clear_on_a_clean_slate_is_harmless(tmp_path, monkeypatch):
    monkeypatch.setattr(ipc.tempfile, "gettempdir", lambda: str(tmp_path))
    ipc.clear_flags("S1", 1)          # xato ko'tarmasligi kerak


def test_nobody_builds_flag_paths_by_hand():
    """Yo'l qolipi FAQAT core/ipc.py da bo'lsin — takror nusxa qaytmasin."""
    offenders = []
    for f in ROOT.rglob("*.py"):
        if f.relative_to(ROOT).as_posix() == "core/ipc.py":
            continue
        if re.search(r'ezviz_(keyerr|offline)', f.read_text(encoding="utf-8")):
            offenders.append(f.relative_to(ROOT).as_posix())
    assert not offenders, f"bayroq yo'li qo'lda qurilmoqda: {offenders}"
