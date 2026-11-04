"""Tasdiqlash kodini JONLI oqimda tekshirish (ffmpeg'siz).

Kameradan birinchi param-set (SPS/VPS) gacha o'qib, kod to'g'ri/noto'g'ri yoki
kamera umuman shifrlanmaganligini aniqlaydi. Ilgari bu faqat
`scripts/check_code.py` ichida edi va `config.py` siz import qilib bo'lmasdi;
endi `cloudcam code check` ham, kutubxona ham shuni ishlatadi.
"""

from __future__ import annotations

import time

from cloudcam.codec.nal import HEVC_VIDEO_PT
from cloudcam.codec.ps import PsStreamDecryptor
from cloudcam.codec.rtp import H264RtpDecryptor, HevcRtpDecryptor, detect_rtp_codec
from cloudcam.sources.cloud.stream import open_cloud_stream
from cloudcam.sources.cloud.vtm import rtp_payload

CORRECT, WRONG, CLEAR, TIMEOUT, NOSTREAM = "correct", "wrong", "clear", "timeout", "nostream"


def check(client, serial: str, code: str | None, channel: int = 1,
          timeout: float = 30.0) -> str:
    """`correct` | `wrong` | `clear` | `timeout` | `nostream`.

    RTP'da codec ANIQLANADI: ilgari doim HEVC dekoderi yaratilardi va H.264
    kamerada param-set hech qachon topilmay, kod TO'G'RI bo'lsa ham `timeout`
    chiqardi."""
    key = code or "AUTO"
    dec = None
    is_rtp = None
    pending = []          # codec aniqlanmaguncha paketlar shu yerda kutadi
    t0 = time.time()
    try:
        with open_cloud_stream(client, serial, channel=channel, timeout=20.0) as s:
            s.start()
            for pkt in s.iter_packets(max_packets=5000):
                b = bytes(pkt.body)
                if is_rtp is None:
                    is_rtp = len(b) >= 1 and (b[0] >> 6) == 2
                if dec is None:
                    if not is_rtp:
                        dec = PsStreamDecryptor(key)   # PS o'zi H.264/HEVC ni aniqlaydi
                    else:
                        pending.append(b)
                        if len(b) > 1 and (b[1] & 0x7F) == HEVC_VIDEO_PT:
                            pl = rtp_payload(b)
                            codec = detect_rtp_codec(pl[0]) if len(pl) >= 1 else None
                            if codec:
                                dec = (H264RtpDecryptor(key) if codec == "h264"
                                       else HevcRtpDecryptor(key))
                        if dec is None:
                            if len(pending) > 400 or time.time() - t0 > timeout:
                                return TIMEOUT
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
                    return TIMEOUT
    except Exception:  # noqa: BLE001 — oqim ochilmadi: sababi muhim emas, natija bir
        return NOSTREAM
    if dec is None:
        return NOSTREAM
    if dec.key_error:
        return WRONG
    if dec.encrypted is False:
        return CLEAR
    if dec.encrypted is True:
        return CORRECT
    return TIMEOUT


def check_with_case_fallback(client, serial: str, code: str, channel: int = 1,
                             *, on_retry=None) -> tuple[str, str]:
    """(ishlatilgan_kod, natija).

    Kod AES kalit — registrga SEZGIR. Yorliqdagi kod katta harfli, lekin
    foydalanuvchi kichik harf bilan yozishi mumkin; ilovada o'zgartirilgan kod
    esa kichik harfli bo'lishi ham mumkin. Shuning uchun avval AYNAN berilgani,
    xato bo'lsa katta harflisi sinaladi. `on_retry(upper)` — xabar uchun."""
    result = check(client, serial, code, channel)
    upper = code.upper()
    if result == WRONG and upper != code:
        if on_retry:
            on_retry(upper)
        r2 = check(client, serial, upper, channel)
        if r2 == CORRECT:
            return upper, r2
    return code, result
