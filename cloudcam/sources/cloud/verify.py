"""Tasdiqlash kodini TEKSHIRISH — kamera oqimidan.

Kodni saqlashdan oldin uning to'g'ri ekanini bilish kerak; bulut buni
aytmaydi. Birinchi param-set NAL (VPS/SPS) kelguncha o'qiymiz va dekodlovchi
qaroriga qaraymiz:

    correct   kod to'g'ri — shifr ochildi
    wrong     kod noto'g'ri (param-set topildi, lekin hech bir variant mos emas)
    clear     kamera umuman shifrlanmagan — kod kerak emas
    timeout   keyframe kelmadi (sekin yoki oflayn)
    nostream  oqim yo'q (kamera oflayn yoki kanal bo'sh)

Ilgari bu mantiq `check_code.py` skriptida yashirin edi, ya'ni kutubxonadan
foydalanib bo'lmasdi va proxy bilan takrorlanardi.
"""
from __future__ import annotations

import time

from pyezvizapi.cloud_stream import open_cloud_stream
from pyezvizapi.stream import rtp_payload

from ...codec.nal import HEVC_VIDEO_PT
from ...codec.ps import PsStreamDecryptor
from ...codec.rtp import H264RtpDecryptor, HevcRtpDecryptor, detect_rtp_codec


def check_code(client, serial, code, channel=1, timeout=30):
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


