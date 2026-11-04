"""Sozlama BOLA-JARAYONGA to'liq yetib borishi kerak.

Har kamera alohida jarayonda ishlaydi ([[stream_manager]]) va u sozlamani
FAQAT muhit o'zgaruvchilaridan oladi. Ilgari faqat `token_file` uzatilardi:
natijada `CloudCam(platform="ezviz")` deb ochilgan oqim bola-jarayonda
standart `hikconnect` deb qabul qilinib, noto'g'ri `clientType` yuborardi —
pagelist esa to'liq kelmasdi (ko'p NVR'li hisobda kameralar "topilmaydi").

Shu bilan birga PAROL uzatilmasligi kerak: u bola-jarayonga kerak emas
(u tokenni fayldan o'qiydi), muhit o'zgaruvchisi esa ko'p tizimlarda
jarayonlar ro'yxatidan ko'rinadi.
"""
import os
import re
from pathlib import Path

import cloudcam
from cloudcam.settings import Settings

PKG = Path(cloudcam.__file__).resolve().parent
MGR = PKG / "stream_manager.py"


def test_every_field_survives_the_round_trip(monkeypatch):
    s = Settings(platform="ezviz", substream=True, stream_mode="auto",
                 display_width=640, display_height=360, use_gpu=False,
                 proxy_start_port=9100, token_file="t.json",
                 camkey_file="k.json", region="apiisgp.ezvizlife.com")
    for k, v in s.to_env().items():
        monkeypatch.setenv(k, v)
    back = Settings.from_env()
    for field in ("platform", "substream", "stream_mode", "display_width",
                  "display_height", "use_gpu", "proxy_start_port",
                  "token_file", "camkey_file", "region"):
        want = getattr(s, field)
        if field in ("token_file", "camkey_file"):
            # Muhitdan kelgan yo'l ABSOLYUT qilinadi — bola-jarayon boshqa cwd
            # bilan ishlaydi ([[Settings.from_env]]).
            want = os.path.abspath(want)
        assert getattr(back, field) == want, field


def test_types_are_restored_not_left_as_strings(monkeypatch):
    """`display_width` matn bo'lib qolsa `numpy.reshape` yiqiladi."""
    monkeypatch.setenv("CLOUDCAM_DISPLAY_WIDTH", "854")
    monkeypatch.setenv("CLOUDCAM_USE_GPU", "0")
    monkeypatch.setenv("CLOUDCAM_SUBSTREAM", "1")
    s = Settings.from_env()
    assert s.display_width == 854 and isinstance(s.display_width, int)
    assert s.use_gpu is False and s.substream is True


def test_password_is_never_put_into_the_environment():
    s = Settings(email="a@b.c", password="maxfiy")
    env = s.to_env()
    assert "maxfiy" not in " ".join(env.values())
    assert "CLOUDCAM_PASSWORD" not in env


def test_manager_passes_the_whole_settings_object():
    """Manba matnini qo'riqlaymiz: faqat `token_file` uzatilsa, platforma
    yana standart qiymatga tushib qoladi."""
    text = MGR.read_text(encoding="utf-8")
    assert re.search(r"\w+\.child_env\(\)", text), \
        "bola-jarayonga TO'LIQ sozlama uzatilmayapti"


def test_secrets_are_not_in_repr():
    r = repr(Settings(email="a@b.c", password="maxfiy", isup_token="tok123"))
    assert "maxfiy" not in r and "tok123" not in r and "a@b.c" in r


def test_inherited_password_does_not_reach_the_child():
    """README `CLOUDCAM_PASSWORD` ni tavsiya qiladi — u os.environ orqali
    bola-jarayonga meros bo'lib o'tmasligi kerak."""
    base = {"CLOUDCAM_PASSWORD": "maxfiy", "CLOUDCAM_ISUP_TOKEN": "t", "PATH": "x"}
    env = Settings(platform="ezviz").child_env(base)
    assert "CLOUDCAM_PASSWORD" not in env and "CLOUDCAM_ISUP_TOKEN" not in env
    assert env["PATH"] == "x" and env["CLOUDCAM_PLATFORM"] == "ezviz"


def test_new_settings_need_no_extra_wiring():
    """`from_config_module` va `from_env` maydonlar bo'ylab avtomatik ishlaydi.

    `substream` aynan shu sababdan config.py'dan o'qilmay qolgan edi."""
    names = set(Settings().to_env())
    assert "CLOUDCAM_SUBSTREAM" in names and "CLOUDCAM_STREAM_MODE" in names


def test_config_py_is_found_in_the_working_directory(tmp_path, monkeypatch):
    """`cloudcam` console script'ida `sys.path` da joriy papka yo'q — config.py
    baribir o'qilishi kerak (ilgari CLI uni ko'rmay "Account required" derdi)."""
    import sys

    (tmp_path / "config.py").write_text('EMAIL = "a@b.c"\nPLATFORM = "ezviz"\n',
                                        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # Console script'dagidek: joriy papka ham, boshqa config.py bor papka ham yo'q.
    monkeypatch.setattr(sys, "path", [
        p for p in sys.path
        if p not in ("", ".") and not (Path(p) / "config.py").is_file()])
    monkeypatch.delitem(sys.modules, "config", raising=False)
    s = Settings.from_config_module()
    assert s.email == "a@b.c" and s.platform == "ezviz"
