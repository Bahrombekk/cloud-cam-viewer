"""Sozlamalar — kutubxona uchun config'siz (obyekt/parametr orqali).

Ilgari `import config` (global modul) ishlatilardi — kutubxona buni qila
olmaydi. Endi barcha sozlama `Settings` obyektida. Uch manba, ustuvorlik
tartibida:

    1. To'g'ridan berilgan qiymatlar  (Settings(email=..., password=...))
    2. Muhit o'zgaruvchilari          (CLOUDCAM_EMAIL, ...)
    3. Eski `config.py` moduli        (mavjud bo'lsa — orqaga moslik uchun)
    4. Standart qiymatlar

`set_active()` / `get_active()` — modul darajasida faol sozlama (proxy
va stream_manager global config o'rniga shuni o'qiydi).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields

ENV_PREFIX = "CLOUDCAM_"
HOME_ENV = "CLOUDCAM_HOME"
# Fayl yo'li bo'lgan maydonlar — nisbiy qiymat JORIY papkaga emas, manbasiga
# (config.py yoniga) bog'lanadi ([[Settings.from_config_module]]).
_PATH_FIELDS = ("token_file", "camkey_file")


def data_dir() -> str:
    r"""cloudcam fayllari (sessiya, kodlar, terminal ID, kesh) uchun standart papka.

    Ilgari standart `token.json` NISBIY edi — buyruq qaysi papkadan ishga
    tushsa, sessiya o'sha yerda yaratilardi. Natijada bitta kompyuterda bir
    nechta `token.json`/`feature_code.txt` paydo bo'lib, bulut bitta hisobni
    bir nechta "qurilma" deb ko'rardi (yangi qurilma 2FA, terminal limiti).

    `CLOUDCAM_HOME` -> Windows `%APPDATA%\cloudcam`, macOS
    `~/Library/Application Support/cloudcam`, boshqalar `$XDG_CONFIG_HOME/cloudcam`
    (`~/.config/cloudcam`)."""
    env = (os.environ.get(HOME_ENV) or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
    return os.path.join(base, "cloudcam")


def default_token_file() -> str:
    return os.path.join(data_dir(), "token.json")


def default_camkey_file() -> str:
    return os.path.join(data_dir(), "cam_keys.json")
_MISSING = object()
_TRUE = ("1", "true", "yes", "on")

# Bola-jarayonga UZATILMAYDIGAN maydonlar. Parol muhit o'zgaruvchisida
# ko'p tizimlarda jarayonlar ro'yxati orqali ko'rinadi, bola-jarayonga esa
# u umuman kerak emas — u tokenni faylidan o'qiydi. `isup_token` ham xuddi
# shunday: ko'prik bilan FAQAT ota-jarayon gaplashadi.
_ENV_EXCLUDE = frozenset({"password", "isup_token"})


def _coerce(type_name, raw: str):
    """Matnni maydon turiga keltiradi (`from __future__ import annotations`
    tufayli `field.type` — matn, masalan "str | None")."""
    t = str(type_name)
    if "bool" in t:
        return raw.strip().lower() in _TRUE
    if "int" in t:
        return int(raw)
    return raw


@dataclass
class Settings:
    platform: str = "hikconnect"          # "hikconnect" | "ezviz"
    email: str = ""
    password: str = field(default="", repr=False)   # repr/log'da ko'rinmaydi
    region: str | None = None             # None — login paytida aniqlanadi
    proxy_start_port: int = 8700          # har kameraga ketma-ket port
    display_width: int = 1280
    display_height: int = 720
    use_gpu: bool = True                  # NVDEC; ishlamasa dasturiyga o'tadi
    # Bulutdan KICHIK oqim (substream) so'raladimi. Grid uchun juda foydali:
    # o'lchov — plitka 640x360 / ~0.11 Mbit/s, asosiy oqim esa ~4 Mbit/s
    # (36 barobar kam trafik va CPU). To'liq ekran uchun False qoldiring.
    substream: bool = False
    # Qaysi oqim so'ralsin: "main" | "sub" | "auto".
    # "auto" — avval substream so'raladi, u kelmasa asosiyga qaytiladi
    # ([[cloudcam.sources.cloud.proxy.serve]]). Hamma kamerada ham substream yo'q (masalan
    # batareyali modellar), shuning uchun qattiq "sub" xavfli.
    stream_mode: str = "main"
    # Standart — foydalanuvchi papkasi ([[data_dir]]), joriy papka EMAS.
    token_file: str = field(default_factory=default_token_file)
    camkey_file: str = field(default_factory=default_camkey_file)
    # Hisob metama'lumot keshini (VTM) fonda issiq tutish — ochilish ~6s o'rniga ~0.2s.
    warm_cache: bool = True
    # ── ISUP 5.0 ([[isup]]) ───────────────────────────────────────────
    # Qurilma bulutga emas, `isup-bridge` ga ulanadi. Ko'prik alohida
    # jarayon (ffmpeg kabi tashqi bog'liqlik) — bu yerda faqat uning
    # manzili. Ko'prik bo'lmasa hech narsa buzilmaydi: bulut yo'li o'z
    # holicha ishlayveradi.
    isup_url: str = "http://127.0.0.1:8091"      # ko'prikning HTTP API'si
    isup_token: str = field(default="", repr=False)  # X-Bridge-Token (ixtiyoriy)
    isup_rtsp_base: str = "rtsp://127.0.0.1:8554"  # ko'prik publish qiladigan MediaMTX

    # ── manbalar ──────────────────────────────────────────────────────
    @classmethod
    def from_config_module(cls) -> "Settings":
        """Eski `config.py` dan (mavjud bo'lsa) — orqaga moslik.

        Maydon nomi UPPER_CASE ga aylantiriladi, ya'ni yangi sozlama
        qo'shilganda bu yerni tahrirlash SHART EMAS (ilgari `substream`
        aynan shu sababdan config'dan o'qilmasdan qolgan edi)."""
        c = _load_config_module()
        if c is None:
            return cls()
        base = os.path.dirname(os.path.abspath(getattr(c, "__file__", "") or "config.py"))
        vals = {}
        for f in fields(cls):
            v = getattr(c, f.name.upper(), _MISSING)
            if v is _MISSING:
                continue
            # `TOKEN_FILE = "token.json"` — config.py YONIDAGI fayl, buyruq
            # qaysi papkadan ishga tushirilganidan qat'i nazar.
            if f.name in _PATH_FIELDS and isinstance(v, str) and v and not os.path.isabs(v):
                v = os.path.join(base, v)
            vals[f.name] = v
        return cls(**vals)

    @classmethod
    def from_env(cls, base: "Settings | None" = None) -> "Settings":
        """`CLOUDCAM_*` muhit o'zgaruvchilarini `base` ustiga qo'yadi.

        Maydonlar bo'ylab avtomatik: `substream` -> `CLOUDCAM_SUBSTREAM`."""
        s = base or cls()
        vals = {}
        for f in fields(cls):
            raw = os.environ.get(f"{ENV_PREFIX}{f.name.upper()}")
            if raw is None:
                continue
            v = _coerce(f.type, raw)
            if f.name in _PATH_FIELDS and v:
                v = os.path.abspath(os.path.expanduser(v))   # bola-jarayonlar bilan bir xil
            vals[f.name] = v
        return s.replace(**vals)

    def to_env(self) -> dict[str, str]:
        """Sozlamani `CLOUDCAM_*` juftliklariga aylantiradi.

        Buning uchun: har kamera ALOHIDA JARAYON ([[stream_manager]]), va
        jarayon sozlamani faqat muhitdan oladi. Ilgari faqat `token_file`
        uzatilardi — natijada kutubxona rejimida `CloudCam(platform="ezviz")`
        deb ochilgan oqim bola-jarayonda `hikconnect` deb qabul qilinib,
        noto'g'ri `clientType` yuborardi va pagelist to'liq kelmasdi."""
        out = {}
        for f in fields(self):
            if f.name in _ENV_EXCLUDE:
                continue
            v = getattr(self, f.name)
            if v is None:
                continue
            out[f"{ENV_PREFIX}{f.name.upper()}"] = (
                ("1" if v else "0") if isinstance(v, bool) else str(v))
        return out

    def child_env(self, base=None) -> dict[str, str]:
        """Bola-jarayon muhiti: `base` (standart — os.environ) + `to_env()`.

        Foydalanuvchi o'zi `CLOUDCAM_PASSWORD` qo'ygan bo'lsa ham u bola-jarayonga
        o'tmaydi — `to_env` uni tashlab ketishi yetarli emas, chunki u
        `os.environ` dan meros bo'lib qolardi."""
        env = dict(os.environ if base is None else base)
        for name in _ENV_EXCLUDE:
            env.pop(f"{ENV_PREFIX}{name.upper()}", None)
        env.update(self.to_env())
        return env

    @classmethod
    def resolve(cls) -> "Settings":
        """Standart yechim: config.py (bo'lsa) -> muhit o'zgaruvchilari."""
        return cls.from_env(cls.from_config_module())

    def replace(self, **kw) -> "Settings":
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data.update(kw)
        return Settings(**data)


# ── modul darajasidagi faol sozlama ──────────────────────────────────
# stream_manager va proxy shuni o'qiydi. Default — config.py'ni
# (bo'lsa) va muhit o'zgaruvchilarini avtomatik yechadi, ya'ni eski
# `import config` xatti-harakati saqlanadi.
_active: Settings | None = None


def get_active() -> Settings:
    global _active
    if _active is None:
        _active = Settings.resolve()
    return _active


def set_active(settings: Settings) -> None:
    global _active
    _active = settings


def _load_config_module():
    """`config.py` ni topadi: avval oddiy import, bo'lmasa JORIY papkadan.

    `cloudcam` buyrug'i (console script) ishga tushganda `sys.path` da joriy
    papka bo'lmaydi — `import config` jimgina yiqilib, CLI "Account required"
    deb turib olardi, garchi `config.py` shu yerda bo'lsa ham."""
    try:
        import config
        return config
    except ImportError:
        pass
    except Exception:
        return None
    path = os.path.join(os.getcwd(), "config.py")
    if not os.path.isfile(path):
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", path)
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod
