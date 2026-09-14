"""Terminal (qurilma) identifikatori — `featureCode`.

Bulut har bir "terminal"ni shu kod bilan tanidi: kirish limiti, CAPTCHA va
2FA "yangi qurilma" tekshiruvi — hammasi shunga bog'liq.

Ilgari bu yerda QATTIQ YOZILGAN bitta kod bor edi
(`1fc28fa018178a1cd1c091b13b2f9f02`) — uchta faylda nusxalangan holda. Bu ikki
tomondan zarar:

  1. Loyihaning HAR bir nusxasi bulut uchun BITTA terminal bo'lib ko'rinadi.
     Bulut esa bir terminaldan kiruvchi hisoblar sonini cheklaydi (EZVIZ da
     shu "terminal limit", code 1069) va shubhali faollikda CAPTCHA (1015)
     so'raydi — ya'ni boshqa birovning ishi sizning hisobingizga urib qo'yadi.
  2. Har ishga tushirishda kod bir xil bo'lsa ham, hisob o'zgarsa bulut buni
     "bitta qurilmada ko'p hisob" deb qaraydi.

Yechim (`pyezvizapi` va `ezviz-ha-addon` shu yo'ldan boradi): kod shu
O'RNATMAGA xos bo'lsin. MAC manzilidan olinadi — ya'ni bitta kompyuterda
DOIM bir xil (bulut sizni "o'sha qurilma" deb tanidi, qayta 2FA so'ramaydi),
lekin boshqa kompyuterda boshqacha.

MAC ba'zan o'zgaradi (VPN/virtual adapter, dok-stansiya). Shuning uchun
hisoblangan kod faylga ham yoziladi va keyingi safar O'SHA fayldan o'qiladi:
adapter almashsa ham terminal o'zgarmaydi.
"""
from __future__ import annotations

import hashlib
import os
import uuid

_FILENAME = "feature_code.txt"
_ENV = "CLOUDCAM_FEATURE_CODE"
_cached: str | None = None


def _from_mac() -> str:
    """MAC manzilidan barqaror 32 belgili kod (pyezvizapi bilan bir xil usul)."""
    mac_int = uuid.getnode()
    mac_str = ":".join(f"{(mac_int >> i) & 0xFF:02x}" for i in range(40, -1, -8))
    return hashlib.md5(mac_str.encode("utf-8")).hexdigest()


def _store_path() -> str:
    """Kod saqlanadigan fayl — token fayli yonida (u ham shu o'rnatmaga tegishli)."""
    from .settings import get_active
    tok = get_active().token_file or "token.json"
    d = os.path.dirname(os.path.abspath(tok))
    return os.path.join(d, _FILENAME)


def feature_code() -> str:
    """Shu o'rnatmaning terminal kodi (32 hex belgi).

    Ustuvorlik: `CLOUDCAM_FEATURE_CODE` -> saqlangan fayl -> MAC dan hisoblash.
    Hisoblangani birinchi marta faylga yoziladi.
    """
    global _cached
    if _cached:
        return _cached
    env = (os.environ.get(_ENV) or "").strip()
    if env:
        _cached = env
        return _cached
    path = _store_path()
    try:
        with open(path, encoding="utf-8") as f:
            saved = f.read().strip()
        if saved:
            _cached = saved
            return _cached
    except OSError:
        pass
    code = _from_mac()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
    except OSError:
        pass            # yozolmasak ham ishlayveramiz — MAC baribir barqaror
    _cached = code
    return _cached


def reset_cache() -> None:
    """Testlar uchun — keshlangan kodni unutish."""
    global _cached
    _cached = None
