"""
Bitta kamerani 24/7 uzilishsiz ko'rish.

Stream uzilsa avtomatik qayta ulanadi, token avtomatik yangilanadi.

Ishga tushirish:
    python single.py GK8973616
    python single.py GK8973616 --decrypt
"""

import sys
import time

import cv2

from cloudcam.client import CloudClient
from cloudcam.stream_manager import StreamManager
import config


def main():
    if len(sys.argv) < 2:
        print("Foydalanish: python single.py <SERIAL> [--decrypt]")
        return
    serial = sys.argv[1]
    decrypt = "--decrypt" in sys.argv

    print("🔐 Login...")
    client = CloudClient(config.EMAIL, config.PASSWORD, config.REGION,
                         platform=getattr(config, "PLATFORM", "hikconnect"))
    client.login()
    client.save_token(config.TOKEN_FILE)
    print("✅ Login!\n")

    manager = StreamManager(client=client)
    manager.start_token_refresh(interval=3600)
    manager.add(serial, channel=1, decrypt=decrypt, width=1280, height=720)

    print("🎥 Stream ochilmoqda... (chiqish: 'q')")
    window = f"EZVIZ: {serial}"

    try:
        while True:
            frame = manager.streams[serial].get_frame()
            if frame is not None:
                st = manager.streams[serial].status()
                color = (0, 255, 0) if st["connected"] else (0, 0, 255)
                cv2.rectangle(frame, (0, 0), (frame.shape[1], 30), (0, 0, 0), -1)
                cv2.putText(frame, f"{serial}  {st['fps']}fps  "
                                   f"reconnects:{st['reconnects']}",
                            (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1)
                cv2.imshow(window, frame)
            if cv2.waitKey(30) & 0xFF == ord("q"):
                break
    finally:
        manager.stop_all()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
