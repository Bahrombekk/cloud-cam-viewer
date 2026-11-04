"""Multi-camera grid viewer with camera selection.

Same viewer as `cloudcam view`, but first lists the cameras with their live
state and lets you pick which ones to open:

    python scripts/app.py

Selection: comma-separated numbers (1,3,5), `all`, or `live` (only the cameras
that are streaming). Keys in the window: q quit, g grid, 1-9 fullscreen, s status.
"""
import _bootstrap  # noqa: F401  (sys.path, stdout)

from cloudcam import CloudCam
from cloudcam.i18n import t

LABELS = {"ok": "live", "nokey": "NO CODE", "offline": "offline", "unknown": "?"}


def choose_cameras(cams, states=None):
    """`cams` — [[cloudcam.api.Camera]]; `states` — `CloudCam.check()` natijasi."""
    states = states or {}
    print()
    for i, c in enumerate(cams, 1):
        st = LABELS.get(states.get((c.serial, c.channel)), "")
        print(f"  [{i:2}] {st:<8} {c.name}  ({c.serial} ch{c.channel})")
    if not cams:
        print(t("  (no cameras found)", "  (kamera topilmadi)"))
        return []
    if states:
        n = {k: sum(v == k for v in states.values()) for k in LABELS}
        print(t(f"\nlive {n['ok']}, no code {n['nokey']}, offline {n['offline']}, "
                f"unknown {n['unknown']}",
                f"\nishlaydi {n['ok']}, kod yo'q {n['nokey']}, oflayn {n['offline']}, "
                f"noma'lum {n['unknown']}"))
    raw = input(t("Select (e.g. 1,3,5 | all | live): ",
                  "Tanlang (masalan 1,3,5 | all | live): ")).strip().lower()
    if raw == "all":
        return list(cams)
    if raw == "live":
        return [c for c in cams if states.get((c.serial, c.channel)) == "ok"]
    picked = []
    for part in raw.replace(" ", "").split(","):
        if part.isdigit() and 0 < int(part) <= len(cams):
            picked.append(cams[int(part) - 1])
    return picked


def main() -> int:
    from cloudcam import viewer

    cam = CloudCam()                       # config.py / CLOUDCAM_* dan
    cam.login()                            # saqlangan sessiya bo'lsa login QILINMAYDI
    print(t(f"Session: {cam.auth_mode}", f"Sessiya: {cam.auth_mode}"))
    cams = cam.cameras()
    print(t("Checking which cameras are live...",
            "Qaysi kameralar ishlayotgani tekshirilmoqda..."))
    try:
        states = cam.check(cams)
    except Exception as e:  # noqa: BLE001 — tekshiruv ixtiyoriy, ro'yxat baribir chiqadi
        print(t(f"Check failed: {e}", f"Tekshirib bo'lmadi: {e}"))
        states = None
    chosen = choose_cameras(cams, states)
    if not chosen:
        print(t("No camera selected.", "Hech qaysi kamera tanlanmadi."))
        cam.close()
        return 1
    return viewer.run(cam, cameras=chosen)


if __name__ == "__main__":
    raise SystemExit(main())
