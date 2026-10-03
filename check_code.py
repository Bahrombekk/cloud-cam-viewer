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
from cloudcam.decrypt_proxy import (HEVC_VIDEO_PT, H264RtpDecryptor, HevcRtpDecryptor,
                                    PsStreamDecryptor, detect_rtp_codec)
from pyezvizapi.cloud_stream import open_cloud_stream
from pyezvizapi.stream import rtp_payload

_CODEC_SCAN = 400   # proxy bilan bir xil: aniq marker kelguncha shuncha paket


def _rtp_codec(b: bytes):
    """RTP video paketidan codec ('h264' | 'hevc') yoki None (noaniq)."""
    if len(b) < 2 or (b[1] & 0x7F) != HEVC_VIDEO_PT:
        return None
    pl = rtp_payload(b)
    return detect_rtp_codec(pl[0]) if len(pl) >= 1 else None


def check(client, serial, code, channel=1, timeout=30):
    """Qaytaradi: 'correct' | 'wrong' | 'clear' | 'timeout' | 'nostream'."""
    key = code or "AUTO"
    dec = None
    buffered = []   # RTP: codec aniqlanguncha paketlar (keyin dekoderga beriladi)
    t0 = time.time()
    try:
        with open_cloud_stream(client, serial, channel=channel,
                               client_type=9, refresh_vtm=False, timeout=20.0) as s:
            s.start()
            for pkt in s.iter_packets(max_packets=5000):
                b = bytes(pkt.body)
                if dec is None:
                    is_rtp = len(b) >= 1 and (b[0] >> 6) == 2
                    if not is_rtp:
                        dec = PsStreamDecryptor(key)
                    else:
                        # H.264 ni HEVC dekoderiga bersak to'g'ri kod ham "xato"
                        # chiqardi — proxy kabi avval codec'ni aniqlaymiz.
                        buffered.append(b)
                        codec = _rtp_codec(b)
                        if codec is None and len(buffered) < _CODEC_SCAN:
                            if time.time() - t0 > timeout:
                                return "timeout"
                            continue
                        dec = (H264RtpDecryptor(key) if codec == "h264"
                               else HevcRtpDecryptor(key))
                if buffered:
                    for x in buffered:
                        dec.feed(x)
                    buffered = []
                else:
                    dec.feed(b)
                if dec._decrypt is not None or dec.key_error:
                    break
                if time.time() - t0 > timeout:
                    return "timeout"
    except Exception:
        return "nostream"
    if dec is None:
        return "nostream"
    if dec.key_error:
        return "wrong"
    if dec._decrypt is False:
        return "clear"
    if dec._decrypt is True:
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
    code = sys.argv[2].strip() if len(sys.argv) > 2 else "AUTO"
    ch_arg = sys.argv[3] if len(sys.argv) > 3 else "1"
    channels = [1, 2, 3, 4] if ch_arg == "all" else [int(ch_arg)]

    client = decrypt_proxy._make_client()
    for ch in channels:
        print(f"🔎 Tekshirilmoqda: {serial} ch{ch} (kod: {code}) ...")
        used, result = check_with_case_fallback(client, serial, code, ch)
        report(serial, used, ch, result)


def check_with_case_fallback(client, serial, code, channel=1):
    """Kod AES kalit — registrga SEZGIR. Yorliqdagi kod katta harfli, lekin
    foydalanuvchi kichik harf bilan yozishi mumkin; ilovada o'zgartirilgan kod
    esa kichik harfli bo'lishi ham mumkin. Shuning uchun avval AYNAN berilgani,
    xato bo'lsa katta harflisi sinaladi. Qaytaradi: (ishlatilgan_kod, natija)."""
    result = check(client, serial, code, channel)
    upper = code.upper()
    if result == "wrong" and upper != code:
        print(f"   ↻ katta harf bilan qayta: {upper}")
        r2 = check(client, serial, upper, channel)
        if r2 == "correct":
            return upper, r2
    return code, result


if __name__ == "__main__":
    main()
