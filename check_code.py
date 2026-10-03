"""Tasdiqlash kodini tekshirish (kutubxona ustidagi yupqa skript).

    python check_code.py <SERIAL> <KOD> [KANAL|all]
    python check_code.py <SERIAL>             # kodsiz: toza/shifrli ekanini aniqlaydi

Mantiq [[cloudcam.sources.cloud.verify]] da — shu yerda faqat chiqarish.
"""
import logging
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
logging.basicConfig(level=logging.CRITICAL)

from cloudcam.sources.cloud import keys as _keys
from cloudcam.sources.cloud.devices import make_client
from cloudcam.sources.cloud.verify import check_code

MESSAGES = {
    "correct":  "✅ {tag}: KOD TO'G'RI! Shifr ochildi.",
    "clear":    "⚠️  {tag}: kamera SHIFRLANMAGAN — kod kerak emas.",
    "wrong":    "❌ {tag}: KOD NOTO'G'RI! Boshqa kodni sinab ko'ring.",
    "timeout":  "⏱  {tag}: keyframe kelmadi (sekin/offline) — qayta urining.",
    "nostream": "❌ {tag}: oqim yo'q (kamera offline yoki kanal bo'sh).",
}


def main():
    if len(sys.argv) < 2:
        print("Foydalanish: python check_code.py <SERIAL> [KOD] [KANAL|all]")
        return
    serial = sys.argv[1]
    code = sys.argv[2] if len(sys.argv) > 2 else "AUTO"
    ch_arg = sys.argv[3] if len(sys.argv) > 3 else "1"
    channels = [1, 2, 3, 4] if ch_arg == "all" else [int(ch_arg)]

    client = make_client()
    for ch in channels:
        print(f"🔎 Tekshirilmoqda: {serial} ch{ch} (kod: {code}) ...")
        result = check_code(client, serial, code, ch)
        print(MESSAGES[result].format(tag=f"{serial} ch{ch}"))
        if result == "correct":
            _keys.set_code(serial, code)
            print(f"   💾 saqlandi: {serial} -> {code}")


if __name__ == "__main__":
    main()
