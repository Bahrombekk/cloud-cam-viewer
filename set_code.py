"""
Shifrlangan kamera "Tasdiqlash Kodu" (verification code) ni cam_keys.json ga saqlash.

Bu kod = videoni ochuvchi kalit. EZVIZ ilovasida kamerani ochganda so'raydigan
yoki qurilma yorlig'idagi kod. Saqlangach app.py o'sha kamerani avtomatik dekodlaydi.

Ishga tushirish:
    python set_code.py BD8712447 SCXYKW
    python set_code.py BD8712447 SCXYKW BD8712513 ABCDEF   # bir nechta (juft-juft)
"""

import json
import os
import sys

import config


def main():
    args = sys.argv[1:]
    if len(args) < 2 or len(args) % 2 != 0:
        print("Foydalanish: python set_code.py <SERIAL> <CODE> [<SERIAL> <CODE> ...]")
        return

    keys = {}
    if os.path.exists(config.CAMKEY_FILE):
        try:
            with open(config.CAMKEY_FILE) as f:
                keys = json.load(f)
        except Exception:
            pass

    for i in range(0, len(args), 2):
        serial, code = args[i], args[i + 1].strip().upper()
        keys[serial] = code
        print(f"✅ {serial} -> {code}")

    with open(config.CAMKEY_FILE, "w") as f:
        json.dump(keys, f, indent=2)
    print(f"\n💾 {config.CAMKEY_FILE} saqlandi: {list(keys)}")
    print("➡️  Endi: python app.py")


if __name__ == "__main__":
    main()
