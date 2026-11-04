"""Testlar uchun umumiy tayyorgarlik.

Bulut transporti (`cloudcam.sources.cloud.vtm`) loyiha ichida — tashqi kutubxona stub'i
kerak emas. Bulutga chiqadigan joylar testlarning o'zida soxtasi bilan
almashtiriladi: testlar tarmoqsiz va hisobsiz ishlaydi.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# src/ layout: o'rnatilmagan holda ham `import cloudcam` ishlasin.
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Standart fayl joyi ([[cloudcam.settings.data_dir]]) — foydalanuvchining
    HAQIQIY `%APPDATA%`/`~/.config` papkasi. Testlar unga tegmasin."""
    monkeypatch.setenv("CLOUDCAM_HOME", str(tmp_path_factory.mktemp("cloudcam-home")))
