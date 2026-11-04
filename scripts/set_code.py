"""
Shifrlangan kamera "Tasdiqlash Kodu" (verification code) ni cam_keys.json ga saqlash.

Bu kod = videoni ochuvchi kalit. EZVIZ ilovasida kamerani ochganda so'raydigan
yoki qurilma yorlig'idagi kod. Saqlangach app.py o'sha kamerani avtomatik dekodlaydi.

Ishga tushirish:
    python scripts/set_code.py AB2345678 ABCDEF
    python scripts/set_code.py AB2345678 ABCDEF AB4567890 ABCDEF   # bir nechta (juft-juft)
"""

import sys

import os as _os  # noqa: E402
import sys as _sys  # noqa: E402

# config.py: joriy papkada yoki loyiha ildizida (scripts/ ichidan ham ishlaydi).
_sys.path[:0] = [_os.getcwd(),
                 _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))]

import config

# token.json / cam_keys.json config.py yonida turadi — qayerdan ishga tushirilsa ham.
if getattr(config, "__file__", None):
    _os.chdir(_os.path.dirname(_os.path.abspath(config.__file__)))

from cloudcam.sources.cloud import keys as _keys
from cloudcam.i18n import t


def main():
    args = sys.argv[1:]
    if len(args) < 2 or len(args) % 2 != 0:
        print(t("Usage", "Foydalanish")
              + ": python scripts/set_code.py <SERIAL> <CODE> [<SERIAL> <CODE> ...]")
        return

    # Kutubxonaning o'zi orqali: fayl 0600 bilan atomar yoziladi ([[cloudcam.sources.cloud.keys]]).
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
    print("➡️  " + t("Next: python scripts/app.py", "Endi: python scripts/app.py"))


if __name__ == "__main__":
    main()
