"""`cloudcam serve` — har kamerani lokal RTSP / WebRTC / HLS manzilga aylantirish.

MediaMTX ustida. Har kamera uchun ikki yo'l:

    cam/<SERIAL>_<KANAL>        asosiy oqim
    cam/<SERIAL>_<KANAL>/sub    kichik oqim (substream'i yo'q kamerada asosiysi)

Yo'l TALAB BO'YICHA ochiladi (`runOnDemand`): birinchi ko'ruvchi kelganda MediaMTX
`cloudcam stream` ni ishga tushiradi, u bulutdan oqimni olib shu yo'lga publish
qiladi; qolgan ko'ruvchilar o'sha oqimga ulanadi (bulutdan bitta upstream), oxirgi
ko'ruvchi ketgach `runOnDemandCloseAfter` dan keyin yopiladi.

Xavfsizlik standartlari:
  * server standart holatda faqat 127.0.0.1 da tinglaydi (`--listen 0.0.0.0` bilan ochiladi);
  * PUBLISH faqat shu kompyuterdan — aks holda tarmoqdagi istalgan kishi `cam/...`
    yo'liga o'z videosini qo'yib, kamera tasvirini almashtira olardi (MediaMTX
    standart sozlamasida publish hammaga ochiq);
  * tashqaridan o'qish uchun ixtiyoriy login/parol (`--read-user/--read-pass`);
  * konfiguratsiyada ro'yxatda yo'q yo'llar umuman qabul qilinmaydi.

Konfiguratsiya JSON ko'rinishida yoziladi — JSON YAML'ning qismi, MediaMTX uni
o'qiydi va PyYAML bog'liqligi kerak emas.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

MTX_MIN_VERSION = (1, 9, 0)         # authInternalUsers (1.7+) va ishlatilgan kalit nomlari
# Versiyaga bog'liq kalitlar: MediaMTX NOMA'LUM kalitda umuman ishga tushmaydi, shuning
# uchun ular faqat o'sha versiyadan boshlab yoziladi (rasmiy mediamtx.yml'lar bo'yicha).
MTX_MOQ_VERSION = (1, 19, 0)        # MoQ: barcha interfeyslarda ochiladi, sertifikat yozadi
START_TIMEOUT_S = 25                # bulutdan birinchi kadr 7-10 s; MediaMTX standarti 10 s
CLOSE_AFTER_S = 30                  # oxirgi ko'ruvchidan keyin oqim shuncha ochiq turadi
# MediaMTX standarti 10 s: publisher shuncha jim tursa uziladi. Bulut oqimi esa
# to'xtab-to'xtab keladi — jonli o'lchov: bir kanalning bir sessiyasi keskin uzilsa
# (masalan asosiy yo'l yopilganda) shu kanalning ikkinchi oqimi ~9 s to'xtab, keyin
# to'plangani bilan davom etadi. 10 s da u uzilib, ~25 s lik qayta ulanishga tushardi.
READ_TIMEOUT_S = 30

_SAFE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def version_str(v) -> str:
    return ".".join(str(x) for x in v)


def mediamtx_version(binary: str) -> tuple[int, int, int] | None:
    """`mediamtx --version` -> (1, 20, 0); aniqlab bo'lmasa None."""
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True,
                             timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", out or "")
    return tuple(int(x) for x in m.groups()) if m else None


def path_name(serial: str, channel: int, sub: bool = False) -> str:
    """MediaMTX yo'li: `cam/AB1234567_1` yoki `cam/AB1234567_1/sub`."""
    return f"cam/{serial}_{int(channel)}" + ("/sub" if sub else "")


def rtsp_path_name(name: str, sub: bool = False) -> str:
    """Oddiy RTSP kamera yo'li: `rtsp/<nom>` yoki `rtsp/<nom>/sub`."""
    return f"rtsp/{name}" + ("/sub" if sub else "")


def _quote(arg: str) -> str:
    """MediaMTX buyruq qatorini bo'sh joy bo'yicha bo'ladi — bo'sh joyli yo'l
    (masalan `C:\\Program Files\\...\\python.exe`) qo'shtirnoqqa olinadi."""
    if arg and not re.search(r'[\s"]', arg):
        return arg
    return '"' + arg.replace('"', '\\"') + '"'


def stream_command(python: str, serial: str, channel: int, sub: bool,
                   publish_host: str) -> str:
    r"""Yo'l ochilganda MediaMTX ishga tushiradigan buyruq.

    `$RTSP_PORT` va `$MTX_PATH` ni MediaMTX o'zi almashtiradi. Sessiyani ota
    jarayon (`cloudcam serve`) yangilaydi — shuning uchun `--no-refresh`.

    MediaMTX buyruqni Unix uslubida bo'ladi va teskari slashni ESCAPE deb olib
    tashlaydi: `C:\Users\...\python.exe` -> `C:Users...python.exe` (jonli sinovda
    topilgan — "cannot find the file"). Windows to'g'ri slashli yo'lni ham qabul qiladi."""
    python = python.replace("\\", "/")
    args = [python, "-m", "cloudcam", "stream", serial, "--channel", str(int(channel)),
            "--rtsp", f"rtsp://{publish_host}:$RTSP_PORT/$MTX_PATH",
            "--stream", "auto" if sub else "main", "--no-refresh"]
    return " ".join(_quote(a) for a in args)


def build_config(cameras, *, python: str | None = None, listen: str = "127.0.0.1",
                 rtsp_port: int = 8554, hls_port: int = 8888, webrtc_port: int = 8889,
                 rtp_port: int = 8000, webrtc_udp_port: int = 8189, substream: bool = True,
                 read_user: str | None = None,
                 read_pass: str | None = None, start_timeout: int = START_TIMEOUT_S,
                 close_after: int = CLOSE_AFTER_S,
                 mtx_version: tuple[int, int, int] | None = None,
                 rtsp_cameras=()) -> dict:
    """MediaMTX konfiguratsiyasi (dict). `cameras` — `Camera` yoki (serial, kanal, nom).

    Seriyasi xavfsiz bo'lmagan kamera (`A-Z a-z 0-9 _ -` dan boshqa belgi) tashlab
    ketiladi: u yo'l nomiga va buyruq qatoriga tushadi.

    `rtsp_cameras` — oddiy RTSP kameralar ([[cloudcam.sources.rtsp.store.RtspCamera]]):
    ular uchun proksi jarayon kerak emas, MediaMTX manbani o'zi tortadi
    (`source` + `sourceOnDemand`). Transport kaliti YOZILMAYDI: uning nomi
    versiyalar orasida o'zgargan (MediaMTX noma'lum kalitda ishga tushmaydi),
    standarti esa baribir avtomatik (TCP/UDP)."""
    if bool(read_user) != bool(read_pass):
        raise ValueError("read_user and read_pass must be given together")
    python = python or sys.executable
    any_host = listen in ("", "0.0.0.0", "::")
    publish_host = "127.0.0.1" if any_host or listen == "127.0.0.1" else listen
    publish_ips = ["127.0.0.1", "::1"] + ([] if publish_host == "127.0.0.1" else [publish_host])
    bind = "" if any_host else listen

    paths: dict[str, dict] = {}
    for cam in cameras:
        serial, channel = (cam.serial, cam.channel) if hasattr(cam, "serial") else cam[:2]
        if not _SAFE.fullmatch(str(serial)):
            continue
        for sub in ((False, True) if substream else (False,)):
            paths[path_name(serial, channel, sub)] = {
                "runOnDemand": stream_command(python, serial, channel, sub, publish_host),
                "runOnDemandRestart": True,
                "runOnDemandStartTimeout": f"{int(start_timeout)}s",
                "runOnDemandCloseAfter": f"{int(close_after)}s",
            }
    for rc in rtsp_cameras:
        if not _SAFE.fullmatch(rc.name):
            continue
        for sub, url in ((False, rc.url), (True, rc.sub_url)):
            if url and (substream or not sub):
                paths[rtsp_path_name(rc.name, sub)] = {
                    "source": url,
                    "sourceOnDemand": True,
                    "sourceOnDemandStartTimeout": f"{int(start_timeout)}s",
                    "sourceOnDemandCloseAfter": f"{int(close_after)}s",
                }

    local_all = [{"action": a} for a in ("publish", "read", "playback")]
    readers = {"user": read_user or "any", "pass": read_pass or "", "ips": [],
               "permissions": [{"action": "read"}]}
    config = {
        "logLevel": "info",
        "readTimeout": f"{READ_TIMEOUT_S}s",
        "rtsp": True,
        "rtspAddress": f"{bind}:{int(rtsp_port)}",
        # RTSP UDP transporti ham shu manzilga — aks holda u `--listen` dan qat'i
        # nazar barcha interfeyslarda ochilardi (MediaMTX standarti `:8000`).
        "rtpAddress": f"{bind}:{int(rtp_port)}",
        "rtcpAddress": f"{bind}:{int(rtp_port) + 1}",
        "hls": True,
        "hlsAddress": f"{bind}:{int(hls_port)}",
        "webrtc": True,
        "webrtcAddress": f"{bind}:{int(webrtc_port)}",
        # WebRTC media (ICE) UDP porti: standart 8189 boshqa dastur bilan band bo'lsa
        # MediaMTX umuman ishga tushmaydi — sozlanadigan.
        "webrtcLocalUDPAddress": f"{bind}:{int(webrtc_udp_port)}",
        "rtmp": False,
        "srt": False,
        "authInternalUsers": [
            {"user": "any", "pass": "", "ips": publish_ips, "permissions": local_all},
            readers,
        ],
        "paths": paths,
    }
    if mtx_version is not None and mtx_version >= MTX_MOQ_VERSION:
        config["moq"] = False
    return config


def write_config(config: dict, path: str) -> str:
    """Konfiguratsiyani yozadi (o'qish paroli bo'lishi mumkin — 0600, atomar)."""
    from cloudcam.core.fsutil import write_private
    write_private(path, json.dumps(config, indent=2) + "\n")
    return path


def urls(cameras, *, host: str, rtsp_port: int = 8554, webrtc_port: int = 8889,
         substream: bool = True, rtsp_cameras=()) -> list[tuple[str, str, str]]:
    """[(nom, RTSP manzil, WebRTC sahifa)] — foydalanuvchiga ko'rsatish uchun."""
    out = []
    for rc in rtsp_cameras:
        if _SAFE.fullmatch(rc.name):
            p = rtsp_path_name(rc.name)
            out.append((rc.name, f"rtsp://{host}:{rtsp_port}/{p}",
                        f"http://{host}:{webrtc_port}/{p}"))
    for cam in cameras:
        serial, channel = (cam.serial, cam.channel) if hasattr(cam, "serial") else cam[:2]
        name = getattr(cam, "name", "") or f"{serial} ch{channel}"
        if not _SAFE.fullmatch(str(serial)):
            continue
        p = path_name(serial, channel)
        out.append((name, f"rtsp://{host}:{rtsp_port}/{p}",
                    f"http://{host}:{webrtc_port}/{p}"))
    return out


def find_mediamtx(explicit: str | None = None) -> str | None:
    """MediaMTX dasturi: berilgan yo'l, PATH, keyin `<data dir>/bin/`."""
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    found = shutil.which("mediamtx")
    if found:
        return found
    from cloudcam.settings import data_dir
    exe = "mediamtx.exe" if os.name == "nt" else "mediamtx"
    local = os.path.join(data_dir(), "bin", exe)
    return local if os.path.isfile(local) else None


def run_mediamtx(binary: str, config_path: str, env: dict) -> int:
    """MediaMTX'ni ishga tushirib, tugashini kutadi (Ctrl+C — to'xtatadi).

    On-demand `cloudcam stream` jarayonlarini MediaMTX o'zi boshqaradi va
    to'xtaganda yopadi; ularning proxy'lari ota jarayon o'lsa o'zi chiqadi.
    `cloudcam serve` MAJBURAN o'ldirilsa ham MediaMTX va uning oqimlari ortda
    qolmaydi ([[cloudcam.core.procutil.popen_bound]])."""
    from cloudcam.core.procutil import popen_bound
    proc = popen_bound([binary, config_path], env=env)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 0
