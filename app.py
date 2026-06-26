"""
EZVIZ Multi-Camera Viewer — 24/7 uzilishsiz ishlash uchun.

Imkoniyatlar:
  - Tanlangan kameralarni jonli ko'rish (grid yoki to'liq ekran)
  - 24/7 uzilishsiz — har qanday uzilishda avtomatik qayta ulanadi
  - Token avtomatik yangilanadi — hech qachon qayta MFA/parol so'ramaydi
  - Har kamera holati ko'rinadi (FPS, qayta ulanishlar soni)

Ishga tushirish:
    python app.py

Klaviatura (video oynasida):
    q   - chiqish
    1-9 - shu raqamli kamerani to'liq ekran
    g   - grid ko'rinish
    s   - holat (status) ko'rsatish/yashirish
"""

import math
import time

import cv2
import numpy as np

from cloudcam.client import CloudClient
from cloudcam.stream_manager import StreamManager, load_cam_keys
from cloudcam import decrypt_proxy
import config


def choose_cameras(items):
    """items: [(serial, channel, name)] — faqat kamera ulangan kanallar."""
    print("\n" + "=" * 60)
    print("📷 KAMERALAR RO'YXATI")
    print("=" * 60)
    for i, (serial, ch, name) in enumerate(items, 1):
        print(f"  [{i:2}] 🟢 {name}  ({serial} ch{ch})")
    if not items:
        print("  (kamera topilmadi)")
    print("=" * 60)
    print("Tanlash: raqamlarni vergul bilan (masalan: 1,3,5) yoki 'all'")
    raw = input("➤ Tanlov: ").strip().lower()

    if raw == "all":
        return items
    chosen = []
    for part in raw.replace(" ", "").split(","):
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(items):
                chosen.append(items[idx])
    return chosen


def make_grid(frames, labels, statuses, cols=None):
    n = len(frames)
    if n == 0:
        return None
    if cols is None:
        cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)

    h, w = config.DISPLAY_HEIGHT, config.DISPLAY_WIDTH
    blank = np.zeros((h, w, 3), dtype=np.uint8)

    cells = []
    for i in range(rows * cols):
        if i < n and frames[i] is not None:
            cell = frames[i]
        else:
            cell = blank.copy()
            err = statuses[i].get("error") if i < n else None
            if err:
                cv2.putText(cell, "XATO!", (w // 2 - 50, h // 2 - 15),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
                cv2.putText(cell, err, (10, h // 2 + 20),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1)
            elif i < n:
                cv2.putText(cell, "Ulanmoqda...", (w // 2 - 80, h // 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)

        if i < n:
            st = statuses[i]
            color = (0, 255, 0) if st["connected"] else (0, 0, 255)
            dot = "●" if st["connected"] else "○"
            cv2.rectangle(cell, (0, 0), (w, 28), (0, 0, 0), -1)
            cv2.putText(cell, f"[{i+1}] {labels[i]}", (8, 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
            cv2.putText(cell, f"{st['fps']}fps", (w - 70, 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
        cells.append(cell)

    grid_rows = []
    for r in range(rows):
        row_cells = cells[r * cols:(r + 1) * cols]
        while len(row_cells) < cols:
            row_cells.append(blank.copy())
        grid_rows.append(np.hstack(row_cells))
    return np.vstack(grid_rows)


def main():
    print("🔐 Login qilinmoqda...")
    client = CloudClient(config.EMAIL, config.PASSWORD, config.REGION,
                         platform=getattr(config, "PLATFORM", "hikconnect"))
    client.login()
    client.save_token(config.TOKEN_FILE)
    print("✅ Login muvaffaqiyatli!")

    # Faqat haqiqiy kamera ulangan kanallarni ko'rsatamiz (bo'sh NVR slotlari emas)
    print("📡 Kameralar aniqlanmoqda...")
    cams = decrypt_proxy.list_cameras()
    chosen = choose_cameras(cams)
    if not chosen:
        print("❌ Hech qaysi kamera tanlanmadi")
        return

    print(f"\n🚀 {len(chosen)} ta kamera ishga tushirilmoqda...")

    # StreamManager — client bilan (token refresh uchun)
    manager = StreamManager(client=client)
    manager.start_token_refresh(interval=3600)  # har soatda token yangilash

    # Shifrlangan kameralar — get_camkey.py bilan kaliti olinganlar.
    # Kaliti bor kamera = shifrlangan -> proxy --decrypt-video bilan ochiladi.
    cam_keys = load_cam_keys()
    if cam_keys:
        print(f"🔓 Shifrli kameralar (dekod yoqiladi): {list(cam_keys)}")

    labels, streams = [], []
    for serial, ch, name in chosen:
        # Hik-Connect kameralari hammasi RTP/HEVC yoki MPEG-PS -> har doim decrypt_proxy.
        # Kod bo'lmasa "AUTO": toza oqim avtomatik o'tadi; shifrli bo'lsa XATO beradi.
        key = cam_keys.get(serial) or "AUTO"
        st = manager.add(serial, channel=ch, decrypt=True, key=key)
        streams.append(st)
        labels.append(name[:22])

    print("⏳ Streamlar ulanmoqda...")
    print("\n📺 Boshqaruv: q=chiqish | 1-9=to'liq ekran | g=grid | s=holat\n")

    window = "EZVIZ 24/7"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, 1280, 720)

    fullscreen_idx = None
    show_status = False

    try:
        while True:
            statuses = [s.status() for s in streams]

            if fullscreen_idx is not None and fullscreen_idx < len(streams):
                frame = streams[fullscreen_idx].get_frame()
                if frame is not None:
                    big = cv2.resize(frame, (1280, 720))
                else:
                    big = np.zeros((720, 1280, 3), dtype=np.uint8)
                    err = statuses[fullscreen_idx].get("error")
                    if err:
                        cv2.putText(big, "XATO!", (560, 330),
                                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)
                        cv2.putText(big, err, (200, 380),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
                    else:
                        cv2.putText(big, "Ulanmoqda...", (560, 360),
                                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 200, 255), 2)
                st = statuses[fullscreen_idx]
                color = (0, 255, 0) if st["connected"] else (0, 0, 255)
                cv2.rectangle(big, (0, 0), (1280, 35), (0, 0, 0), -1)
                cv2.putText(big, f"[{fullscreen_idx+1}] {labels[fullscreen_idx]}  "
                                 f"{st['fps']}fps  reconnects:{st['reconnects']}  (g=grid)",
                            (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                cv2.imshow(window, big)
            else:
                frames = [s.get_frame() for s in streams]
                grid = make_grid(frames, labels, statuses)
                if grid is not None:
                    cv2.imshow(window, grid)

            if show_status:
                online = sum(1 for s in statuses if s["connected"])
                print(f"\r[{time.strftime('%H:%M:%S')}] "
                      f"Online: {online}/{len(streams)}  ", end="", flush=True)

            key = cv2.waitKey(30) & 0xFF
            if key == ord("q"):
                break
            elif key == ord("g"):
                fullscreen_idx = None
            elif key == ord("s"):
                show_status = not show_status
            elif ord("1") <= key <= ord("9"):
                idx = key - ord("1")
                if idx < len(streams):
                    fullscreen_idx = idx
    finally:
        print("\n🛑 To'xtatilmoqda...")
        manager.stop_all()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
