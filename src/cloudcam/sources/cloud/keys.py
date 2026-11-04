"""Kamera tasdiqlash kodlari (verification code) — saqlash va BULUTDAN olish.

Tasdiqlash kodi = oqimni ochuvchi AES kaliti ([[cloudcam.codec]]). Ilgari uni
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

from cloudcam.sources.cloud.client import MfaRequired
from cloudcam.core.fsutil import harden, write_private
from cloudcam.settings import default_camkey_file, get_active


def _path(path: str | None = None) -> str:
    return path or get_active().camkey_file or default_camkey_file()


class KeysFileError(ValueError):
    """`cam_keys.json` bor, lekin o'qib bo'lmaydi — ustidan YOZILMAYDI."""


def load(path: str | None = None, *, strict: bool = False) -> dict[str, str]:
    """Saqlangan kodlar: {serial: KOD}.

    `strict=False` (o'qish uchun) — buzuq fayl `{}` beradi, oqim baribir ochiladi.
    `strict=True` — faylni QAYTA YOZISHDAN oldin: fayl bor-u o'qilmasa
    [[KeysFileError]]. Aks holda qo'lda tahrirlangan fayldagi bitta ortiqcha
    vergul `{}` ga aylanib, keyingi `save()` HAMMA kodni o'chirib yuborardi."""
    p = _path(path)
    if not os.path.exists(p):
        return {}
    harden(p)                   # eski versiya 0644 bilan yozgan bo'lishi mumkin
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as err:
        if strict:
            raise KeysFileError(f"{p} is unreadable ({err}) - fix or remove it; "
                                f"not overwriting") from err
        return {}
    if not isinstance(data, dict):
        if strict:
            raise KeysFileError(f"{p} is not a JSON object - not overwriting")
        return {}
    return {k: str(v) for k, v in data.items()}


def save(keys: dict[str, str], path: str | None = None) -> None:
    """Kodlarni ATOMAR va 0600 bilan yozadi — kod = oqimning AES kaliti."""
    write_private(_path(path), json.dumps(keys, indent=2, sort_keys=True))


def set_code(serial: str, code: str, path: str | None = None) -> dict[str, str]:
    """Bitta kodni qo'shib, faylni saqlaydi.

    Kod AES kalit — registrga SEZGIR, AYNAN berilgandek saqlanadi. Ilgari
    katta harfga o'girilardi: zavod kodi (6 katta harf) uchun bu zararsiz edi,
    lekin ilovada o'zgartirilgan kod ("1q2w..." kabi) buzilib, jonli hisobda
    10 kamera "kod xato" bergan. Yorliqdagi kodni kichik harf bilan yozish
    holatini [[codecheck.check_with_case_fallback]] hal qiladi."""
    keys = load(path, strict=True)
    keys[serial] = code.strip()
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
    keys = load(path, strict=True)      # bulutga borishdan OLDIN — yozib bo'lmasa so'ramaymiz
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
