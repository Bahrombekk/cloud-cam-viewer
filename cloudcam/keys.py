"""Kamera tasdiqlash kodlari (verification code) — saqlash va BULUTDAN olish.

Tasdiqlash kodi = oqimni ochuvchi AES kaliti ([[decrypt_proxy]]). Ilgari uni
faqat QO'LDA kiritish mumkin edi (`set_code.py`, `check_code.py`): 150 kamerali
hisobda bu har bir qurilma yorlig'ini o'qib chiqishni anglatardi va oflayn NVR
uchun umuman imkonsiz edi.

Bulut esa kodni o'zi beradi. `pyezvizapi` dagi `get_cam_auth_code` shuni
ko'rsatdi: `GET /v3/devconfig/authcode/query/<serial>` -> `devAuthCode`.
Birinchi so'rovda bulut sessiyani "ko'tarishni" talab qiladi (2FA, meta.code
80000) — kod emailga keladi, BIR MARTA kiritiladi, keyin o'sha sessiyada
qolgan hamma kamera uchun so'rov to'g'ridan-to'g'ri ishlaydi.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .client import MfaRequired
from .settings import get_active


def _path(path: str | None = None) -> str:
    return path or get_active().camkey_file or "cam_keys.json"


def load(path: str | None = None) -> dict[str, str]:
    """Saqlangan kodlar: {serial: KOD}."""
    p = _path(path)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def save(keys: dict[str, str], path: str | None = None) -> None:
    """Kodlarni ATOMAR yozadi (yozish paytida uzilsa eski fayl buzilmasin)."""
    p = _path(path)
    tmp = f"{p}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=2, sort_keys=True)
    os.replace(tmp, p)


def set_code(serial: str, code: str, path: str | None = None) -> dict[str, str]:
    """Bitta kodni qo'shib, faylni saqlaydi."""
    keys = load(path)
    keys[serial] = code.strip().upper()
    save(keys, path)
    return keys


@dataclass
class FetchResult:
    """[[fetch]] natijasi."""
    fetched: dict[str, str] = field(default_factory=dict)   # yangi olingan kodlar
    skipped: list[str] = field(default_factory=list)        # allaqachon bor edi
    failed: dict[str, str] = field(default_factory=dict)    # serial -> sabab
    needs_mfa: bool = False                                 # 2FA kod kerak

    @property
    def ok(self) -> bool:
        return bool(self.fetched) and not self.needs_mfa


def fetch(client, serials, *, mfa_code: str | None = None,
          overwrite: bool = False, path: str | None = None) -> FetchResult:
    """Berilgan kameralar uchun tasdiqlash kodini BULUTDAN oladi va saqlaydi.

    `client` — login qilingan [[CloudClient]]. `serials` — seriyalar ro'yxati
    (NVR uchun BITTA seriya yetarli: kod qurilmaga tegishli, hamma kanalga
    birdek amal qiladi).

    2FA talab qilinsa `needs_mfa=True` bilan DARHOL qaytadi — chaqiruvchi
    `client.send_verification_2fa()` qilib, emaildagi kodni `mfa_code` da
    qaytaradi. Kod faqat BIRINCHI so'rovga kerak: u sessiyani ko'taradi,
    qolganlari kodsiz o'tadi.
    """
    result = FetchResult()
    keys = load(path)
    pending = [s for s in dict.fromkeys(serials)
               if overwrite or not keys.get(s) or keys.get(s) == "AUTO"]
    result.skipped = [s for s in dict.fromkeys(serials) if s not in pending]

    code_arg = mfa_code
    for serial in pending:
        try:
            code = client.get_verification_code(serial, mfa_code=code_arg)
        except MfaRequired:
            result.needs_mfa = True
            break                       # kodsiz davom etishning ma'nosi yo'q
        except Exception as e:
            result.failed[serial] = str(e)
            continue
        code_arg = None                 # 2FA kod bir marta ishlatiladi
        if code:
            keys[serial] = code
            result.fetched[serial] = code

    if result.fetched:
        save(keys, path)
    return result
