"""Foydalanuvchi matnlari: standart inglizcha, CLOUDCAM_LANG=uz bilan o'zbekcha."""
import pytest

from cloudcam import cli
from cloudcam.i18n import lang, t


def test_english_is_the_default(monkeypatch):
    monkeypatch.delenv("CLOUDCAM_LANG", raising=False)
    assert lang() == "en"
    assert t("Connecting...", "Ulanmoqda...") == "Connecting..."


@pytest.mark.parametrize("value", ["uz", "UZ", "uz_UZ.UTF-8"])
def test_uzbek_via_env(monkeypatch, value):
    monkeypatch.setenv("CLOUDCAM_LANG", value)
    assert lang() == "uz"
    assert t("Connecting...", "Ulanmoqda...") == "Ulanmoqda..."


def test_unknown_language_falls_back_to_english(monkeypatch):
    monkeypatch.setenv("CLOUDCAM_LANG", "de")
    assert t("Connecting...", "Ulanmoqda...") == "Connecting..."


def test_cli_help_follows_the_language(monkeypatch, capsys):
    monkeypatch.delenv("CLOUDCAM_LANG", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "fetch verification codes from the cloud" in capsys.readouterr().out

    monkeypatch.setenv("CLOUDCAM_LANG", "uz")
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "tasdiqlash kodlarini bulutdan olish" in capsys.readouterr().out
