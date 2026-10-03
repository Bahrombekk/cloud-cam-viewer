"""Tasdiqlash kodini qo'lda saqlash (kutubxona ustidagi yupqa skript).

    python set_code.py BD8712447 SCXYKW
    python set_code.py BD8712447 SCXYKW BD8712513 ABCDEF    # juft-juft

Kodni bulutdan AVTOMATIK olish uchun: `cloudcam keys`.
"""
import sys

from cloudcam.sources.cloud import keys as _keys


def main():
    args = sys.argv[1:]
    if len(args) < 2 or len(args) % 2 != 0:
        print("Foydalanish: python set_code.py <SERIAL> <KOD> [<SERIAL> <KOD> ...]")
        return
    for serial, code in zip(args[::2], args[1::2]):
        _keys.set_code(serial, code)
        print(f"✅ {serial} -> {code.strip().upper()}")
    print("\n➡️  Endi: python app.py")


if __name__ == "__main__":
    main()
