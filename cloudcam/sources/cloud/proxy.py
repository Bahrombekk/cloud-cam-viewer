"""Bitta kamera uchun lokal dekodlovchi HTTP proxy (alohida JARAYON).

    bulut (VTM) -> depaketlash -> AES deshifr -> Annex-B -> HTTP -> ffmpeg

`stream_manager` buni har shifrlangan kamera uchun ishga tushiradi. Ichki
ffmpeg YO'Q: chiqish [[media.paced.PacedWriter]] orqali tekislanadi, codec'ni
tashqi ffmpeg o'zi aniqlaydi.

Ishga tushirish:
    python -m cloudcam.sources.cloud.proxy <SERIAL> <PORT> <CODE> [CHANNEL] [--stream=auto]
"""
from __future__ import annotations

import sys
from itertools import chain
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pyezvizapi.cloud_stream import open_cloud_stream
from pyezvizapi.stream import rtp_payload

from ...codec.nal import HEVC_VIDEO_PT
from ...codec.ps import PsStreamDecryptor
from ...codec.rtp import H264RtpDecryptor, HevcRtpDecryptor, detect_rtp_codec
from ...core import ipc
from ...media.paced import PacedWriter
from . import vtm_cache
from .compat import install_patches, set_substream
from .devices import make_client

_CACHE_KEY = install_patches()

STREAM_PROBE_TIMEOUT = 6.0     # "auto" rejimda substream shuncha kutiladi

def _is_missing_resource(exc) -> bool:
    """Qurilma bulut ro'yxatida YO'Q (oflayn kamera ro'yxatdan tushadi).

    Bu ESKIRGAN metama'lumot EMAS — pagelist muvaffaqiyatli olingan, qurilmaning
    o'zi yo'q. Shuning uchun bu holatda VTM keshini bekor qilish zarar: kesh
    hisob bo'yicha, ya'ni bitta oflayn kamera butun hisobning keshini o'chirardi
    (bizda 151 kameradan 31 tasi oflayn edi — bu doim ishlab turardi)."""
    return "could not find vtm resource" in str(exc or "").lower()



def serve(serial: str, port: int, key: str, channel: int = 1,
          substream: bool = False, stream_mode: str | None = None):
    """Bitta kamera uchun lokal dekodlovchi HTTP proxy.

    `stream_mode`: "main" | "sub" | "auto". "auto" — avval KICHIK oqim
    (substream) so'raladi, u [[STREAM_PROBE_TIMEOUT]] ichida kadr bermasa
    asosiy oqimga qaytiladi.

    Nega "auto" kerak: substream trafikni ~36 barobar kamaytiradi
    ([[set_substream]]), lekin hamma kamerada ham yo'q (batareyali modellar,
    ba'zi NVR kanallari). Qattiq `--stream=sub` bunday kamerada qora ekran
    beradi; qattiq "main" esa grid'da bekorga 1440p tortadi. `hikcloudstream`
    ham shu yo'ldan boradi: substream'ni sinab ko'rib, kerak bo'lsa asosiysiga
    qaytadi."""
    client = make_client()
    mode = stream_mode or ("sub" if substream else "main")
    if mode not in ("main", "sub", "auto"):
        mode = "main"
    # Inter-shifr qarori shu KAMERA bo'yicha keshlanadi ([[_enc_cache_load]]).
    ck = f"{serial}-{channel}"

    # Ota-jarayonga hodisa yuborish ([[core.ipc]]).
    def _key_error():
        ipc.raise_flag(ipc.KEY_ERROR, serial, channel)

    def _offline():
        ipc.raise_flag(ipc.OFFLINE, serial, channel)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            # Xom oqimni dekodlab to'g'ridan-to'g'ri uzatamiz (ichki ffmpeg yo'q):
            #   RTP/PS -> Annex-B (H.264/HEVC), MPEG-TS -> o'zicha.
            # Tashqi ffmpeg (stream_manager) codec'ni auto-detect qilib, bardoshli dekodlaydi.
            # Bulut resurslarida shu KANAL bormi? NVR online bo'lsa ham, unda
            # yo'q kanal hech qachon oqim bermaydi — bunda 20s kutib o'tirmaymiz
            # va "offline" emas, ANIQ sabab qaytaramiz ([[vtm_cache.channel_missing]]).
            if vtm_cache.channel_missing(_CACHE_KEY, serial, channel):
                _offline()
                self.send_error(404, "channel not in cloud device list")
                return
            got_packets = False          # kesh FAQAT paket kelmasa bekor qilinadi

            def _prime(pkts):
                """Format (RTP/PS/TS) va codec (H.264/HEVC) ni aniqlash uchun
                boshlang'ich paketlarni o'qiydi (keyin ular ham uzatiladi)."""
                buffered, is_rtp, codec = [], None, None
                for pkt in pkts:
                    b = bytes(pkt.body)
                    buffered.append(b)
                    if is_rtp is None:
                        is_rtp = len(b) >= 1 and (b[0] >> 6) == 2
                    if not is_rtp:
                        break  # MPEG-PS yoki TS
                    if (b[1] & 0x7F) == HEVC_VIDEO_PT:
                        pl = rtp_payload(b)
                        if len(pl) >= 1:
                            codec = detect_rtp_codec(pl[0])
                            if codec:
                                break
                    # keyframe/aniq marker kelguncha skanerlaymiz (uzun GOP uchun)
                    if len(buffered) >= 400:
                        break
                return buffered, is_rtp, codec

            def _fail_open(e):
                """Oqim umuman ochilmadi — sababni bayroqlab, 502 qaytaramiz.

                Oflayn qurilma keshni O'CHIRMAYDI — sababi
                [[_is_missing_resource]] da: kesh HISOB bo'yicha, ya'ni bitta
                oflayn kamera butun hisobning metama'lumotini bekor qilardi."""
                if "offline" in str(e).lower() or "unreachable" in str(e).lower():
                    _offline()
                elif _is_missing_resource(e):
                    _offline()
                else:
                    vtm_cache.invalidate(_CACHE_KEY)   # metama'lumot eskirgan bo'lishi mumkin
                self.send_error(502)

            # "auto": avval KICHIK oqim, kadr kelmasa asosiysi ([[serve]]).
            # Har urinish uchun `set_substream` qayta chaqiriladi, chunki u
            # kutubxonaning URL quruvchisini almashtiradi.
            attempts = [("sub", STREAM_PROBE_TIMEOUT), ("main", 20.0)] if mode == "auto" \
                else [(mode, 20.0)]
            opened = None
            for want, tmo in attempts:
                # Bu urinish PROBE mi (ya'ni muvaffaqiyatsizlikda keyingisi bor)
                is_probe = want == "sub" and mode == "auto"
                set_substream(want == "sub")
                try:
                    cm = open_cloud_stream(client, serial, channel=channel,
                                           client_type=9, refresh_vtm=False, timeout=tmo)
                except Exception as e:
                    if is_probe:
                        continue                      # substream yo'q — asosiysini sinaymiz
                    _fail_open(e)
                    return
                try:
                    stream = cm.__enter__()
                    stream.start()
                    pkts = stream.iter_packets()
                    primed = _prime(pkts)
                except Exception as e:
                    try:
                        cm.__exit__(None, None, None)
                    except Exception:
                        pass
                    if is_probe:
                        continue                      # soket jim qoldi — asosiysiga
                    # Bu yo'l tashqi `except` ga YETMAYDI (u quyiroqda
                    # boshlanadi), shuning uchun shu yerda hal qilamiz.
                    _fail_open(e)
                    return
                if primed[0] or not is_probe:
                    opened = (cm, stream, pkts, primed)
                    break
                cm.__exit__(None, None, None)         # substream bo'sh — keyingi urinish

            if opened is None:
                self.send_error(502)
                return
            stream_cm, _stream, pkts, (buffered, is_rtp, codec) = opened
            try:
                # `stream_cm` YUQORIDA ochilgan (`__enter__`) — probe uchun
                # paketlarni o'qish kerak edi; shuning uchun bu yerda `with`
                # emas, `finally` da yopamiz.
                try:
                    if not buffered:
                        self.send_error(502)
                        return

                    annexb = True                            # Annex-B chiqadimi (pacing uchun)
                    if not is_rtp and buffered[0][:1] == b"\x47":
                        transform = lambda b: b              # MPEG-TS (shifrsiz)
                        keyerr = lambda: False
                        annexb = False
                    elif not is_rtp:
                        dec = PsStreamDecryptor(key, cache_key=ck)  # MPEG-PS (H.264/HEVC avto)
                        transform = dec.feed; keyerr = lambda: dec.key_error
                    elif codec == "h264":
                        dec = H264RtpDecryptor(key, cache_key=ck)   # RTP/H.264
                        transform = dec.feed; keyerr = lambda: dec.key_error
                    else:
                        dec = HevcRtpDecryptor(key, cache_key=ck)   # RTP/HEVC
                        transform = dec.feed; keyerr = lambda: dec.key_error

                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()

                    # Yozuv ALOHIDA thread'da va O'LCHOVLI ([[PacedWriter]]) —
                    # bulut o'quvchisi hech qachon bloklanmaydi va 2 soniyalik
                    # burstlar tekis oqimga yoyiladi.
                    writer = PacedWriter(self.wfile, codec, paced=annexb).start()
                    try:
                        for body in chain(buffered, (bytes(p.body) for p in pkts)):
                            data = transform(body)
                            if keyerr():
                                _key_error()
                                break
                            if data:
                                got_packets = True
                                writer.feed(data)
                            if writer.failed:
                                break          # mijoz uzildi
                    finally:
                        writer.close()
                finally:
                    stream_cm.__exit__(None, None, None)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            except Exception as e:
                if "offline" in str(e).lower() or "unreachable" in str(e).lower():
                    _offline()
                elif not got_packets and not _is_missing_resource(e):
                    # Bitta ham paket kelmadi -> keshdagi metama'lumot (VTDU
                    # tokeni) eskirgan bo'lishi mumkin. Paket kelgan bo'lsa
                    # kesh TO'G'RI edi — uzilish boshqa sababdan, tegmaymiz.
                    vtm_cache.invalidate(_CACHE_KEY)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        server.server_close()




if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Foydalanish: python -m cloudcam.sources.cloud.proxy "
              "<SERIAL> <PORT> <CODE> [CHANNEL] [--stream=main|sub|auto]")
        sys.exit(1)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--stream=")]
    mode = flags[-1].split("=", 1)[1] if flags else None
    if mode is None and "--substream" in sys.argv:
        mode = "sub"                    # eski bayroq — orqaga moslik
    serve(args[0], int(args[1]), args[2],
          int(args[3]) if len(args) > 3 else 1,
          stream_mode=mode)
