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
from .i18n import t
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
        sys.exit(t("Account required: --email/--password or "
                   "CLOUDCAM_EMAIL/CLOUDCAM_PASSWORD (or config.py).",
                   "Hisob kerak: --email/--password yoki CLOUDCAM_EMAIL/PASSWORD "
                   "(yoki config.py)."))
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
            print(t("(no cameras found)", "(kamera topilmadi)"))
    finally:
        cam.close()
    return 0


def cmd_snapshot(args) -> int:
    cam = _cam(args)
    try:
        stream = cam.open(args.serial, channel=args.channel, key=args.key)
        if not stream.wait_first_frame(timeout=args.timeout):
            sys.exit(t("No frame (timeout). Is the camera online and the key correct?",
                       "Kadr kelmadi (timeout). Kamera online/kalit to'g'rimi?"))
        data = stream.snapshot(quality=args.quality)
        if not data:
            sys.exit(t("Could not take a snapshot.", "Surat olinmadi."))
        out = args.out or f"{args.serial}_ch{args.channel}.jpg"
        with open(out, "wb") as f:
            f.write(data)
        print(t(f"Saved: {out} ({len(data)} bytes)", f"Saqlandi: {out} ({len(data)} bayt)"))
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
            where = (info.get("contact") or {}).get("fuzzyContact",
                                                      t("your email", "emailingizga"))
            print(t(f"2FA code sent to {where}. Run again:\n"
                    f"  cloudcam keys --code <CODE>",
                    f"2FA kod {where} yuborildi. Qayta ishga tushiring:\n"
                    f"  cloudcam keys --code <KOD>"))
            return 2
        for serial, code in sorted(res.fetched.items()):
            print(f"{serial}  {code}")
        if res.skipped:
            n = len(res.skipped)
            print(t(f"({n} codes already stored — use --overwrite to re-fetch)",
                    f"({n} ta kod allaqachon bor — qayta olish uchun --overwrite)"))
        for serial, why in sorted(res.failed.items()):
            print(t(f"{serial}  ERROR: {why}", f"{serial}  XATO: {why}"))
        if not res.fetched and not res.skipped:
            print(t("(no codes fetched)", "(kod olinmadi)"))
    finally:
        cam.close()
    return 0


def cmd_isup(args) -> int:
    """ISUP ko'prigi: qurilmalar, kalitlar, holat. Bulut hisobi kerak emas."""
    from .isup import IsupError, from_settings
    from .settings import Settings

    bridge = from_settings(Settings.resolve())
    try:
        if args.isup_cmd == "keys":
            keys = {}
            for pair in args.pair:
                dev, _, key = pair.partition("=")
                if not key:
                    sys.exit(t(f"Format: <DeviceID>=<KEY> (got: {pair!r})",
                               f"Format: <DeviceID>=<KALIT> (berildi: {pair!r})"))
                keys[dev] = key
            res = bridge.set_keys(keys, default=not args.no_default)
            kicked = res.get("kicked")
            print(t(f"Keys sent: {len(keys)} device(s)"
                    + (f", {kicked} disconnected" if kicked else ""),
                    f"Kalitlar yuborildi: {len(keys)} ta qurilma"
                    + (f", {kicked} ta uzildi" if kicked else "")))
            return 0

        st = bridge.status()
        if not st.keys_set:
            print(t("(no keys sent yet — `cloudcam isup keys ...`)",
                    "(kalitlar hali yuborilmagan — `cloudcam isup keys ...`)"))
        for dev in st.devices:
            flag = "" if dev.valid else t("  [suspicious DeviceID format]",
                                          "  [DeviceID formati shubhali]")
            ch_w, on_w = t("channels", "kanal"), t("online", "onlayn")
            print(f"{dev.id}  {dev.ip or '-':<15} {dev.firmware or '-':<12} "
                  f"{ch_w}:{dev.channels_total} {on_w}:{dev.online_seconds}s{flag}")
            for s in dev.streams:
                kind = "sub " if s.sub else "main"
                live = t("live", "jonli") if s.live else t("idle", "jim")
                print(f"    ch{s.channel} {kind} {live}  {s.bytes / 1e6:.1f} MB")
        for r in st.rejected:
            print(t("REJECTED", "RAD ETILDI")
                  + f" {r.get('id')}: {r.get('reason')} x{r.get('count')}")
        if not st.devices:
            print(t("(no devices connected)", "(ulangan qurilma yo'q)"))
    except IsupError as e:
        sys.exit(str(e))
    return 0


def cmd_view(args) -> int:
    try:
        import cv2  # noqa: F401
    except ImportError:
        sys.exit(t("The viewer needs OpenCV:  pip install cloudcam[viewer]",
                   "Ko'rish uchun OpenCV kerak:  pip install cloudcam[viewer]"))
    from . import viewer
    return viewer.run(_cam(args), args)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="cloudcam",
        description=t("Watch and decrypt EZVIZ / Hik-Connect cloud cameras "
                      "and Hikvision ISUP devices",
                      "EZVIZ / Hik-Connect bulut kameralari"))
    p.add_argument("--email")
    p.add_argument("--password")
    p.add_argument("--platform", choices=["hikconnect", "ezviz"])
    sub = p.add_subparsers(dest="cmd", required=True)

    cp = sub.add_parser("cameras", help=t("list cameras", "kameralar ro'yxati"))
    cp.set_defaults(fn=cmd_cameras)

    sp = sub.add_parser("snapshot", help=t("save one JPEG snapshot", "bitta JPEG surat saqlash"))
    sp.add_argument("serial")
    sp.add_argument("--channel", type=int, default=1)
    sp.add_argument("--key", default=None, help=t("verification code (default: cam_keys.json)",
                           "shifr kodi (bo'lmasa cam_keys.json)"))
    sp.add_argument("--out", default=None)
    sp.add_argument("--quality", type=int, default=85)
    sp.add_argument("--timeout", type=float, default=15.0)
    sp.set_defaults(fn=cmd_snapshot)

    kp = sub.add_parser("keys", help=t("fetch verification codes from the cloud",
                                      "tasdiqlash kodlarini bulutdan olish"))
    kp.add_argument("serial", nargs="*",
                    help=t("default: every device", "bo'sh qoldirilsa — hamma qurilma"))
    kp.add_argument("--code", default=None,
                    help=t("2FA code from your email", "emailga kelgan 2FA kodi"))
    kp.add_argument("--overwrite", action="store_true",
                    help=t("re-fetch codes that are already stored",
                           "saqlangan kodlarni ham qayta olish"))
    kp.set_defaults(fn=cmd_keys)

    ip = sub.add_parser("isup", help=t("ISUP 5.0 bridge (cloud-free cameras)",
                                     "ISUP 5.0 ko'prigi (bulutsiz kameralar)"))
    isub = ip.add_subparsers(dest="isup_cmd")
    isub.add_parser("devices", help=t("connected devices and streams",
                                       "ulangan qurilmalar va oqimlar"))
    ik = isub.add_parser("keys", help=t("send device keys", "qurilma kalitlarini yuborish"))
    ik.add_argument("pair", nargs="+", metavar="DeviceID=KALIT")
    ik.add_argument("--no-default", action="store_true",
                    help=t("do not fall back to ISUP_KEY (recommended)",
                           "zaxira ISUP_KEY ishlatilmasin (tavsiya etiladi)"))
    ip.set_defaults(fn=cmd_isup, isup_cmd="devices")

    vp = sub.add_parser("view", help=t("OpenCV grid viewer", "OpenCV grid ko'ruvchi"))
    vp.set_defaults(fn=cmd_view)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
