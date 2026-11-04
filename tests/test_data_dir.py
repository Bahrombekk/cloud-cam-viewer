"""Sessiya, kodlar va terminal ID qayerda saqlanadi.

Ilgari standart `token.json` NISBIY edi: buyruq qaysi papkadan ishga
tushirilsa, sessiya va `feature_code.txt` o'sha yerda yaratilardi. Bitta
kompyuterda ikki xil terminal ID paydo bo'lib, bulut bitta hisobni ikki
"qurilma" deb ko'rardi (jonli holatda `scripts/` ichida ikkinchi nusxa topilgan).
"""
import os

from cloudcam import settings as settings_mod
from cloudcam.core import paths
from cloudcam.settings import Settings, data_dir


def test_defaults_live_in_the_user_data_dir_not_the_cwd(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CLOUDCAM_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.token_file == str(home / "token.json")
    assert s.camkey_file == str(home / "cam_keys.json")
    assert os.path.isabs(s.token_file)


def test_platform_default_dirs(monkeypatch):
    monkeypatch.delenv("CLOUDCAM_HOME", raising=False)
    d = data_dir()
    assert os.path.isabs(d) and os.path.basename(d) == "cloudcam"
    if os.name == "nt" and os.environ.get("APPDATA"):
        assert d == os.path.join(os.environ["APPDATA"], "cloudcam")


def test_relative_paths_in_config_py_are_next_to_config_py(tmp_path, monkeypatch):
    """`TOKEN_FILE = "token.json"` — config.py YONIDA, joriy papkadan qat'i nazar."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "config.py").write_text('TOKEN_FILE = "token.json"\nCAMKEY_FILE = "k/c.json"\n',
                                    encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", proj / "config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(settings_mod, "_load_config_module", lambda: mod)
    monkeypatch.chdir(elsewhere)
    s = Settings.from_config_module()
    assert s.token_file == str(proj / "token.json")
    assert s.camkey_file == os.path.join(str(proj), "k/c.json")


def test_env_paths_are_made_absolute(tmp_path, monkeypatch):
    """Bola-jarayonlar boshqa cwd bilan ishlaydi — nisbiy yo'l boshqa faylni topardi."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLOUDCAM_TOKEN_FILE", "t.json")
    assert Settings.from_env().token_file == str(tmp_path / "t.json")


def _legacy(dir_):
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "token.json").write_text('{"session_id": "s"}', encoding="utf-8")
    (dir_ / "cam_keys.json").write_text('{"A1": "ABCDEF"}', encoding="utf-8")
    (dir_ / "feature_code.txt").write_text("a" * 32, encoding="utf-8")


def test_legacy_files_are_copied_once_with_the_terminal_id(tmp_path, monkeypatch):
    """`feature_code.txt` ham ko'chishi SHART — aks holda bulut "yangi qurilma"
    deb qayta 2FA so'raydi."""
    home, old = tmp_path / "home", tmp_path / "old"
    monkeypatch.setenv("CLOUDCAM_HOME", str(home))
    _legacy(old)
    got = paths.migrate_legacy(Settings(), str(old))
    assert sorted(got) == ["cam_keys.json", "feature_code.txt", "token.json"]
    assert (home / "feature_code.txt").read_text(encoding="utf-8") == "a" * 32
    assert (old / "token.json").exists(), "asl fayl o'chirilmasligi kerak"
    assert paths.migrate_legacy(Settings(), str(old)) == [], "ikkinchi marta ko'chirmaydi"


def test_existing_session_in_the_new_place_is_never_overwritten(tmp_path, monkeypatch):
    home, old = tmp_path / "home", tmp_path / "old"
    monkeypatch.setenv("CLOUDCAM_HOME", str(home))
    home.mkdir()
    (home / "token.json").write_text('{"session_id": "NEW"}', encoding="utf-8")
    _legacy(old)
    assert paths.migrate_legacy(Settings(), str(old)) == []
    assert "NEW" in (home / "token.json").read_text(encoding="utf-8")


def test_explicit_paths_are_not_migrated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDCAM_HOME", str(tmp_path / "home"))
    _legacy(tmp_path / "old")
    s = Settings(token_file=str(tmp_path / "mine.json"))
    assert paths.migrate_legacy(s, str(tmp_path / "old")) == []
