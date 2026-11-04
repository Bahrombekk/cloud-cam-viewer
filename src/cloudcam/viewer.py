"""OpenCV grid ko'ruvchi — yuqori darajali API ustida (ixtiyoriy `viewer` deps).

Yadro (cloudcam) buni import qilmaydi; faqat `cloudcam view` yoki
`scripts/app.py` chaqirsa yuklanadi.
Boshqaruv: q=chiqish · g=grid · 1-9=to'liq ekran · s=holat.
"""
from __future__ import annotations

import math
import time

import cv2
import numpy as np

from cloudcam.i18n import t

WINDOW = "cloudcam"
DEFAULT_WINDOW = (1280, 720)
FULL_SIZE = (1280, 720)


def _window_size(win: str) -> tuple[int, int]:
    """Oynaning joriy ichki o'lchami (foydalanuvchi o'zgartirgan bo'lishi mumkin)."""
    try:
        _x, _y, w, h = cv2.getWindowImageRect(win)
        if w > 0 and h > 0:
            return w, h
    except cv2.error:
        pass
    return DEFAULT_WINDOW


def _label(cell, text: str, color, *, right: str | None = None) -> None:
    w = cell.shape[1]
    scale = 0.5 if w >= 400 else 0.4
    cv2.rectangle(cell, (0, 0), (w, 24), (0, 0, 0), -1)
    cv2.putText(cell, text, (6, 17), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)
    if right:
        (tw, _), _ = cv2.getTextSize(right, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
        cv2.putText(cell, right, (w - tw - 6, 17), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    color, 1, cv2.LINE_AA)


def _placeholder(size: tuple[int, int], status: dict):
    w, h = size
    cell = np.zeros((h, w, 3), dtype=np.uint8)
    err = status.get("error")
    msg, color = (err, (0, 0, 255)) if err else (t("Connecting...", "Ulanmoqda..."),
                                                 (0, 200, 255))
    cv2.putText(cell, msg[: max(10, w // 9)], (8, h // 2), cv2.FONT_HERSHEY_SIMPLEX,
                0.45, color, 1, cv2.LINE_AA)
    return cell


def make_grid(frames, labels, statuses, size: tuple[int, int] = DEFAULT_WINDOW):
    """Kadrlarni `size` (oyna) ichiga sig'adigan grid'ga yig'adi.

    Ilgari har plitka to'liq `display_width x display_height` (1280x720)
    o'lchamda chizilardi: 23 kamerada ~6400x3600 kadr har 30 ms da yig'ilib,
    keyin oyna 4 barobar kichraytirardi — yozuvlar o'qib bo'lmas, CPU bekorga
    ketardi. Endi plitka o'lchami oynadan hisoblanadi."""
    n = len(frames)
    if n == 0:
        return None
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    tw, th = max(1, size[0] // cols), max(1, size[1] // rows)
    blank = np.zeros((th, tw, 3), dtype=np.uint8)
    cells = []
    for i in range(rows * cols):
        if i >= n:
            cells.append(blank)
            continue
        st = statuses[i]
        f = frames[i]
        cell = (cv2.resize(f, (tw, th), interpolation=cv2.INTER_AREA) if f is not None
                else _placeholder((tw, th), st))
        color = (0, 255, 0) if st["connected"] else (0, 0, 255)
        _label(cell, f"[{i + 1}] {labels[i]}", color, right=f"{st['fps']}fps")
        cells.append(cell)
    return np.vstack([np.hstack(cells[r * cols:(r + 1) * cols]) for r in range(rows)])


def _fullscreen(frame, label: str, st: dict):
    w, h = FULL_SIZE
    big = (cv2.resize(frame, (w, h)) if frame is not None else _placeholder((w, h), st))
    color = (0, 255, 0) if st["connected"] else (0, 0, 255)
    _label(big, f"{label}   reconnects: {st['reconnects']}   (g = grid)", color,
           right=f"{st['fps']}fps")
    return big


def run(cam, args=None, *, cameras=None, rtsp_cameras=(), cloud: bool = True) -> int:
    """Kameralarni grid'da ko'rsatadi. `cam` — CloudCam (bulut uchun login qilingan).

    `cameras` — ko'rsatiladigan [[cloudcam.api.Camera]] ro'yxati (standart: hammasi);
    `rtsp_cameras` — oddiy RTSP kameralar ([[cloudcam.sources.rtsp.store.RtspCamera]]),
    ular bulut kameralari yonida chiqadi. `cloud=False` — faqat RTSP (hisobsiz)."""
    cams = []
    if cloud:
        cam.start_token_refresh(interval=3600)
        cams = list(cameras) if cameras is not None else cam.cameras()
    rtsp_cams = list(rtsp_cameras)
    if not cams and not rtsp_cams:
        print(t("No cameras found.", "Kamera topilmadi."))
        return 1
    streams = [cam.open(c.serial, channel=c.channel) for c in cams]
    labels = [c.name[:22] for c in cams]
    for rc in rtsp_cams:
        # Grid uchun kichik oqim (bo'lsa) — xuddi bulutdagi substream kabi.
        streams.append(cam.open_rtsp(rc.sub_url or rc.url, rc.name))
        labels.append(rc.name[:22])
    print(t(f"{len(streams)} cameras · q=quit · g=grid · 1-9=fullscreen · s=status",
            f"{len(streams)} kamera · q=chiqish · g=grid · 1-9=to'liq · s=holat"))

    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW, *DEFAULT_WINDOW)
    full, show_status = None, False
    try:
        while True:
            statuses = [s.status() for s in streams]
            if full is not None and full < len(streams):
                cv2.imshow(WINDOW, _fullscreen(streams[full].frame(),
                                               f"[{full + 1}] {labels[full]}", statuses[full]))
            else:
                grid = make_grid([s.frame() for s in streams], labels, statuses,
                                 _window_size(WINDOW))
                if grid is not None:
                    cv2.imshow(WINDOW, grid)
            if show_status:
                online = sum(1 for s in statuses if s["connected"])
                print(f"\r[{time.strftime('%H:%M:%S')}] online: {online}/{len(streams)}  ",
                      end="", flush=True)
            k = cv2.waitKey(30) & 0xFF
            if k == ord("q") or cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                break
            elif k == ord("g"):
                full = None
            elif k == ord("s"):
                show_status = not show_status
            elif ord("1") <= k <= ord("9"):
                idx = k - ord("1")
                if idx < len(streams):
                    full = idx
    finally:
        cam.close()
        cv2.destroyAllWindows()
    return 0
