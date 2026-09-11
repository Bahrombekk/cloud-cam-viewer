"""cloudcam buyruq qatori interfeysi.

    cloudcam cameras                 # kameralar ro'yxati
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

    vp = sub.add_parser("view", help="OpenCV grid ko'ruvchi")
    vp.set_defaults(fn=cmd_view)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
