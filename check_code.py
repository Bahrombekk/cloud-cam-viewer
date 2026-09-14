"""
Shifrlangan kamera "verification code" (shifr paroli) ni tekshirish.

Kamera oqimidan birinchi keyframe (VPS) ni olib, kod to'g'ri/noto'g'ri yoki
kamera umuman shifrlanmaganligini aniqlaydi. To'g'ri bo'lsa cam_keys.json ga saqlaydi.

Ishga tushirish:
    python check_code.py <SERIAL> <KOD> [KANAL]
    python check_code.py <SERIAL>             # kodsiz: toza/shifrli ekanini aniqlaydi
    python check_code.py <SERIAL> <KOD> all   # NVR ning hamma (1-4) kanalini tekshiradi
"""

import json
import logging
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(level=logging.CRITICAL)

import config
from cloudcam import decrypt_proxy
from cloudcam.decrypt_proxy import (
    HEVC_VIDEO_PT, H264RtpDecryptor, HevcRtpDecryptor, PsStreamDecryptor,
    detect_rtp_codec,
)
from pyezvizapi.stream import rtp_payload
from pyezvizapi.cloud_stream import open_cloud_stream


def check(client, serial, code, channel=1, timeout=30):
    """Qaytaradi: 'correct' | 'wrong' | 'clear' | 'timeout' | 'nostream'."""
    key = code or "AUTO"
    dec = None
    is_rtp = None
    pending = []          # codec aniqlanmaguncha paketlar shu yerda kutadi
    t0 = time.time()
    try:
        with open_cloud_stream(client, serial, channel=channel,
                               client_type=9, refresh_vtm=False, timeout=20.0) as s:
            s.start()
            for pkt in s.iter_packets(max_packets=5000):
                b = bytes(pkt.body)
                if is_rtp is None:
                    is_rtp = len(b) >= 1 and (b[0] >> 6) == 2
                if dec is None:
                    if not is_rtp:
                        dec = PsStreamDecryptor(key)   # PS o'zi H.264/HEVC ni aniqlaydi
                    else:
                        # RTP: codec'ni ANIQLAYMIZ. Ilgari bu yerda doim HEVC
                        # dekodlovchi yaratilardi — H.264 kamerada esa param-set
                        # hech qachon topilmay, kod TO'G'RI bo'lsa ham "timeout"
                        # chiqardi.
                        pending.append(b)
                        if (b[1] & 0x7F) == HEVC_VIDEO_PT:
                            pl = rtp_payload(b)
                            codec = detect_rtp_codec(pl[0]) if len(pl) >= 1 else None
                            if codec:
                                dec = (H264RtpDecryptor(key) if codec == "h264"
                                       else HevcRtpDecryptor(key))
                        if dec is None:
                            if len(pending) > 400 or time.time() - t0 > timeout:
                                return "timeout"
                            continue
                        for old in pending:            # kutgan paketlarni ham beramiz
                            dec.feed(old)
                        pending = []
                        if dec.encrypted is not None or dec.key_error:
                            break
                        continue
                dec.feed(b)
                if dec.encrypted is not None or dec.key_error:
                    break
                if time.time() - t0 > timeout:
                    return "timeout"
    except Exception:
        return "nostream"
    if dec is None:
        return "nostream"
    if dec.key_error:
        return "wrong"
    if dec.encrypted is False:
        return "clear"
    if dec.encrypted is True:
        return "correct"
    return "timeout"


def save_code(serial, code):
    keys = {}
    if os.path.exists(config.CAMKEY_FILE):
        try:
            with open(config.CAMKEY_FILE) as f:
                keys = json.load(f)
        except Exception:
            pass
    keys[serial] = code
    with open(config.CAMKEY_FILE, "w") as f:
        json.dump(keys, f, indent=2)


def report(serial, code, channel, result):
    tag = f"{serial} ch{channel}"
    if result == "correct":
        print(f"✅ {tag}: KOD TO'G'RI! Shifr ochildi.")
        save_code(serial, code)
        print(f"   💾 cam_keys.json ga saqlandi: {serial} -> {code}")
    elif result == "clear":
        print(f"⚠️  {tag}: kamera SHIFRLANMAGAN — kod kerak emas (kodsiz ishlaydi).")
    elif result == "wrong":
        print(f"❌ {tag}: KOD NOTO'G'RI! Boshqa kodni sinab ko'ring.")
    elif result == "timeout":
        print(f"⏱  {tag}: keyframe kelmadi (sekin/offline) — qayta urining.")
    else:
        print(f"❌ {tag}: oqim yo'q (kamera offline yoki kanal bo'sh).")


def main():
    if len(sys.argv) < 2:
        print("Foydalanish: python check_code.py <SERIAL> [KOD] [KANAL|all]")
        return
    serial = sys.argv[1]
    code = sys.argv[2] if len(sys.argv) > 2 else "AUTO"
    ch_arg = sys.argv[3] if len(sys.argv) > 3 else "1"
    channels = [1, 2, 3, 4] if ch_arg == "all" else [int(ch_arg)]

    client = decrypt_proxy._make_client()
    for ch in channels:
        print(f"🔎 Tekshirilmoqda: {serial} ch{ch} (kod: {code}) ...")
        report(serial, code, ch, check(client, serial, code, ch))


if __name__ == "__main__":
    main()
