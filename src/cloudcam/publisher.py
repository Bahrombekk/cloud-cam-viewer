"""Kamerani RTSP serverga (MediaMTX, go2rtc, ...) TRANSKODSIZ publish qilish.

`cloudcam stream` va `CloudCam.publish()` shu yerda. Bu `cloudcam serve` (0.4) ning
poydevori: har kamera bitta upstream bo'lib RTSP serverga chiqadi, ko'ruvchilarga esa
server o'zi tarqatadi (bulutdan har ko'ruvchiga alohida oqim ochilmaydi).

[[CameraStream]] dan meros: dekodlovchi proxy, qayta ulanish, bayroqlar (kod xato /
oflayn), watchdog — hammasi o'sha. Farqi faqat ffmpeg'da:

  * kadr DEKODLANMAYDI: `-c:v copy -f rtsp` (CPU ~0, sifat yo'qolmaydi);
  * proxy chiqishi xom Annex-B — unda VAQT BELGISI YO'Q. ffmpeg uni o'zicha 25 fps
    deb hisoblaydi, bulut kamerasi esa 9-15 fps beradi: o'yinchi tasvirni tezlashtirib
    yoki kechiktirib, oxiri buferni to'ldirib yuboradi. Shuning uchun kelish vaqti
    bo'yicha belgilanadi (`-use_wallclock_as_timestamps 1`). Kadrlar proxy'ning
    jitter buferidan ([[PacedWriter]]) allaqachon tekis tezlikda keladi;
  * jonlilik xom kadrlardan emas, ffmpeg `-progress` hisobotidagi `frame=` dan.
"""

from __future__ import annotations

import collections
import functools
import re
import subprocess
import threading
from urllib.parse import urlsplit, urlunsplit

from cloudcam.stream_manager import CameraStream


def redact_url(url: str) -> str:
    """URL'dagi parolni yashiradi (log va ekran uchun)."""
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    if p.password is None:
        return url
    host = p.hostname or ""
    if p.port:
        host += f":{p.port}"
    return urlunsplit(p._replace(netloc=f"{p.username}:***@{host}"))


CONNECT_TIMEOUT_US = 5_000_000   # RTSP serverga ulanish / yozish uchun soket timeout


@functools.lru_cache(maxsize=1)
def ffmpeg_major() -> int | None:
    """O'rnatilgan ffmpeg'ning asosiy versiyasi (aniqlab bo'lmasa None)."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-version"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"ffmpeg version n?(\d+)\.", out)
    return int(m.group(1)) if m else None


def check_target(url: str) -> str:
    """Publish manzili `rtsp://` yoki `rtsps://` bo'lishi kerak."""
    p = urlsplit(url or "")
    if p.scheme not in ("rtsp", "rtsps") or not p.hostname or len(p.path) < 2:
        raise ValueError(f"RTSP target must look like rtsp://host:8554/path, got {url!r}")
    return url


class RtspPublisher(CameraStream):
    """Bitta kamera -> RTSP server. `start()` / `stop()` / `status()` CameraStream'dagidek."""

    STDERR_LINES = 20

    def __init__(self, serial, channel, port, target, *, decrypt=True, key=None,
                 url=None, settings=None):
        super().__init__(serial, channel, port, decrypt=decrypt, key=key, url=url,
                         settings=settings)
        self.target = check_target(target)
        self._use_gpu = False                 # dekod yo'q — NVDEC kerak emas
        self._stderr = collections.deque(maxlen=self.STDERR_LINES)

    def _ffmpeg_cmd(self):
        src = self.url or f"http://127.0.0.1:{self.port}/{self.serial}.ts"
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-nostats", "-progress", "pipe:1"]
        if self.url and self.url.startswith("rtsp"):
            # Tashqi RTSP manba (ISUP) o'z vaqt belgilari bilan keladi.
            cmd += ["-rtsp_transport", "tcp"]
        else:
            # Proxy: xom Annex-B, vaqt belgisi yo'q (modul izohiga qarang).
            cmd += ["-use_wallclock_as_timestamps", "1"]
        cmd += [
            # nobuffer YO'Q: birinchi parametr to'plamlari va IDR tashlanib ketadi.
            "-fflags", "+genpts",
            "-analyzeduration", "200000",
            "-probesize", "200000",
            "-i", src,
            "-map", "0:v:0", "-c:v", "copy", "-an",
            "-f", "rtsp", "-rtsp_transport", "tcp",
        ]
        # Server javob bermasa ffmpeg xatosiz OSILIB qolardi (o'lchandi: 30 s+); timeout
        # bilan ~5 s da aniq xato beradi. ffmpeg < 5 da RTSP `-timeout` boshqa ma'noda
        # (tinglash rejimini yoqadi) — u yerda qo'yilmaydi, watchdog baribir uzadi.
        major = ffmpeg_major()
        if major is not None and major >= 5:
            cmd += ["-timeout", str(CONNECT_TIMEOUT_US)]
        cmd.append(self.target)
        return cmd

    def _ffmpeg_popen_kwargs(self) -> dict:
        return dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, bufsize=0)

    def _consume(self, proc) -> int:
        """`-progress` qatorlarini o'qiydi; `frame=N` o'sganda — oqim jonli.

        stderr alohida thread'da o'qiladi (to'lib qolib ffmpeg'ni to'xtatmasin) va
        oxirgi qatorlari saqlanadi: RTSP server javob bermasa sabab ko'rinsin."""
        self._stderr.clear()
        drain = None
        if getattr(proc, "stderr", None) is not None:
            drain = threading.Thread(target=self._drain_stderr, args=(proc.stderr,),
                                     daemon=True)
            drain.start()
        total = last = 0
        for raw in iter(proc.stdout.readline, b""):
            if not self.running:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("frame="):
                continue
            try:
                frame = int(line.split("=", 1)[1])
            except ValueError:
                continue
            if frame > last:
                total += frame - last
                self._mark_frames(frame - last)
                last = frame
        if drain is not None:
            drain.join(timeout=1.0)
        if total == 0 and self._stderr and not self.error:
            self.error = "ffmpeg: " + self._stderr[-1]
            self._error_transient = True
        return total

    def _drain_stderr(self, stream) -> None:
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._stderr.append(line)
        except (OSError, ValueError):
            pass

    def get_frame(self):
        return None                           # kadr dekodlanmaydi

    def status(self):
        st = super().status()
        st["target"] = redact_url(self.target)
        return st
