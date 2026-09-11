"""OpenCV grid ko'ruvchi — yuqori darajali API ustida (ixtiyoriy `viewer` deps).

Yadro (cloudcam) buni import qilmaydi; faqat `cloudcam view` chaqirsa yuklanadi.
Boshqaruv: q=chiqish · g=grid · 1-9=to'liq ekran · s=holat.
"""
from __future__ import annotations

import math
import time

import cv2
import numpy as np

from .settings import get_active


def _grid(frames, labels, statuses):
    n = len(frames)
    if n == 0:
        return None
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    s = get_active()
    h, w = s.display_height, s.display_width
    blank = np.zeros((h, w, 3), dtype=np.uint8)

    cells = []
    for i in range(rows * cols):
        cell = frames[i] if (i < n and frames[i] is not None) else blank.copy()
        if i < n and frames[i] is None:
            err = statuses[i].get("error")
            msg, color = (err, (0, 0, 255)) if err else ("Ulanmoqda...", (0, 200, 255))
            cv2.putText(cell, msg[:40], (10, h // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 1)
        if i < n:
            st = statuses[i]
            color = (0, 255, 0) if st["connected"] else (0, 0, 255)
            cv2.rectangle(cell, (0, 0), (w, 28), (0, 0, 0), -1)
            cv2.putText(cell, f"[{i+1}] {labels[i]}", (8, 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
            cv2.putText(cell, f"{st['fps']}fps", (w - 70, 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
        cells.append(cell)

    grid_rows = []
    for r in range(rows):
        row = cells[r * cols:(r + 1) * cols]
        while len(row) < cols:
            row.append(blank.copy())
        grid_rows.append(np.hstack(row))
    return np.vstack(grid_rows)


def run(cam, args=None) -> int:
    """Barcha kameralarni grid'da ko'rsatadi. `cam` — login qilingan CloudCam."""
    cam.start_token_refresh(interval=3600)
    cams = cam.cameras()
    if not cams:
        print("Kamera topilmadi.")
        return 1
    streams = [cam.open(c.serial, channel=c.channel) for c in cams]
    labels = [c.name[:22] for c in cams]
    print(f"{len(streams)} kamera · q=chiqish · g=grid · 1-9=to'liq · s=holat")

    win = "cloudcam"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, 1280, 720)
    full, show_status = None, False
    try:
        while True:
            statuses = [s.status() for s in streams]
            if full is not None and full < len(streams):
                frame = streams[full].frame()
                big = (cv2.resize(frame, (1280, 720)) if frame is not None
                       else np.zeros((720, 1280, 3), dtype=np.uint8))
                st = statuses[full]
                color = (0, 255, 0) if st["connected"] else (0, 0, 255)
                cv2.rectangle(big, (0, 0), (1280, 35), (0, 0, 0), -1)
                cv2.putText(big, f"[{full+1}] {labels[full]}  {st['fps']}fps  "
                                 f"reconnects:{st['reconnects']}  (g=grid)",
                            (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                cv2.imshow(win, big)
            else:
                grid = _grid([s.frame() for s in streams], labels, statuses)
                if grid is not None:
                    cv2.imshow(win, grid)
            if show_status:
                online = sum(1 for s in statuses if s["connected"])
                print(f"\r[{time.strftime('%H:%M:%S')}] online: {online}/{len(streams)}  ",
                      end="", flush=True)
            k = cv2.waitKey(30) & 0xFF
            if k == ord("q"):
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
