"""Sir fayllar 0600 bilan, atomar yoziladi (central-server audit 2026-11-03)."""
import json
import os
import stat

import pytest

from cloudcam.core import fsutil

from cloudcam.sources.cloud import keys
from cloudcam.sources.cloud.client import CloudClient

posix = pytest.mark.skipif(os.name != "posix", reason="POSIX huquqlari faqat Linux/macOS'da")


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_write_private_is_atomic_and_leaves_no_temp(tmp_path):
    p = tmp_path / "s.json"
    fsutil.write_private(str(p), "a")
    fsutil.write_private(str(p), "b")
    assert p.read_text(encoding="utf-8") == "b"
    assert [x.name for x in tmp_path.iterdir()] == ["s.json"]


@posix
def test_token_and_keys_are_owner_only(tmp_path):
    c = CloudClient("a@b.c", "pw")
    tok = tmp_path / "token.json"
    c.save_token(str(tok))
    assert _mode(tok) == 0o600
    kp = tmp_path / "cam_keys.json"
    keys.save({"BD1": "ABCDEF"}, str(kp))
    assert _mode(kp) == 0o600
    assert json.loads(kp.read_text()) == {"BD1": "ABCDEF"}


@posix
def test_old_world_readable_files_are_hardened_on_read(tmp_path):
    kp = tmp_path / "cam_keys.json"
    kp.write_text('{"BD1": "X"}')
    os.chmod(kp, 0o644)
    keys.load(str(kp))
    assert _mode(kp) == 0o600
