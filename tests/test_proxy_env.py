"""Proxy subprocess'iga uzatiladigan sozlamalar.

Proxy `cwd=loyiha ildizi` bilan ishga tushadi — nisbiy token yo'li boshqa
faylni qidirib proxy darhol yiqilardi (pip o'rnatilganda cwd=site-packages).
Platforma uzatilmasa proxy standart "hikconnect" clientType'ini ishlatardi.
"""
import os

from cloudcam import settings as settings_mod
from cloudcam import stream_manager
from cloudcam.settings import Settings


def _captured_env(monkeypatch, decrypt):
    monkeypatch.setattr(settings_mod, "_active",
                        Settings(platform="ezviz", token_file="token.json"))
    seen = {}

    def fake_popen(cmd, **kw):
        seen.update(kw, cmd=cmd)
        return None

    monkeypatch.setattr(stream_manager.subprocess, "Popen", fake_popen)
    cs = stream_manager.CameraStream("SER1", 1, 8799, decrypt=decrypt, key="ABCDEF")
    cs._start_proxy()
    return seen


def test_token_file_is_absolute_for_decrypt_proxy(monkeypatch):
    seen = _captured_env(monkeypatch, decrypt=True)
    tok = seen["env"]["CLOUDCAM_TOKEN_FILE"]
    assert os.path.isabs(tok)
    assert tok == os.path.abspath("token.json")


def test_platform_is_passed_to_proxy(monkeypatch):
    seen = _captured_env(monkeypatch, decrypt=True)
    assert seen["env"]["CLOUDCAM_PLATFORM"] == "ezviz"


def test_key_is_not_on_command_line(monkeypatch):
    """Kod = AES kalit; argv process ro'yxatida boshqalarga ko'rinadi."""
    seen = _captured_env(monkeypatch, decrypt=True)
    assert "ABCDEF" not in seen["cmd"]
    assert seen["env"][stream_manager.KEY_ENV] == "ABCDEF"


def test_proxy_reads_key_from_env(monkeypatch):
    from cloudcam import decrypt_proxy
    assert decrypt_proxy.KEY_ENV == stream_manager.KEY_ENV
    monkeypatch.setenv(decrypt_proxy.KEY_ENV, "QWERTY")
    assert decrypt_proxy.resolve_key("-") == "QWERTY"
    assert decrypt_proxy.resolve_key("ABCDEF") == "ABCDEF"   # qo'lda ishga tushirish


def test_token_file_is_absolute_for_plain_proxy(monkeypatch):
    seen = _captured_env(monkeypatch, decrypt=False)
    cmd = seen["cmd"]
    assert os.path.isabs(cmd[cmd.index("--token-file") + 1])
