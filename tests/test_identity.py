"""Terminal kodi (`featureCode`) shu O'RNATMAGA xos bo'lishi kerak.

Ilgari uchta faylda bitta QATTIQ YOZILGAN kod bor edi. Bu shuni anglatardi:
loyihaning hamma nusxasi bulut uchun BITTA terminal. Bulut esa terminal bo'yicha
cheklaydi (EZVIZ da "terminal limit", code 1069) va shubhali faollikda CAPTCHA
(1015) so'raydi — ya'ni butunlay boshqa foydalanuvchining faolligi sizning
hisobingizga urib qo'yardi.

`pyezvizapi` va `ezviz-ha-addon` ikkalasi ham kodni o'rnatmaga bog'laydi.
"""
from pathlib import Path

import pytest

from cloudcam import identity
from cloudcam.settings import Settings, set_active

ROOT = Path(__file__).resolve().parents[1]
OLD_HARDCODED = "1fc28fa018178a1cd1c091b13b2f9f02"


@pytest.fixture(autouse=True)
def _tmp_home(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOUDCAM_FEATURE_CODE", raising=False)
    set_active(Settings(token_file=str(tmp_path / "token.json")))
    identity.reset_cache()
    yield
    identity.reset_cache()
    set_active(Settings())


def test_hardcoded_shared_code_is_gone_from_the_source():
    """Qattiq yozilgan umumiy kod qaytib kelmasligi kerak.

    `identity.py` — yagona istisno: u yerda kod faqat IZOHDA, nega tashlab
    yuborilgani tushuntirilgan."""
    allowed = {"cloudcam/identity.py", f"tests/{Path(__file__).name}"}
    offenders = [p.relative_to(ROOT).as_posix()
                 for p in ROOT.rglob("*.py")
                 if "venv" not in p.parts
                 and p.relative_to(ROOT).as_posix() not in allowed
                 and OLD_HARDCODED in p.read_text(encoding="utf-8")]
    assert not offenders, f"umumiy featureCode qolgan: {offenders}"


def test_every_featurecode_value_comes_from_identity():
    """Har bir `featureCode` qiymati [[feature_code]] dan olinishi kerak —
    to'g'ridan-to'g'ri yoki `self.FEATURE_CODE` xossasi orqali."""
    ok = ("feature_code()", "self.FEATURE_CODE")
    bad = [f"{p.name}:{i}"
           for p in (ROOT / "cloudcam").glob("*.py")
           for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
           if '"featureCode":' in line and not any(k in line for k in ok)]
    assert not bad, f"featureCode qattiq yozilgan: {bad}"


def test_code_is_stable_across_calls_and_persisted(tmp_path):
    """Bulut sizni "o'sha qurilma" deb tanishi uchun kod O'ZGARMASLIGI kerak —
    aks holda har ishga tushirishda yangi 2FA so'raladi."""
    first = identity.feature_code()
    assert len(first) == 32 and all(c in "0123456789abcdef" for c in first)
    identity.reset_cache()
    assert identity.feature_code() == first          # keshdan emas, fayldan
    assert (tmp_path / "feature_code.txt").read_text(encoding="utf-8").strip() == first


def test_saved_file_wins_over_recomputation(tmp_path):
    """MAC o'zgarsa ham (VPN/dok-stansiya) terminal o'zgarmasligi kerak."""
    (tmp_path / "feature_code.txt").write_text("a" * 32, encoding="utf-8")
    identity.reset_cache()
    assert identity.feature_code() == "a" * 32


def test_env_overrides_everything(monkeypatch, tmp_path):
    (tmp_path / "feature_code.txt").write_text("b" * 32, encoding="utf-8")
    monkeypatch.setenv("CLOUDCAM_FEATURE_CODE", "c" * 32)
    identity.reset_cache()
    assert identity.feature_code() == "c" * 32


def test_client_and_proxy_use_the_same_code():
    """Klient va proxy bir xil terminal bo'lib ko'rinishi shart — aks holda
    bulut ularni ikki qurilma deb hisoblaydi."""
    from cloudcam.client import CloudClient
    c = CloudClient("a@b.c", "pw")
    assert c.session.headers["featureCode"] == identity.feature_code()
