"""cloudcam buyruq qatori interfeysi.

    cloudcam cameras                 # kameralar ro'yxati
    cloudcam keys                    # tasdiqlash kodlarini bulutdan olish
    cloudcam snapshot <SERIAL>       # bitta surat saqlash (JPEG)
    cloudcam view                    # OpenCV grid ko'ruvchi (viewer kerak)

Hisob ma'lumotlari: --email/--password, yoki CLOUDCAM_EMAIL/PASSWORD
muhit o'zgaruvchilari, yoki eski config.py.
"""
from __future__ import annotations

import argparse
import sys

from .api import CloudCam
from .settings import Settings


def _cam(args) -> CloudCam:
    s = Settings.resolve()
    if args.email:
        s = s.replace(email=args.email)
    if args.password:
        s = s.replace(password=args.password)
    if args.platform:
        s = s.replace(platform=args.platform)
    if not s.email or not s.password:
        sys.exit("Hisob kerak: --email/--password yoki CLOUDCAM_EMAIL/PASSWORD "
                 "(yoki config.py).")
    cam = CloudCam(settings=s)
    cam.login()
    return cam


def cmd_cameras(args) -> int:
    cam = _cam(args)
    try:
        cams = cam.cameras()
        for c in cams:
            print(f"{c.serial}  ch{c.channel}  {c.name}")
        if not cams:
            print("(kamera topilmadi)")
    finally:
        cam.close()
    return 0


def cmd_snapshot(args) -> int:
    cam = _cam(args)
    try:
        stream = cam.open(args.serial, channel=args.channel, key=args.key)
        if not stream.wait_first_frame(timeout=args.timeout):
            sys.exit("Kadr kelmadi (timeout). Kamera online/kalit to'g'rimi?")
        data = stream.snapshot(quality=args.quality)
        if not data:
            sys.exit("Surat olinmadi.")
        out = args.out or f"{args.serial}_ch{args.channel}.jpg"
        with open(out, "wb") as f:
            f.write(data)
        print(f"Saqlandi: {out} ({len(data)} bayt)")
    finally:
        cam.close()
    return 0


def cmd_keys(args) -> int:
    """Tasdiqlash kodlarini bulutdan olib `cam_keys.json` ga saqlaydi."""
    cam = _cam(args)
    try:
        serials = args.serial or None
        res = cam.fetch_keys(serials, mfa_code=args.code, overwrite=args.overwrite)
        if res.needs_mfa:
            info = cam.send_key_2fa()
            where = (info.get("contact") or {}).get("fuzzyContact", "emailingizga")
            print(f"2FA kod {where} yuborildi. Qayta ishga tushiring:\n"
                  f"  cloudcam keys --code <KOD>")
            return 2
        for serial, code in sorted(res.fetched.items()):
            print(f"{serial}  {code}")
        if res.skipped:
            print(f"({len(res.skipped)} ta kod allaqachon bor — "
                  f"qayta olish uchun --overwrite)")
        for serial, why in sorted(res.failed.items()):
            print(f"{serial}  XATO: {why}")
        if not res.fetched and not res.skipped:
            print("(kod olinmadi)")
    finally:
        cam.close()
    return 0


def cmd_view(args) -> int:
    try:
        import cv2  # noqa: F401
    except ImportError:
        sys.exit("Ko'rish uchun OpenCV kerak:  pip install cloudcam[viewer]")
    from . import viewer
    return viewer.run(_cam(args), args)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cloudcam",
                                description="EZVIZ / Hik-Connect bulut kameralari")
    p.add_argument("--email")
    p.add_argument("--password")
    p.add_argument("--platform", choices=["hikconnect", "ezviz"])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("cameras", help="kameralar ro'yxati").set_defaults(fn=cmd_cameras)

    sp = sub.add_parser("snapshot", help="bitta JPEG surat saqlash")
    sp.add_argument("serial")
    sp.add_argument("--channel", type=int, default=1)
    sp.add_argument("--key", default=None, help="shifr kodi (bo'lmasa cam_keys.json)")
    sp.add_argument("--out", default=None)
    sp.add_argument("--quality", type=int, default=85)
    sp.add_argument("--timeout", type=float, default=15.0)
    sp.set_defaults(fn=cmd_snapshot)

    kp = sub.add_parser("keys", help="tasdiqlash kodlarini bulutdan olish")
    kp.add_argument("serial", nargs="*", help="bo'sh qoldirilsa — hamma qurilma")
    kp.add_argument("--code", default=None, help="emailga kelgan 2FA kodi")
    kp.add_argument("--overwrite", action="store_true",
                    help="saqlangan kodlarni ham qayta olish")
    kp.set_defaults(fn=cmd_keys)

    vp = sub.add_parser("view", help="OpenCV grid ko'ruvchi")
    vp.set_defaults(fn=cmd_view)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
