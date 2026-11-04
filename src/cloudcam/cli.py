"""cloudcam buyruq qatori interfeysi.

    cloudcam cameras                 # kameralar ro'yxati
    cloudcam cameras --check         # ... va har kanal haqiqatan ishlaydimi
    cloudcam keys                    # tasdiqlash kodlarini bulutdan olish
    cloudcam snapshot <SERIAL>       # bitta surat saqlash (JPEG)
    cloudcam status                  # qurilmalar: onlayn, disklar, shifrlash
    cloudcam events [--watch]        # hodisalar (--watch: jonli push, cloudcam[events])
    cloudcam records <SERIAL>        # qurilmadagi (SD/NVR) yozuvlar
    cloudcam ptz <SERIAL> LEFT       # PTZ kamerani burish
    cloudcam view                    # OpenCV grid ko'ruvchi (viewer kerak)

Hisob ma'lumotlari: --email/--password, yoki CLOUDCAM_EMAIL/PASSWORD
muhit o'zgaruvchilari, yoki eski config.py.
"""
from __future__ import annotations

import argparse
import sys

from cloudcam.api import CloudCam
from cloudcam.sources.cloud.rest import PTZ_COMMANDS
from cloudcam.i18n import t
from cloudcam.settings import Settings


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
    if cam.migrated:
        import os
        where = os.path.dirname(s.token_file)
        print(t(f"Moved {', '.join(cam.migrated)} to {where} (originals kept; "
                f"you may delete them)",
                f"{', '.join(cam.migrated)} {where} ga ko'chirildi (asl fayllar "
                f"qoldi; o'chirsangiz bo'ladi)"), file=sys.stderr)
    return cam


def cmd_cameras(args) -> int:
    cam = _cam(args)
    try:
        cams = cam.cameras()
        states = cam.check(cams) if args.check else {}
        icons = {"ok": "live   ", "nokey": "NO CODE", "offline": "offline", "unknown": "?      "}
        for c in cams:
            st = icons.get(states.get((c.serial, c.channel)), "") if args.check else ""
            print(f"{c.serial}  ch{c.channel:<3} {st + '  ' if st else ''}{c.name}")
        if not cams:
            print(t("(no cameras found)", "(kamera topilmadi)"))
        if args.check and cams:
            n = {k: sum(v == k for v in states.values()) for k in icons}
            print()
            print(t(f"live {n['ok']}, no code {n['nokey']}, offline {n['offline']}, "
                    f"unknown {n['unknown']}",
                    f"ishlaydi {n['ok']}, kod yo'q {n['nokey']}, oflayn {n['offline']}, "
                    f"noma'lum {n['unknown']}"))
            return 1 if n["offline"] or n["nokey"] or n["unknown"] else 0
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
    from cloudcam.sources.cloud.keys import KeysFileError
    cam = _cam(args)
    try:
        serials = args.serial or None
        try:
            res = cam.fetch_keys(serials, mfa_code=args.code, overwrite=args.overwrite)
        except KeysFileError as e:
            sys.exit(str(e))
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
    from cloudcam.sources.isup import IsupError, from_settings
    from cloudcam.settings import Settings

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


def cmd_status(args) -> int:
    """Qurilmalar holati: online, disklar, shifrlash — bitta bulut so'rovi."""
    cam = _cam(args)
    bad = 0
    try:
        st = cam.status(args.serial or None)
        for serial, s in sorted(st.items()):
            online = (t("online", "onlayn") if s.online
                      else t("OFFLINE", "OFLAYN") if s.online is False else "?")
            disks = " ".join(
                f"#{d.slot}:" + (t("ok", "ok") if d.ok else t(f"ERR{d.state}", f"XATO{d.state}"))
                + (f"/{d.capacity_mb / 1024 / 1024:.1f}TB" if d.capacity_mb else "")
                for d in s.disks) or t("no disk", "disk yo'q")
            enc = t("encrypted", "shifrli") if s.encrypted else t("clear", "ochiq")
            priv = t("  PRIVACY", "  MAXFIY") if s.privacy else ""
            print(f"{serial}  {online:<8} {enc:<9} {disks}{priv}")
            bad += (s.online is False) + (not s.disks_ok)
        if not st:
            print(t("(no devices)", "(qurilma yo'q)"))
    finally:
        cam.close()
    return 1 if bad else 0


def _print_event(ev) -> None:
    ch = f" ch{ev.channel}" if ev.channel is not None else ""
    pic = t("  [picture]", "  [rasm]") if ev.picture_url else ""
    print(f"{ev.time_str or '-'}  {ev.serial}{ch}  {ev.kind}  {ev.title}{pic}", flush=True)


def _save_event_image(cam, ev, folder) -> None:
    import os
    import time as _time
    try:
        data = cam.event_image(ev)
    except Exception as e:  # noqa: BLE001 — bitta rasm butun lentani to'xtatmasin
        print(t(f"    image: {e}", f"    rasm: {e}"))
        return
    os.makedirs(folder, exist_ok=True)
    name = f"{ev.serial}_{ev.channel or 0}_{ev.msg_id or int(_time.time())}.jpg"
    path = os.path.join(folder, "".join(c for c in name if c.isalnum() or c in "._-"))
    with open(path, "wb") as f:
        f.write(data)
    print(f"    -> {path}")


def cmd_events(args) -> int:
    """Hodisalar: oxirgilari, yoki `--watch` bilan jonli (push/MQTT)."""
    cam = _cam(args)
    try:
        if args.watch:
            import time as _time

            def on_event(ev):
                _print_event(ev)
                if args.save_images and ev.picture_url:
                    _save_event_image(cam, ev, args.save_images)

            # Uzoq ishlaydi — sessiya eskirmasin (hodisa rasmi, qayta ulanish uchun).
            cam.start_token_refresh()
            with cam.push(on_event):
                print(t("Waiting for events (Ctrl+C to stop)...",
                        "Hodisalar kutilmoqda (to'xtatish: Ctrl+C)..."), flush=True)
                try:
                    while True:
                        _time.sleep(1)
                except KeyboardInterrupt:
                    pass
            return 0
        evs, _ = cam.events(args.serial or None, limit=args.limit)
        for ev in evs:
            _print_event(ev)
            if args.save_images and ev.picture_url:
                _save_event_image(cam, ev, args.save_images)
        if not evs:
            print(t("(no events)", "(hodisa yo'q)"))
    finally:
        cam.close()
    return 0


def cmd_records(args) -> int:
    """Qurilmadagi (SD/NVR) yozuvlar vaqt bo'yicha."""
    import datetime as _dt
    cam = _cam(args)
    try:
        end = _dt.datetime.now()
        recs = cam.records(args.serial, args.channel, end - _dt.timedelta(hours=args.hours), end)
        for r in recs:
            print(f"{r.start:%Y-%m-%d %H:%M:%S} - {r.end:%H:%M:%S}  "
                  f"{r.seconds / 60:5.1f} {t('min', 'daq')}  type={r.type}")
        total = sum(r.seconds for r in recs) / 3600
        print(t(f"{len(recs)} segments, {total:.1f} h recorded in the last {args.hours} h",
                f"{len(recs)} bo'lak, oxirgi {args.hours} soatda {total:.1f} soat yozuv"))
    finally:
        cam.close()
    return 0


def cmd_ptz(args) -> int:
    cam = _cam(args)
    try:
        cam.ptz(args.serial, args.command, channel=args.channel, speed=args.speed,
                duration=args.duration)
    finally:
        cam.close()
    return 0


def cmd_view(args) -> int:
    try:
        import cv2  # noqa: F401
    except ImportError:
        sys.exit(t("The viewer needs OpenCV:  pip install cloudcam[viewer]",
                   "Ko'rish uchun OpenCV kerak:  pip install cloudcam[viewer]"))
    from cloudcam import viewer
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
    cp.add_argument("--check", action="store_true",
                    help=t("check each channel live (offline / missing code)",
                           "har kanalni jonli tekshirish (oflayn / kod yo'q)"))
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

    stp = sub.add_parser("status", help=t("device status: online, disks, encryption",
                                          "qurilma holati: onlayn, disklar, shifrlash"))
    stp.add_argument("serial", nargs="*", help=t("default: every device",
                                                 "bo'sh qoldirilsa — hamma qurilma"))
    stp.set_defaults(fn=cmd_status)

    ep = sub.add_parser("events", help=t("motion/alarm events", "harakat/alarm hodisalari"))
    ep.add_argument("serial", nargs="*")
    ep.add_argument("--limit", type=int, default=20)
    ep.add_argument("--watch", action="store_true",
                    help=t("live push events (needs cloudcam[events])",
                           "jonli push hodisalari (cloudcam[events] kerak)"))
    ep.add_argument("--save-images", metavar="DIR", default=None,
                    help=t("save (decrypted) event pictures here",
                           "hodisa rasmlarini (ochib) shu papkaga saqlash"))
    ep.set_defaults(fn=cmd_events)

    rp = sub.add_parser("records", help=t("recordings on the device (SD/NVR)",
                                          "qurilmadagi yozuvlar (SD/NVR)"))
    rp.add_argument("serial")
    rp.add_argument("--channel", type=int, default=1)
    rp.add_argument("--hours", type=int, default=24)
    rp.set_defaults(fn=cmd_records)

    pp = sub.add_parser("ptz", help=t("move a PTZ camera", "PTZ kamerani burish"))
    pp.add_argument("serial")
    pp.add_argument("command", type=str.upper,
                    choices=sorted(PTZ_COMMANDS))
    pp.add_argument("--channel", type=int, default=1)
    pp.add_argument("--speed", type=int, default=5)
    pp.add_argument("--duration", type=float, default=0.5,
                    help=t("seconds to move", "necha soniya harakat"))
    pp.set_defaults(fn=cmd_ptz)

    vp = sub.add_parser("view", help=t("OpenCV grid viewer", "OpenCV grid ko'ruvchi"))
    vp.set_defaults(fn=cmd_view)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
