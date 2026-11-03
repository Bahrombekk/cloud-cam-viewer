"""
Shifrlangan kamera "Tasdiqlash Kodu" (verification code) ni cam_keys.json ga saqlash.

Bu kod = videoni ochuvchi kalit. EZVIZ ilovasida kamerani ochganda so'raydigan
yoki qurilma yorlig'idagi kod. Saqlangach app.py o'sha kamerani avtomatik dekodlaydi.

Ishga tushirish:
    python set_code.py BD8712447 SCXYKW
    python set_code.py BD8712447 SCXYKW BD8712513 ABCDEF   # bir nechta (juft-juft)
"""

import sys

import config

from cloudcam import keys as _keys
from cloudcam.i18n import t


def main():
    args = sys.argv[1:]
    if len(args) < 2 or len(args) % 2 != 0:
        print(t("Usage", "Foydalanish")
              + ": python set_code.py <SERIAL> <CODE> [<SERIAL> <CODE> ...]")
        return

    # Kutubxonaning o'zi orqali: fayl 0600 bilan atomar yoziladi ([[cloudcam.keys]]).
    keys = _keys.load(config.CAMKEY_FILE)

    for i in range(0, len(args), 2):
        # Registr O'ZGARTIRILMAYDI: kod = AES kalit (registrga sezgir), ilovada
        # o'zgartirilgan kod kichik harfli bo'lishi mumkin. Ishonchingiz komil
        # bo'lmasa check_code.py dan foydalaning — u ikkala variantni sinaydi.
        serial, code = args[i], args[i + 1].strip()
        keys[serial] = code
        print(f"✅ {serial} -> {code}")

    _keys.save(keys, config.CAMKEY_FILE)
    print(t(f"\n💾 Saved {config.CAMKEY_FILE}: {list(keys)}",
            f"\n💾 {config.CAMKEY_FILE} saqlandi: {list(keys)}"))
    print("➡️  " + t("Next: python app.py", "Endi: python app.py"))


if __name__ == "__main__":
    main()
