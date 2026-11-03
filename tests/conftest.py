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
