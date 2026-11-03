"""Testlar uchun umumiy tayyorgarlik.

Bulut transporti (`cloudcam.vtm`) loyiha ichida — tashqi kutubxona stub'i
kerak emas. Bulutga chiqadigan joylar testlarning o'zida soxtasi bilan
almashtiriladi: testlar tarmoqsiz va hisobsiz ishlaydi.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
