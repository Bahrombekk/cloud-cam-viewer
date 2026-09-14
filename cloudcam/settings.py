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
from dataclasses import dataclass, fields

ENV_PREFIX = "CLOUDCAM_"
_MISSING = object()
_TRUE = ("1", "true", "yes", "on")

# Bola-jarayonga UZATILMAYDIGAN maydonlar. Parol muhit o'zgaruvchisida
# ko'p tizimlarda jarayonlar ro'yxati orqali ko'rinadi, bola-jarayonga esa
# u umuman kerak emas — u tokenni faylidan o'qiydi.
_ENV_EXCLUDE = frozenset({"password"})


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
    password: str = ""
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
    # ([[decrypt_proxy.serve]]). Hamma kamerada ham substream yo'q (masalan
    # batareyali modellar), shuning uchun qattiq "sub" xavfli.
    stream_mode: str = "main"
    token_file: str = "token.json"
    camkey_file: str = "cam_keys.json"

    # ── manbalar ──────────────────────────────────────────────────────
    @classmethod
    def from_config_module(cls) -> "Settings":
        """Eski `config.py` dan (mavjud bo'lsa) — orqaga moslik.

        Maydon nomi UPPER_CASE ga aylantiriladi, ya'ni yangi sozlama
        qo'shilganda bu yerni tahrirlash SHART EMAS (ilgari `substream`
        aynan shu sababdan config'dan o'qilmasdan qolgan edi)."""
        try:
            import config as c
        except Exception:
            return cls()
        vals = {}
        for f in fields(cls):
            v = getattr(c, f.name.upper(), _MISSING)
            if v is not _MISSING:
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
            vals[f.name] = _coerce(f.type, raw)
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

    @classmethod
    def resolve(cls) -> "Settings":
        """Standart yechim: config.py (bo'lsa) -> muhit o'zgaruvchilari."""
        return cls.from_env(cls.from_config_module())

    def replace(self, **kw) -> "Settings":
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data.update(kw)
        return Settings(**data)


# ── modul darajasidagi faol sozlama ──────────────────────────────────
# stream_manager va decrypt_proxy shuni o'qiydi. Default — config.py'ni
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
