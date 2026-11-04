"""
Stream Manager — 24/7 uzilishsiz ishlash uchun mo'ljallangan.

Xususiyatlar:
  - Har kamera alohida threadda (proxy + ffmpeg)
  - Stream uzilsa CHEKSIZ qayta ulanadi (exponential backoff bilan)
  - Token avtomatik yangilanadi (parol/MFA so'ramaydi)
  - Har bir kamera holatini kuzatadi (FPS, oxirgi frame vaqti)
"""

import atexit
import os
import socket
import subprocess
import sys
import threading
import time
import weakref

import numpy as np

# Proxy bilan shartnoma (muhit o'zgaruvchilari, bayroq fayllar) bitta joyda —
# [[cloudcam.core.ipc]]: ikki tomon nomlari endi ajralib keta olmaydi.
from cloudcam.core.ipc import (
    KEY_ENV,
    PARENT_WATCH_ENV,
    keyerror_flag_path,
    offline_flag_path,
)
from cloudcam.i18n import t
from cloudcam.settings import get_active

# Har kamera proxy'si shu modul sifatida ishga tushadi.
PROXY_MODULE = "cloudcam.sources.cloud.proxy"


def _stop_proc(proc, timeout=3.0):
    """Jarayonni to'xtatib, TUGASHINI kutadi (kerak bo'lsa kill).

    Faqat terminate() yetmaydi: Linux'da zombie qoladi, Windows'da esa port
    qisqa vaqt band bo'lib, yangi proxy shu portga ulanolmasdi."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=timeout)
    except Exception:
        pass
    for name in ("stdin", "stdout", "stderr"):          # quvur dastaklari oqib ketmasin
        f = getattr(proc, name, None)
        try:
            if f:
                f.close()
        except Exception:
            pass


# Ishlab turgan oqimlar — dastur chiqishida (atexit) proxy/ffmpeg qolmasin.
_LIVE = weakref.WeakSet()


@atexit.register
def _stop_all_live():
    for s in list(_LIVE):
        s.stop()


def load_cam_keys(path=None):
    """Saqlangan tasdiqlash kodlari ([[keys.load]] ustidagi eski nom)."""
    from cloudcam.sources.cloud import keys as _keys
    return _keys.load(path)


class CameraStream:
    # Qotgan oqim watchdog: ffmpeg ishlab turibdi-yu, shuncha soniya kadr
    # kelmasa — oqim qotgan (bulut relay ulanishni yopmasdan to'xtagan) yoki
    # birinchi kadr umuman kelmagan. ffmpeg o'ldiriladi -> reader qayta
    # ulanadi. FREEZE_TIMEOUT birinchi kadr kechikishidan (~7-10 s) katta.
    FREEZE_TIMEOUT = 20.0
    FREEZE_CHECK = 5.0
    OFFLINE_BACKOFF_MAX = 60.0   # oflayn kamerani qayta sinash oralig'i (maks)

    def __init__(self, serial, channel, port, decrypt=False,
                 width=None, height=None, key=None, url=None):
        self.serial = serial
        self.channel = channel
        self.port = port
        self.decrypt = decrypt
        self.key = key  # shifrlangan kamera "Tasdiqlash Kodu" (verification code)
        # TASHQI manba (masalan ISUP ko'prigi bergan RTSP). Berilgan bo'lsa
        # lokal dekodlovchi proxy ishga tushmaydi — oqim allaqachon toza
        # ([[cloudcam.sources.isup]]), ffmpeg uni to'g'ridan-to'g'ri o'qiydi.
        self.url = url
        _s = get_active()
        self.width = width or _s.display_width
        self.height = height or _s.display_height

        self.proxy_proc = None
        self.ffmpeg_proc = None
        self.latest_frame = None
        self.running = False
        self.connected = False
        self.last_frame_time = 0
        self.reconnect_count = 0
        self.fps = 0
        self.error = None  # masalan: shifr kodi xato yoki signal yo'q
        # error vaqtinchami (OFFLINE / signal yo'q / qotgan) — kadr kelsa tozalanadi.
        # Matn tilga bog'liq, shuning uchun qaror matndan emas, shu bayroqdan.
        self._error_transient = False
        self._consec_empty = 0  # ketma-ket kadrsiz urinishlar (bo'sh kanalni aniqlash)
        self._offline_tries = 0  # ketma-ket OFFLINE urinishlar (kutishni uzaytirish)

        self._lock = threading.Lock()
        self._thread = None
        self._watch_thread = None
        self._ffmpeg_started = 0.0    # joriy ffmpeg urinishi boshlangan vaqt
        self._frame_counter = 0
        self._fps_time = time.time()
        # GPU (NVDEC) dekod — sozlamadan; ishlamasa avtomatik dasturiyga o'tadi
        self._use_gpu = get_active().use_gpu
        self._gpu_empty = 0           # GPU bilan ketma-ket kadrsiz urinishlar

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
        return keyerror_flag_path(self.serial, self.channel)

    def _offline_path(self):
        return offline_flag_path(self.serial, self.channel)

    def _project_root(self):
        """`cloudcam` paketini import qila oladigan papka (src/ layout'da `src/`):
        proxy shu cwd bilan ishga tushadi, paket o'rnatilmagan bo'lsa ham topiladi."""
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _python_exe(self):
        """venv python (Crypto/requests shu yerda). 'py app.py' bilan ham ishlashi uchun.
        venv loyiha ildizida — src/ layout'da bu `src/` ning ota-papkasi."""
        root = self._project_root()
        for base in (root, os.path.dirname(root)):
            venv_py = os.path.join(base, "venv", "Scripts", "python.exe")
            if os.path.exists(venv_py):
                return venv_py
        return sys.executable

    def _start_proxy(self):
        _stop_proc(self.proxy_proc)
        self.proxy_proc = None
        # eski bayroqlarni tozalaymiz (yangi urinish)
        for p in (self._keyerr_path(), self._offline_path()):
            try:
                os.remove(p)
            except OSError:
                pass
        python = self._python_exe()
        s = get_active()
        # Subprocess sozlamani muhit o'zgaruvchilari orqali oladi (config.py
        # ga bog'liq emas — kutubxona rejimida ham ishlaydi). TO'LIQ sozlama
        # uzatiladi ([[Settings.to_env]]): ilgari faqat `token_file` ketardi
        # va bola-jarayon `platform` ni standart qiymatdan olib, noto'g'ri
        # `clientType` yuborardi.
        env = s.child_env()
        # ABSOLUT yo'l: proxy cwd=loyiha ildizi (pip o'rnatilganda site-packages)
        # bilan ishlaydi, token esa joriy papkaga yozilgan — nisbiy yo'l boshqa
        # faylni qidirib proxy darhol yiqilardi.
        token_file = os.path.abspath(s.token_file)
        env["CLOUDCAM_TOKEN_FILE"] = token_file
        # Har kamera — o'z dekodlovchi proxy (modul sifatida, loyiha ildizidan).
        # Shifrlanmagan kamera ham shu yerdan o'tadi: dekoderlar toza oqimni
        # o'zi aniqlab, o'zgartirmasdan uzatadi ("AUTO" kaliti). Ilgari buning
        # uchun alohida `python -m pyezvizapi ... stream proxy` jarayoni bor edi.
        # Kod = AES kalit: argv'da process ro'yxatida hammaga ko'rinardi, shuning
        # uchun env orqali beriladi ("-" — "kalitni env'dan ol" belgisi).
        env[KEY_ENV] = self.key if (self.decrypt and self.key) else "AUTO"
        env[PARENT_WATCH_ENV] = "1"
        cmd = [
            python, "-m", PROXY_MODULE,
            self.serial, str(self.port), "-", str(self.channel),
        ]
        # Grid uchun bulutdan KICHIK oqim so'raymiz ([[sources.cloud.stream.open_cloud_stream]]):
        # plitka baribir ~250-500 px, 1440p esa 36 barobar ortiqcha trafik.
        mode = "sub" if s.substream else s.stream_mode
        if mode in ("sub", "auto"):
            cmd.append(f"--stream={mode}")
        # stdin=PIPE — ota jarayon yiqilsa quvur yopiladi va proxy buni
        # sezib chiqadi ([[PARENT_WATCH_ENV]]). Biz unga hech narsa yozmaymiz.
        self.proxy_proc = subprocess.Popen(
            cmd, cwd=self._project_root(), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not self.running:          # shu orada stop() chaqirilgan — yetim qolmasin
            _stop_proc(self.proxy_proc)
            self.proxy_proc = None

    def _ffmpeg_cmd(self):
        url = self.url or f"http://127.0.0.1:{self.port}/{self.serial}.ts"
        # -nostdin: konsoldagi tugmalarni (masalan 'q') o'qib, o'zini to'xtatmasin.
        cmd = ["ffmpeg", "-nostdin"]
        if self.url and self.url.startswith("rtsp://"):
            # RTSP: TCP majburan — UDP da paket yo'qolishi tasvirni buzadi,
            # va NAT/konteyner tarmog'ida UDP ko'pincha umuman o'tmaydi.
            cmd += ["-rtsp_transport", "tcp"]
        if self._use_gpu:
            # NVIDIA NVDEC — dekodni GPU ga o'tkazadi (ko'p kamera uchun CPU ni bo'shatadi)
            cmd += ["-hwaccel", "cuda"]
        cmd += [
            # DIQQAT: `nobuffer` QO'YILMAYDI! U demuxer'ga probe paytida
            # buferlamaslikni aytadi va o'sha paytda yutilgan paketlar —
            # VPS/SPS/PPS va BIRINCHI IDR — shunchaki tashlanadi. Dekoder
            # P-freymlardan boshlaydi va tomoshabin kameraning KEYINGI IDR'igacha
            # kul rang/buzilgan tasvir ko'radi. Bir xil kirish faylida o'lchandi:
            # `nobuffer` bilan birinchi kadr Y(low..high)=125..127 (tekis kul
            # rang), busiz 54..190 (haqiqiy tasvir). Kechikish uchun u kerak emas
            # — kichik probe ayni shu foydani beradi.
            # discardcorrupt ham yo'q — kadr tashlanishini kamaytiradi (silliqroq)
            "-fflags", "+genpts",
            "-flags", "low_delay",
            "-err_detect", "ignore_err",
            # Kichik probe = tez boshlash. Kirish Annex-B (H.264/HEVC) bo'lsa
            # SPS/PPS birinchi KB'larda; MPEG-TS bo'lsa PAT/PMT uchun bir oz
            # ko'proq kerak — 200 KB ikkalasiga ham yetadi (avval 500 KB edi,
            # ya'ni ochilishda ~0.5s bekorga ketardi).
            "-analyzeduration", "200000",
            "-probesize", "200000",
        ]
        # proxy MPEG-TS chiqaradi (RTP/HEVC ham, MPEG-PS ham) -> avtomatik aniqlanadi
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
                # 1. Proxy ishga tushirish — TASHQI manbada (ISUP) kerak emas:
                #    oqim allaqachon toza RTSP, deshifr qilinadigan narsa yo'q.
                if not self.url:
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
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=frame_size * 4,
                )
                self._ffmpeg_started = time.time()  # watchdog shu vaqtdan sanaydi
                # backoff bu yerda EMAS, birinchi kadrdan keyin tiklanadi: Popen
                # doim muvaffaqiyatli, shuning uchun doimiy xatoda kamera har
                # 1-2 s da qayta urinib, bulutga cheksiz so'rov yuborardi.
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
                    # Kod umuman yo'q bo'lsa "AUTO" beriladi — bunda "kod xato"
                    # deyish chalg'itadi: foydalanuvchi mavjud bo'lmagan faylni
                    # tekshirishga tushadi. Nima qilish kerakligini aytamiz.
                    self.error = (t("No verification code - run `cloudcam keys`",
                                    "Shifr kodi yo'q — `cloudcam keys` ishga tushiring")
                                  if not self.key or self.key == "AUTO"
                                  else t("Wrong verification code! Check cam_keys.json",
                                         "Shifr kodi xato! cam_keys.json ni tekshiring"))
                    self._error_transient = False
                    self.connected = False
                    # qayta urinish foydasiz (kod baribir xato) — proxy ham
                    # yopiladi, aks holda stop() gacha portni band qilib turardi
                    _stop_proc(self.proxy_proc)
                    self.proxy_proc = None
                    break

                # Kamera OFFLINE (VTM ma'lumot bermadi) — proxy bayroq qo'ygan.
                # Oflayn kamera qaytib kelguncha har 15s da urinish bulutga
                # bekorga yuk (200 kameralik hisobda bu doimiy toshqin) —
                # shuning uchun ketma-ket urinishlarda kutish UZAYADI.
                if os.path.exists(self._offline_path()):
                    self._offline_tries += 1
                    wait = min(self.OFFLINE_BACKOFF_MAX,
                               15 * min(self._offline_tries, 4))
                    self.error = t(f"OFFLINE (camera off or no signal) - "
                                   f"retrying in {wait:.0f}s",
                                   f"OFFLINE (kamera o'chiq yoki signal yo'q) — "
                                   f"{wait:.0f}s dan keyin qayta urinamiz")
                    self._error_transient = True
                    self.connected = False
                    time.sleep(wait)
                    continue
                self._offline_tries = 0

                # GPU bilan ketma-ket 0 kadr -> NVDEC ishlamayapti, dasturiyga
                # o'tamiz. Bitta bo'sh urinish (502, bulut qotishi) yetarli emas.
                if frames_this_attempt == 0 and self._use_gpu:
                    self._gpu_empty += 1
                    if self._gpu_empty >= 3:
                        self._use_gpu = False
                elif frames_this_attempt:
                    self._gpu_empty = 0

                # Bo'sh kanal (kamera ulanmagan) ni aniqlash: ketma-ket kadrsiz urinishlar
                if frames_this_attempt > 0:
                    backoff = 1
                    self._consec_empty = 0
                    if self.error and self._error_transient:
                        self.error = None  # kamera qaytib keldi
                        self._error_transient = False
                else:
                    self._consec_empty += 1
                    if self._consec_empty >= 3 and not self.error:
                        self.error = t("No signal (camera not connected?)",
                                       "Signal yo'q (kamera ulanmagan?)")
                        self._error_transient = True

            except FileNotFoundError:
                # ffmpeg PATH'da yo'q: avval jim yutilib, oynada abadiy
                # "Connecting..." turardi.
                self.error = t("ffmpeg not found - install it and add it to PATH",
                               "ffmpeg topilmadi — o'rnating va PATH ga qo'shing")
                self._error_transient = False
                backoff = 30
            except Exception as e:  # noqa: BLE001 — 24/7 sikl to'xtamasin
                if not self.error:
                    self.error = t(f"Stream error: {e}", f"Oqim xatosi: {e}")
                    self._error_transient = True
            finally:
                proc, self.ffmpeg_proc = self.ffmpeg_proc, None
                _stop_proc(proc)

            # Uzildi — qayta ulanishdan oldin kutish
            if self.running:
                self.connected = False
                self.fps = 0.0
                self.reconnect_count += 1
                time.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)

    def _watchdog_loop(self):
        """Qotgan oqimni aniqlaydi: ffmpeg ishlayapti-yu, FREEZE_TIMEOUT dan
        beri kadr yo'q -> ffmpeg'ni o'ldiradi. Shunda reader'ning bloklangan
        read() i uziladi va qayta ulanish boshlanadi. Bloklangan read busiz
        abadiy kutardi (bulut ba'zan ulanishni yopmasdan kadrni to'xtatadi)."""
        while self.running:
            time.sleep(self.FREEZE_CHECK)
            if not self.running or not self.ffmpeg_proc:
                continue
            # Sanoq nuqtasi: oxirgi kadr yoki (kadr yo'q bo'lsa) ffmpeg boshi.
            ref = max(self.last_frame_time, self._ffmpeg_started)
            if ref and time.time() - ref > self.FREEZE_TIMEOUT:
                self.connected = False
                if self.error is None:
                    self.error = t("Stream frozen (no frames) - reconnecting",
                                   "Oqim qotdi (kadr yo'q) — qayta ulanmoqda")
                    self._error_transient = True
                try:
                    self.ffmpeg_proc.kill()   # read() uziladi -> reader qayta ulanadi
                except Exception:
                    pass

    def start(self):
        self.running = True
        _LIVE.add(self)
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()
        self._watch_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watch_thread.start()

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
        _LIVE.discard(self)
        _stop_proc(self.ffmpeg_proc)
        _stop_proc(self.proxy_proc)
        self.proxy_proc = None


class StreamManager:
    def __init__(self, client=None, settings=None):
        self.streams = {}
        self.client = client  # token refresh uchun
        if settings is not None:
            from cloudcam.settings import set_active
            set_active(settings)
        self._next_port = get_active().proxy_start_port
        self._token_thread = None
        self._running = False

    def add(self, serial, channel=1, decrypt=False, width=None, height=None,
            key=None, url=None):
        """Kamera qo'shadi. `url` berilsa — tashqi manba (ISUP RTSP), bulut
        proxy'si ishlatilmaydi ([[CameraStream.url]])."""
        skey = (serial, channel)  # NVR'ning har kanali alohida stream
        if skey in self.streams:
            return self.streams[skey]
        stream = CameraStream(serial, channel, self._next_port, decrypt,
                              width, height, key, url)
        self._next_port += 1
        self.streams[skey] = stream
        stream.start()
        return stream

    def remove(self, serial, channel=1):
        skey = (serial, channel)
        if skey in self.streams:
            self.streams[skey].stop()
            del self.streams[skey]

    # Yangilash kerakmi degan savol shu oraliqda tekshiriladi; yangilashning
    # o'zi sessiya YOSHIGA qarab bo'ladi ([[CloudClient.is_refresh_needed]]).
    TOKEN_CHECK_INTERVAL = 60.0

    def start_token_refresh(self, interval=None):
        """Sessiyani fon threadda yangilab turadi — 24/7 uchun.

        Ilgari bu `time.sleep(3600)` edi: kompyuter uyquga ketib qaytsa yoki
        oqim uzilib qayta ulansa, sessiya allaqachon eskirgan bo'lsa ham
        thread hali "soat"ini kutib turardi. Endi har daqiqada sessiya
        YOSHI tekshiriladi — `hikconnect` dagi `is_refresh_login_needed()`
        bilan bir xil yondashuv.

        `interval` berilsa — sessiyaning maksimal yoshi (orqaga moslik uchun
        eski `interval=3600` chaqiruvi ham xuddi shunday ishlaydi).
        """
        if not self.client:
            return
        self._running = True
        # Eski `interval=3600` chaqiruvlari sessiyani `connect()` "eskirgan"
        # deb hisoblagan yoshdan (SESSION_MAX_AGE) ancha keyin yangilardi —
        # o'rtadagi vaqtda proxy'lar 401 olib, o'zlari yangilashga urinardi.
        limit = getattr(self.client, "SESSION_MAX_AGE", None)
        max_age = interval if not (interval and limit) else min(interval, limit)

        def loop():
            while self._running:
                time.sleep(self.TOKEN_CHECK_INTERVAL)
                if not self._running:
                    break
                try:
                    if not self.client.is_refresh_needed(max_age):
                        continue
                    self._refresh_and_save(get_active().token_file)
                    print(f"[{time.strftime('%H:%M:%S')}] 🔄 "
                          + t("Token refreshed", "Token yangilandi"))
                except Exception as e:
                    print(f"[{time.strftime('%H:%M:%S')}] ⚠️  "
                          + t(f"Token refresh failed: {e}", f"Token yangilash xatosi: {e}"))

        self._token_thread = threading.Thread(target=loop, daemon=True)
        self._token_thread.start()

    def _refresh_and_save(self, path):
        """Avval parolsiz (boshqa jarayon yangilagan bo'lsa — o'shani olib),
        bulut refresh tokenni rad etsagina parol bilan qayta kiradi."""
        from cloudcam.sources.cloud.client import SessionExpired
        again = getattr(self.client, "refresh_and_save", None)
        if again is None:                       # eski/soxta mijoz
            self.client.refresh_session()
            self.client.save_token(path)
            return
        try:
            again(path)
        except SessionExpired:
            self.client.login()
            self.client.save_token(path)

    def stop_all(self):
        self._running = False
        for s in self.streams.values():
            s.stop()
        self.streams.clear()
