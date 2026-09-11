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
    token_file: str = "token.json"
    camkey_file: str = "cam_keys.json"

    # ── manbalar ──────────────────────────────────────────────────────
    @classmethod
    def from_config_module(cls) -> "Settings":
        """Eski `config.py` dan (mavjud bo'lsa) — orqaga moslik."""
        try:
            import config as c
        except Exception:
            return cls()
        return cls(
            platform=getattr(c, "PLATFORM", cls.platform),
            email=getattr(c, "EMAIL", cls.email),
            password=getattr(c, "PASSWORD", cls.password),
            region=getattr(c, "REGION", cls.region),
            proxy_start_port=getattr(c, "PROXY_START_PORT", cls.proxy_start_port),
            display_width=getattr(c, "DISPLAY_WIDTH", cls.display_width),
            display_height=getattr(c, "DISPLAY_HEIGHT", cls.display_height),
            use_gpu=getattr(c, "USE_GPU", cls.use_gpu),
            token_file=getattr(c, "TOKEN_FILE", cls.token_file),
            camkey_file=getattr(c, "CAMKEY_FILE", cls.camkey_file),
        )

    @classmethod
    def from_env(cls, base: "Settings | None" = None) -> "Settings":
        """`CLOUDCAM_*` muhit o'zgaruvchilarini `base` ustiga qo'yadi."""
        s = base or cls()
        env = {
            "platform": os.environ.get("CLOUDCAM_PLATFORM"),
            "email": os.environ.get("CLOUDCAM_EMAIL"),
            "password": os.environ.get("CLOUDCAM_PASSWORD"),
            "region": os.environ.get("CLOUDCAM_REGION"),
            "proxy_start_port": os.environ.get("CLOUDCAM_PROXY_START_PORT"),
            "display_width": os.environ.get("CLOUDCAM_DISPLAY_WIDTH"),
            "display_height": os.environ.get("CLOUDCAM_DISPLAY_HEIGHT"),
            "use_gpu": os.environ.get("CLOUDCAM_USE_GPU"),
            "token_file": os.environ.get("CLOUDCAM_TOKEN_FILE"),
            "camkey_file": os.environ.get("CLOUDCAM_CAMKEY_FILE"),
        }
        vals = {k: v for k, v in env.items() if v is not None}
        for k in ("proxy_start_port", "display_width", "display_height"):
            if k in vals:
                vals[k] = int(vals[k])
        if "use_gpu" in vals:
            vals["use_gpu"] = vals["use_gpu"].lower() in ("1", "true", "yes", "on")
        return s.replace(**vals)

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
