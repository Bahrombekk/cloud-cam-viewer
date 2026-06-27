"""
Stream Manager — 24/7 uzilishsiz ishlash uchun mo'ljallangan.

Xususiyatlar:
  - Har kamera alohida threadda (proxy + ffmpeg)
  - Stream uzilsa CHEKSIZ qayta ulanadi (exponential backoff bilan)
  - Token avtomatik yangilanadi (parol/MFA so'ramaydi)
  - Har bir kamera holatini kuzatadi (FPS, oxirgi frame vaqti)
"""

import subprocess
import sys
import threading
import socket
import time
import atexit
import json
import os
import tempfile

import numpy as np

import config


def load_cam_keys():
    """Saqlangan cam_key larni yuklash."""
    if os.path.exists(config.CAMKEY_FILE):
        try:
            with open(config.CAMKEY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


class CameraStream:
    def __init__(self, serial, channel, port, decrypt=False,
                 width=None, height=None, key=None):
        self.serial = serial
        self.channel = channel
        self.port = port
        self.decrypt = decrypt
        self.key = key  # shifrlangan kamera "Tasdiqlash Kodu" (verification code)
        self.width = width or config.DISPLAY_WIDTH
        self.height = height or config.DISPLAY_HEIGHT

        self.proxy_proc = None
        self.ffmpeg_proc = None
        self.latest_frame = None
        self.running = False
        self.connected = False
        self.last_frame_time = 0
        self.reconnect_count = 0
        self.fps = 0
        self.error = None  # masalan: shifr kodi xato yoki signal yo'q
        self._consec_empty = 0  # ketma-ket kadrsiz urinishlar (bo'sh kanalni aniqlash)

        self._lock = threading.Lock()
        self._thread = None
        self._frame_counter = 0
        self._fps_time = time.time()
        # GPU (NVDEC) dekod — config.USE_GPU bilan; ishlamasa avtomatik dasturiyga o'tadi
        self._use_gpu = getattr(config, "USE_GPU", False)

    def _wait_port(self, timeout=20):
        start = time.time()
        while time.time() - start < timeout and self.running:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return True
            except OSError:
                time.sleep(0.3)
        return False

    def _keyerr_path(self):
        return os.path.join(tempfile.gettempdir(),
                            f"ezviz_keyerr_{self.serial}_{self.channel}.flag")

    def _offline_path(self):
        return os.path.join(tempfile.gettempdir(),
                            f"ezviz_offline_{self.serial}_{self.channel}.flag")

    def _project_root(self):
        # cloudcam/ ning ota-papkasi = loyiha ildizi
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _python_exe(self):
        """venv python (pyezvizapi/Crypto shu yerda). 'py app.py' bilan ham ishlashi uchun."""
        venv_py = os.path.join(self._project_root(), "venv", "Scripts", "python.exe")
        return venv_py if os.path.exists(venv_py) else sys.executable

    def _start_proxy(self):
        if self.proxy_proc:
            self.proxy_proc.terminate()
        # eski bayroqlarni tozalaymiz (yangi urinish)
        for p in (self._keyerr_path(), self._offline_path()):
            try:
                os.remove(p)
            except OSError:
                pass
        python = self._python_exe()
        if self.decrypt and self.key:
            # Shifrlangan kamera — o'z dekodlovchi proxy (modul sifatida, loyiha ildizidan)
            cmd = [
                python, "-m", "cloudcam.decrypt_proxy",
                self.serial, str(self.port), self.key, str(self.channel),
            ]
        else:
            # Shifrlanmagan kamera — standart pyezvizapi proxy (modul sifatida)
            cmd = [
                python, "-m", "pyezvizapi", "--token-file", config.TOKEN_FILE,
                "stream", "proxy",
                "--serial", self.serial,
                "--channel", str(self.channel),
                "--listen-port", str(self.port),
                "--allow-encrypted",
            ]
        self.proxy_proc = subprocess.Popen(
            cmd, cwd=self._project_root(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _ffmpeg_cmd(self):
        url = f"http://127.0.0.1:{self.port}/{self.serial}.ts"
        cmd = ["ffmpeg"]
        if self._use_gpu:
            # NVIDIA NVDEC — dekodni GPU ga o'tkazadi (ko'p kamera uchun CPU ni bo'shatadi)
            cmd += ["-hwaccel", "cuda"]
        cmd += [
            # discardcorrupt olib tashlandi — kadr tashlanishini kamaytiradi (silliqroq)
            "-fflags", "nobuffer+genpts",
            "-flags", "low_delay",
            "-err_detect", "ignore_err",
            # tezroq boshlash uchun kichraytirildi (HEVC tez aniqlanadi)
            "-analyzeduration", "500000",
            "-probesize", "500000",
        ]
        # decrypt_proxy MPEG-TS chiqaradi (RTP/HEVC ham, MPEG-PS ham) -> avtomatik aniqlanadi
        cmd += [
            "-i", url,
            # passthrough — kadrlarni takrorlamaydi (faqat haqiqiy kadrlar -> kam CPU)
            "-fps_mode", "passthrough",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-vf", f"scale={self.width}:{self.height}",
            "-an", "-",
        ]
        return cmd

    def _reader_loop(self):
        """24/7 loop — har qanday uzilishda qayta ulanadi, cheksiz."""
        frame_size = self.width * self.height * 3
        backoff = 1  # qayta ulanish kechikishi (sekund)

        while self.running:
            try:
                # 1. Proxy ishga tushirish
                self._start_proxy()
                if not self._wait_port():
                    self.connected = False
                    self.reconnect_count += 1
                    time.sleep(min(backoff, 30))
                    backoff = min(backoff * 2, 30)
                    continue

                time.sleep(0.3)  # proxy GET ga tayyor bo'lishi uchun qisqa kutish

                # 2. FFmpeg ishga tushirish
                self.ffmpeg_proc = subprocess.Popen(
                    self._ffmpeg_cmd(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=frame_size * 4,
                )

                backoff = 1  # muvaffaqiyatli ulandi, backoff ni tiklash
                frames_this_attempt = 0

                # 3. Frame o'qish
                while self.running:
                    raw = self.ffmpeg_proc.stdout.read(frame_size)
                    if not raw or len(raw) < frame_size:
                        break  # uzildi
                    frames_this_attempt += 1

                    frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                        (self.height, self.width, 3)).copy()

                    with self._lock:
                        self.latest_frame = frame
                        self.connected = True
                        self.last_frame_time = time.time()

                    # FPS hisoblash
                    self._frame_counter += 1
                    now = time.time()
                    if now - self._fps_time >= 1.0:
                        self.fps = self._frame_counter / (now - self._fps_time)
                        self._frame_counter = 0
                        self._fps_time = now

                # Shifr kodi xato bo'lsa — proxy bayroq qo'ygan bo'ladi
                if os.path.exists(self._keyerr_path()):
                    self.error = "Shifr kodi xato! cam_keys.json ni tekshiring"
                    self.connected = False
                    break  # qayta urinish foydasiz (kod baribir xato)

                # Kamera OFFLINE (VTM ma'lumot bermadi) — proxy bayroq qo'ygan
                if os.path.exists(self._offline_path()):
                    self.error = "OFFLINE (kamera o'chiq yoki signal yo'q)"
                    self.connected = False
                    time.sleep(min(backoff, 15))
                    backoff = min(backoff * 2, 15)
                    continue

                # GPU bilan 0 kadr olindi -> NVDEC ishlamayapti, dasturiyga o'tamiz
                if frames_this_attempt == 0 and self._use_gpu:
                    self._use_gpu = False

                # Bo'sh kanal (kamera ulanmagan) ni aniqlash: ketma-ket kadrsiz urinishlar
                if frames_this_attempt > 0:
                    self._consec_empty = 0
                    if self.error and ("Signal" in self.error or "OFFLINE" in self.error):
                        self.error = None  # kamera qaytib keldi
                else:
                    self._consec_empty += 1
                    if self._consec_empty >= 3 and not self.error:
                        self.error = "Signal yo'q (kamera ulanmagan?)"

            except Exception:
                pass
            finally:
                if self.ffmpeg_proc:
                    self.ffmpeg_proc.terminate()
                    self.ffmpeg_proc = None

            # Uzildi — qayta ulanishdan oldin kutish
            if self.running:
                self.connected = False
                self.reconnect_count += 1
                time.sleep(min(backoff, 10))
                backoff = min(backoff * 2, 10)

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()

    def get_frame(self):
        with self._lock:
            return self.latest_frame.copy() if self.latest_frame is not None else None

    def status(self):
        """Kamera holati — monitoring uchun."""
        age = time.time() - self.last_frame_time if self.last_frame_time else 999
        return {
            "serial": self.serial,
            "connected": self.connected and age < 10,
            "fps": round(self.fps, 1),
            "reconnects": self.reconnect_count,
            "frame_age": round(age, 1),
            "error": self.error,
        }

    def stop(self):
        self.running = False
        if self.ffmpeg_proc:
            self.ffmpeg_proc.terminate()
        if self.proxy_proc:
            self.proxy_proc.terminate()


class StreamManager:
    def __init__(self, client=None):
        self.streams = {}
        self.client = client  # token refresh uchun
        self._next_port = config.PROXY_START_PORT
        self._token_thread = None
        self._running = False

    def add(self, serial, channel=1, decrypt=False, width=None, height=None, key=None):
        skey = (serial, channel)  # NVR'ning har kanali alohida stream
        if skey in self.streams:
            return self.streams[skey]
        stream = CameraStream(serial, channel, self._next_port, decrypt, width, height, key)
        self._next_port += 1
        self.streams[skey] = stream
        stream.start()
        return stream

    def remove(self, serial, channel=1):
        skey = (serial, channel)
        if skey in self.streams:
            self.streams[skey].stop()
            del self.streams[skey]

    def start_token_refresh(self, interval=3600):
        """
        Token ni davriy yangilab turish (default: har soatda).
        Bu MFA/parol qayta so'ralishining oldini oladi — 24/7 ishlash uchun.
        Token faylga yoziladi, proxy lar uni o'qiydi.
        """
        if not self.client:
            return
        self._running = True

        def loop():
            while self._running:
                time.sleep(interval)
                if not self._running:
                    break
                try:
                    self.client.refresh_session()
                    self.client.save_token(config.TOKEN_FILE)
                    print(f"[{time.strftime('%H:%M:%S')}] 🔄 Token yangilandi")
                except Exception as e:
                    print(f"[{time.strftime('%H:%M:%S')}] ⚠️  Token yangilash xatosi: {e}")

        self._token_thread = threading.Thread(target=loop, daemon=True)
        self._token_thread.start()

    def stop_all(self):
        self._running = False
        for s in self.streams.values():
            s.stop()
        self.streams.clear()
