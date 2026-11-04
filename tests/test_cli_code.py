"""`cloudcam code set|check` va ularning eski skript o'ramlari.

Ilgari bu mantiq faqat `scripts/` ichida edi: `config.py` siz import qilib
bo'lmasdi, grid kodi esa `cloudcam view` bilan ikki joyda takrorlanardi.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cloudcam import cli
from cloudcam.settings import Settings, set_active

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def keyfile(tmp_path, monkeypatch):
    p = tmp_path / "cam_keys.json"
    # Muhit config.py dan USTUN — testlar haqiqiy cam_keys.json ga tegmaydi.
    monkeypatch.setenv("CLOUDCAM_CAMKEY_FILE", str(p))
    monkeypatch.setenv("CLOUDCAM_TOKEN_FILE", str(tmp_path / "token.json"))
    yield p
    set_active(Settings())


def test_code_set_keeps_the_case(keyfile):
    assert cli.main(["code", "set", "AA1", "mycode", "BB2", "ABCDEF"]) == 0
    assert json.loads(keyfile.read_text(encoding="utf-8")) == {"AA1": "mycode", "BB2": "ABCDEF"}


def test_code_set_refuses_an_unreadable_store(keyfile):
    keyfile.write_text('{"AA1": "X",}', encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        cli.main(["code", "set", "BB2", "Y"])
    assert "not overwriting" in str(e.value)
    assert keyfile.read_text(encoding="utf-8") == '{"AA1": "X",}'


def test_code_set_needs_pairs(keyfile):
    with pytest.raises(SystemExit):
        cli.main(["code", "set", "AA1", "X", "BB2"])


def test_code_check_saves_a_correct_code_and_tries_every_channel(keyfile, monkeypatch):
    from cloudcam.sources.cloud import codecheck, stream
    monkeypatch.setattr(stream, "_make_client", lambda s=None: object())
    seen = []

    def fake(client, serial, code, ch, on_retry=None):
        seen.append(ch)
        return code, "correct" if ch == 2 else "nostream"

    monkeypatch.setattr(codecheck, "check_with_case_fallback", fake)
    assert cli.main(["code", "check", "AA1", "abcdef", "--channel", "all"]) == 1
    assert seen == [1, 2, 3, 4]
    assert json.loads(keyfile.read_text(encoding="utf-8")) == {"AA1": "abcdef"}


def test_code_check_without_a_session_explains_what_to_do(keyfile):
    with pytest.raises(SystemExit) as e:
        cli.main(["code", "check", "AA1", "X"])
    assert "cloudcam cameras" in str(e.value)


def test_old_check_code_command_line_maps_to_the_cli():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import check_code
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    assert check_code._argv(["AA1", "CODE", "all"]) == ["code", "check", "AA1", "CODE",
                                                         "--channel", "all"]
    assert check_code._argv(["AA1"]) == ["code", "check", "AA1"]


def test_set_code_script_still_works(tmp_path):
    """Skript sifatida (subprocess) — `_bootstrap` paketni va config'ni topadi."""
    keys = tmp_path / "k.json"
    env = dict(os.environ, CLOUDCAM_CAMKEY_FILE=str(keys), CLOUDCAM_HOME=str(tmp_path))
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "set_code.py"), "AA1", "xYz"],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(keys.read_text(encoding="utf-8")) == {"AA1": "xYz"}


def test_grid_fits_the_window_instead_of_full_size_tiles():
    """23 kamera: ilgari ~6400x3600 kadr yig'ilib, oyna uni kichraytirardi."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from cloudcam import viewer
    frames = [np.zeros((720, 1280, 3), np.uint8)] * 22 + [None]
    st = {"connected": True, "fps": 12.0, "error": None}
    g = viewer.make_grid(frames, [f"cam{i}" for i in range(23)], [st] * 23, (1600, 900))
    assert g.shape[1] <= 1600 and g.shape[0] <= 900
    assert g.shape[1] >= 1600 - 5 and g.shape[0] >= 900 - 5
