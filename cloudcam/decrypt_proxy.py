"""
Shifrlangan EZVIZ HEVC kameralar uchun dekodlovchi proxy.

Bu kameralar videoni RTP/HEVC (H.265) tarzida uzatadi va NAL body ni
AES-ECB bilan shifrlaydi. Kalit = kamera "Tasdiqlash Kodu" (verification code),
16 baytgacha nol bilan to'ldirilgan. Har NAL body ning birinchi 4096 baytigacha
(to'liq 16-baytli bloklar) shifrlangan.

pyezvizapi ning standart "stream proxy" buni ocha olmaydi (u MPEG-PS kutadi).
Bu yerda biz o'zimiz:
  1) VTM dan RTP paketlarni o'qiymiz
  2) RTP/HEVC ni depaketlaymiz (VPS/SPS/PPS + FU/AP) -> Annex-B
  3) Har NAL body ni verification code bilan dekodlaymiz
  4) ichki ffmpeg orqali MPEG-TS qilib HTTP da uzatamiz (stream_manager o'qiydi)

Ishga tushirish (odatda app.py/stream_manager chaqiradi):
    python decrypt_proxy.py <SERIAL> <PORT> <CODE> [CHANNEL]
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from itertools import chain
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from Crypto.Cipher import AES

import config
import requests as _rq
import pyezvizapi.cloud_stream as _cs
from pyezvizapi.client import EzvizClient
from pyezvizapi.cloud_stream import open_cloud_stream
from pyezvizapi.stream import rtp_payload

# Platformaga qarab klient turi (pagelist to'liq natija qaytarishi uchun muhim)
_CLIENT_TYPE = "55" if getattr(config, "PLATFORM", "hikconnect") == "hikconnect" else "1"


def _paged_vtm_page_list(client):
    """VTM pagelist'ni TO'LIQ (barcha sahifalar) yig'adi.
    Kutubxona faqat 1-sahifani oladi va clientType si boshqacha -> ko'p NVR
    hisobida kameralar topilmaydi. Shuning uchun to'g'ri header bilan xom so'rov."""
    tok = getattr(client, "_token", {}) or {}
    api = tok.get("api_url")
    sess = _rq.Session()
    sess.headers.update({
        "clientType": _CLIENT_TYPE, "lang": "en-US",
        "featureCode": "1fc28fa018178a1cd1c091b13b2f9f02",
        "sessionId": str(tok.get("session_id")),
    })
    base = None
    merged_res, merged_vtm = [], {}
    offset = 0
    for _ in range(40):
        r = sess.get(f"https://{api}/v3/userdevices/v1/resources/pagelist",
                     params={"filter": "VTM", "groupId": -1, "limit": 50, "offset": offset},
                     timeout=25)
        pl = r.json()
        if base is None:
            base = pl
        res = pl.get("resourceInfos") or []
        merged_res.extend(res)
        vtm = pl.get("VTM")
        if isinstance(vtm, dict):
            merged_vtm.update(vtm)
        page = pl.get("page") or {}
        if not res or not page.get("hasNext"):
            break
        offset += len(res)
    if base is not None:
        base["resourceInfos"] = merged_res
        base["VTM"] = merged_vtm
    return base


# kutubxonaning bir-sahifali funksiyasini to'liq sahifalovchi bilan almashtiramiz
_cs.get_vtm_page_list = _paged_vtm_page_list

HEVC_VIDEO_PT = 96                  # RTP payload type — video
NAL_ENCRYPTED_PREFIX = 4096         # har NAL ning birinchi shu qadar bayti shifrlangan
START = b"\x00\x00\x00\x01"


def keyerror_flag_path(serial: str, channel: int = 1) -> str:
    """Kod xato bo'lganda stream_manager bilan aloqa uchun bayroq fayl yo'li."""
    return os.path.join(tempfile.gettempdir(), f"ezviz_keyerr_{serial}_{channel}.flag")


def offline_flag_path(serial: str, channel: int = 1) -> str:
    """Kamera offline (VTM ma'lumot bermayapti) bo'lganda bayroq fayl yo'li."""
    return os.path.join(tempfile.gettempdir(), f"ezviz_offline_{serial}_{channel}.flag")


class _NalDecryptBase:
    """HEVC NAL body ni AES-ECB bilan dekodlash + VPS orqali shifrni avtomatik aniqlash."""

    def __init__(self, key: str):
        self.key = key.encode().ljust(16, b"\0")[:16]
        self._decrypt = None   # None=noma'lum, True=shifrli, False=toza
        self.key_error = False
        self.codec = None      # 'h264' | 'hevc' | None

    def _decrypt_body(self, body) -> bytes:
        if not self._decrypt:  # None yoki False -> dekod qilmaymiz
            return bytes(body)
        n = (min(len(body), NAL_ENCRYPTED_PREFIX) // 16) * 16
        if n == 0:
            return bytes(body)
        head = AES.new(self.key, AES.MODE_ECB).decrypt(bytes(body[:n]))
        return head + bytes(body[n:])

    def _detect(self, nal_type: int, body) -> None:
        """VPS (tur 32) orqali: toza HEVC VPS body 0x0c bilan boshlanadi; shifrli=tasodifiy."""
        if self._decrypt is not None or nal_type != 32 or len(body) < 1:
            return
        if body[0] == 0x0C:                 # toza — qisqa VPS uchun ham
            self._decrypt = False
            return
        if len(body) < 16:                  # AES tekshiruvi uchun 1 blok kerak
            return
        dec0 = AES.new(self.key, AES.MODE_ECB).decrypt(bytes(body[:16]))[0]
        if dec0 == 0x0C:
            self._decrypt = True            # dekod kerak, kod to'g'ri
        else:
            self.key_error = True           # na toza na to'g'ri dekod -> kod xato

    def _emit(self, out: bytearray, nal) -> None:
        if len(nal) >= 2:
            self._detect((nal[0] >> 1) & 0x3F, nal[2:])
            if self._decrypt is None:
                return  # shifr aniqlanmaguncha (VPS'gacha) garbage chiqarmaymiz
            out += START + bytes(nal[:2]) + self._decrypt_body(nal[2:])


class HevcRtpDecryptor(_NalDecryptBase):
    """RTP/HEVC paketlarini Annex-B ga aylantirib, NAL body ni dekodlaydi."""

    def __init__(self, key: str):
        super().__init__(key)
        self._cur = None       # joriy FU (fragment) yig'indisi

    def feed(self, rtp_packet: bytes) -> bytes:
        """Bitta RTP paketdan Annex-B bayt qaytaradi (bo'sh bo'lishi mumkin)."""
        if (rtp_packet[1] & 0x7F) != HEVC_VIDEO_PT:
            return b""
        pl = rtp_payload(rtp_packet)
        if len(pl) < 3:
            return b""
        out = bytearray()
        nal_type = (pl[0] >> 1) & 0x3F
        if nal_type == 49:  # FU — bo'lingan NAL
            fu = pl[2]
            start_bit = fu >> 7
            end_bit = (fu >> 6) & 1
            fu_type = fu & 0x3F
            if start_bit:
                nh0 = ((pl[0] & 0x81) | (fu_type << 1)) & 0xFF
                self._cur = bytearray([nh0, pl[1]])
                self._cur += pl[3:]
            elif self._cur is not None:
                self._cur += pl[3:]
            if end_bit and self._cur is not None:
                self._emit(out, self._cur)
                self._cur = None
        elif nal_type == 48:  # AP — bir paketda bir nechta NAL
            i = 2
            while i + 2 <= len(pl):
                sz = int.from_bytes(pl[i:i + 2], "big")
                i += 2
                self._emit(out, pl[i:i + sz])
                i += sz
        else:  # yagona NAL (VPS/SPS/PPS/slice)
            self._emit(out, pl)
        return bytes(out)


class H264RtpDecryptor(_NalDecryptBase):
    """RTP/H.264 paketlarini Annex-B ga aylantirib, NAL body ni dekodlaydi.
    H.264 NAL header 1 bayt; FU-A=28, STAP-A=24, yagona NAL=1..23."""

    # H.264 SPS body[0] = profile_idc (toza oqimda shu qiymatlardan biri)
    _PROFILES = {66, 77, 88, 100, 110, 122, 244, 44, 83, 86, 118, 128}

    def __init__(self, key: str):
        super().__init__(key)
        self._cur = None

    def _detect(self, nal_type, body):  # override: H.264 SPS (tur 7)
        if self._decrypt is not None or nal_type != 7 or len(body) < 1:
            return
        if body[0] in self._PROFILES:   # toza (profile_idc ko'rinadi) — qisqa SPS uchun ham
            self._decrypt = False
            return
        if len(body) < 16:              # AES tekshiruvi uchun 1 blok kerak
            return
        dec0 = AES.new(self.key, AES.MODE_ECB).decrypt(bytes(body[:16]))[0]
        if dec0 in self._PROFILES:
            self._decrypt = True
        else:
            self.key_error = True

    def _emit(self, out: bytearray, nal):  # override: 1 baytli NAL header
        if len(nal) >= 1:
            self._detect(nal[0] & 0x1F, nal[1:])
            if self._decrypt is None:
                return  # shifr aniqlanmaguncha (SPS'gacha) garbage chiqarmaymiz
            out += START + bytes(nal[:1]) + self._decrypt_body(nal[1:])

    def feed(self, rtp_packet: bytes) -> bytes:
        if (rtp_packet[1] & 0x7F) != HEVC_VIDEO_PT:
            return b""
        pl = rtp_payload(rtp_packet)
        if len(pl) < 1:
            return b""
        out = bytearray()
        t = pl[0] & 0x1F
        if t == 28:  # FU-A — bo'lingan NAL
            if len(pl) < 2:
                return b""
            fu = pl[1]
            start_bit = fu >> 7
            end_bit = (fu >> 6) & 1
            fu_type = fu & 0x1F
            if start_bit:
                self._cur = bytearray([(pl[0] & 0xE0) | fu_type])
                self._cur += pl[2:]
            elif self._cur is not None:
                self._cur += pl[2:]
            if end_bit and self._cur is not None:
                self._emit(out, self._cur)
                self._cur = None
        elif t == 24:  # STAP-A — bir paketda bir nechta NAL
            i = 1
            while i + 2 <= len(pl):
                sz = int.from_bytes(pl[i:i + 2], "big")
                i += 2
                self._emit(out, pl[i:i + sz])
                i += sz
        else:  # yagona NAL (SPS/PPS/slice)
            self._emit(out, pl)
        return bytes(out)


def detect_rtp_codec(payload0: int):
    """RTP video payload birinchi baytidan codec: 'h264' | 'hevc' | None.
    Faqat ANIQ markerlar (FU yoki param-set) bo'yicha; noaniq (oddiy slice)
    paketda None qaytaramiz -> chaqiruvchi keyingi paketni tekshiradi."""
    h264_t = payload0 & 0x1F
    hevc_t = (payload0 >> 1) & 0x3F
    # Aniq H.264: FU-A=28, STAP-A=24, SPS=0x67, PPS=0x68, IDR=0x65
    if h264_t in (28, 24) or payload0 in (0x67, 0x68, 0x65):
        return "h264"
    # Aniq HEVC: FU=49, AP=48, VPS=0x40, SPS=0x42, PPS=0x44
    # (HEVC IDR 0x26/0x28 ishlatilmaydi — H.264 SEI/PPS bilan to'qnashadi;
    #  HEVC IDR baribir katta -> FU=49 orqali aniqlanadi)
    if hevc_t in (49, 48) or payload0 in (0x40, 0x42, 0x44):
        return "hevc"
    return None  # noaniq


class PsStreamDecryptor(_NalDecryptBase):
    """MPEG-PS oqimini demux qilib, ES dagi NAL body larni dekodlaydi.
    H.264 va HEVC ni avtomatik aniqlaydi. (Kutubxona PS dekodlovchisi cheksiz
    video PES'da ishlamaydi, shuning uchun o'zimiz.)"""

    _PROFILES = {66, 77, 88, 100, 110, 122, 244, 44, 83, 86, 118, 128}

    def __init__(self, key: str):
        super().__init__(key)
        self._buf = bytearray()   # demux qilinmagan PS qoldig'i
        self._es = bytearray()    # ajratilgan ES (NAL ga ajratilmagan qoldiq)
        self._hdr = 2             # NAL header o'lchami (codec aniqlangach o'rnatiladi)

    def _emit(self, out: bytearray, nal) -> None:  # override: codec-aware
        if not nal:
            return
        b0 = nal[0]
        if self.codec is None:  # param-set NAL dan codec aniqlash
            if b0 in (0x40, 0x42, 0x44):       # HEVC VPS/SPS/PPS
                self.codec, self._hdr = "hevc", 2
            elif b0 in (0x67, 0x68):           # H.264 SPS/PPS
                self.codec, self._hdr = "h264", 1
            else:
                return  # codec hali noma'lum (slice) -> o'tkazib yuboramiz
        hdr = self._hdr
        if len(nal) < hdr:
            return
        if self._decrypt is None:
            self._detect_ps(b0, nal[hdr:])
        if self._decrypt is None:
            return  # shifr aniqlanmaguncha garbage chiqarmaymiz
        out += START + bytes(nal[:hdr]) + self._decrypt_body(nal[hdr:])

    def _detect_ps(self, b0, body):
        # Faqat param-set NAL (HEVC VPS=0x40 / H.264 SPS=0x67) orqali aniqlaymiz
        if len(body) < 1 or not ((self.codec == "hevc" and b0 == 0x40) or
                                  (self.codec == "h264" and b0 == 0x67)):
            return
        clear_marker = (body[0] == 0x0C) if self.codec == "hevc" else (body[0] in self._PROFILES)
        if clear_marker:                    # toza — qisqa param-set uchun ham
            self._decrypt = False
            return
        if len(body) < 16:                  # AES tekshiruvi uchun 1 blok kerak
            return
        dec0 = AES.new(self.key, AES.MODE_ECB).decrypt(bytes(body[:16]))[0]
        dec_marker = (dec0 == 0x0C) if self.codec == "hevc" else (dec0 in self._PROFILES)
        if dec_marker:
            self._decrypt = True
        else:
            self.key_error = True

    def _demux(self):
        """PS dan video PES (0xE0-EF) payload'larini ajratib _es ga qo'shadi."""
        b = self._buf
        n = len(b)
        pos = 0
        while True:
            sc = b.find(b"\x00\x00\x01", pos)
            if sc < 0 or sc + 4 > n:
                break
            code = b[sc + 3]
            if code == 0xBA:  # pack header
                if sc + 14 > n:
                    break
                end = sc + 14 + (b[sc + 13] & 0x07)  # + stuffing
                if end > n:
                    break
                pos = end
            elif code == 0xB9:  # MPEG end
                pos = sc + 4
            elif 0xE0 <= code <= 0xEF:  # video PES
                if sc + 9 > n:
                    break
                length = (b[sc + 4] << 8) | b[sc + 5]
                payload_start = sc + 9 + b[sc + 8]  # 9 = 6 + 2 flags + 1 hdrlen
                if payload_start > n:
                    break
                if length > 0:
                    end = sc + 6 + length
                    if end > n:
                        break
                    self._es += b[payload_start:end]
                    pos = end
                else:  # cheksiz: keyingi start kodgacha
                    nxt = b.find(b"\x00\x00\x01", payload_start)
                    if nxt < 0:
                        break  # to'liq emas — keyingi chunk'ni kutamiz
                    self._es += b[payload_start:nxt]
                    pos = nxt
            else:  # boshqa stream (audio/system/PSM) — uzunlik bo'yicha o'tkazamiz
                if sc + 6 > n:
                    break
                length = (b[sc + 4] << 8) | b[sc + 5]
                end = sc + 6 + length
                if end > n:
                    break
                pos = end
        self._buf = b[pos:]

    def _process_es(self) -> bytes:
        """_es dagi to'liq NAL'larni dekodlab Annex-B qaytaradi."""
        e = self._es
        starts = []
        i = e.find(b"\x00\x00\x01", 0)
        while i >= 0:
            starts.append(i)
            i = e.find(b"\x00\x00\x01", i + 3)
        if len(starts) < 2:
            return b""
        out = bytearray()
        for k in range(len(starts) - 1):
            nal = e[starts[k] + 3:starts[k + 1]]  # header(2) + body
            # keyingi start kod 4-baytli bo'lsa, oxirgi 0x00 ni kesamiz
            if nal and nal[-1:] == b"\x00":
                nal = nal[:-1]
            self._emit(out, nal)
        self._es = e[starts[-1]:]  # oxirgi to'liqsiz NAL qoladi
        return bytes(out)

    def feed(self, ps_chunk: bytes) -> bytes:
        self._buf += ps_chunk
        self._demux()
        return self._process_es()


def _make_client():
    import re
    with open(config.TOKEN_FILE, encoding="utf-8") as f:
        token = json.load(f)
    client = EzvizClient(token.get("username"), None, token.get("api_url"), token=token)
    # Klient turi — busiz pagelist hamma resurslarni qaytarmaydi (platformaga qarab)
    try:
        client._session.headers.update({"clientType": _CLIENT_TYPE, "lang": "en-US"})
    except Exception:
        pass
    # Hik-Connect authAddr ni "https://null" qaytaradi -> regiondan derive qilamiz
    # (apiiSGP.hik-connect.com -> sgpauth.ezvizlife.com). EZVIZ uchun ham to'g'ri.
    su = (token.get("service_urls") or {})
    if not su.get("authAddr") or "null" in str(su.get("authAddr")).lower():
        m = re.match(r"apii([a-z]+)\.", str(token.get("api_url", "")))
        if m:
            client._token.setdefault("service_urls", {})["authAddr"] = \
                f"https://{m.group(1)}auth.ezvizlife.com"
    return client


def _device_names(client):
    """{serial: qurilma_nomi} — ilovadagi nomlar (NVR/kamera nomi)."""
    tok = getattr(client, "_token", {}) or {}
    api = tok.get("api_url")
    sess = _rq.Session()
    sess.headers.update({
        "clientType": _CLIENT_TYPE, "lang": "en-US",
        "featureCode": "1fc28fa018178a1cd1c091b13b2f9f02",
        "sessionId": str(tok.get("session_id")),
    })
    names = {}
    offset = 0
    for _ in range(40):
        r = sess.get(f"https://{api}/v3/userdevices/v1/devices/pagelist",
                     params={"filter": "CONNECTION", "groupId": -1, "limit": 50, "offset": offset},
                     timeout=25)
        d = r.json()
        di = d.get("deviceInfos") or []
        for x in di:
            names[x.get("deviceSerial")] = (x.get("name") or "").strip()
        page = d.get("page") or {}
        if not di or not page.get("hasNext"):
            break
        offset += len(di)
    return names


def list_cameras(client=None):
    """Haqiqiy kameralar ro'yxati: [(serial, channel, name)].
    Nom = "Qurilma nomi — Kanal nomi" (ilovadagi nom bilan moslash uchun).
    VTM resurslaridan olinadi — bo'sh NVR kanallari (kamera ulanmagan) ko'rsatilmaydi."""
    client = client or _make_client()
    dev_names = _device_names(client)
    res = _cs.get_vtm_page_list(client).get("resourceInfos", []) or []
    cams = []
    for r in res:
        try:
            ch = int(r.get("localIndex"))
        except (TypeError, ValueError):
            continue
        if ch < 1:  # localIndex 0 = qurilma (NVR) o'zi, kamera emas
            continue
        serial = r.get("deviceSerial")
        chname = (r.get("resourceName") or "").strip()
        devname = dev_names.get(serial, "")
        if devname and chname and devname.lower() not in chname.lower():
            label = f"{devname} — {chname}"
        else:
            label = chname or devname or f"{serial} CH{ch}"
        cams.append((serial, ch, label))
    cams.sort(key=lambda x: (x[0], x[1]))
    return cams


def serve(serial: str, port: int, key: str, channel: int = 1):
    client = _make_client()

    def _flag_keyerror():
        try:
            with open(keyerror_flag_path(serial, channel), "w") as fl:
                fl.write("1")
        except Exception:
            pass

    def _flag_offline():
        try:
            with open(offline_flag_path(serial, channel), "w") as fl:
                fl.write("1")
        except Exception:
            pass

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            # Xom oqimni dekodlab to'g'ridan-to'g'ri uzatamiz (ichki ffmpeg yo'q):
            #   RTP/PS -> Annex-B (H.264/HEVC), MPEG-TS -> o'zicha.
            # Tashqi ffmpeg (stream_manager) codec'ni auto-detect qilib, bardoshli dekodlaydi.
            try:
                stream_cm = open_cloud_stream(client, serial, channel=channel,
                                              client_type=9, refresh_vtm=False, timeout=20.0)
            except Exception as e:
                if "offline" in str(e).lower() or "unreachable" in str(e).lower():
                    _flag_offline()
                self.send_error(502)
                return
            try:
                with stream_cm as stream:
                    stream.start()
                    pkts = stream.iter_packets()
                    # Format (RTP/PS/TS) va codec (H.264/HEVC) ni aniqlash uchun
                    # boshlang'ich paketlarni o'qiymiz (keyin ularni ham uzatamiz)
                    buffered = []
                    is_rtp = None
                    codec = None
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
                    if not buffered:
                        self.send_error(502)
                        return

                    if not is_rtp and buffered[0][:1] == b"\x47":
                        transform = lambda b: b              # MPEG-TS (shifrsiz)
                        keyerr = lambda: False
                    elif not is_rtp:
                        dec = PsStreamDecryptor(key)          # MPEG-PS (H.264/HEVC avto)
                        transform = dec.feed; keyerr = lambda: dec.key_error
                    elif codec == "h264":
                        dec = H264RtpDecryptor(key)           # RTP/H.264
                        transform = dec.feed; keyerr = lambda: dec.key_error
                    else:
                        dec = HevcRtpDecryptor(key)           # RTP/HEVC
                        transform = dec.feed; keyerr = lambda: dec.key_error

                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()

                    for body in chain(buffered, (bytes(p.body) for p in pkts)):
                        data = transform(body)
                        if keyerr():
                            _flag_keyerror()
                            break
                        if data:
                            self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            except Exception as e:
                if "offline" in str(e).lower() or "unreachable" in str(e).lower():
                    _flag_offline()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Foydalanish: python decrypt_proxy.py <SERIAL> <PORT> <CODE> [CHANNEL]")
        sys.exit(1)
    serve(sys.argv[1], int(sys.argv[2]), sys.argv[3],
          int(sys.argv[4]) if len(sys.argv) > 4 else 1)
