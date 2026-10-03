"""Bola-jarayon -> ota-jarayon hodisalari.

Dekodlovchi proxy alohida JARAYONDA ishlaydi ([[sources.cloud.proxy]]), ya'ni
"kod xato" yoki "kamera oflayn" xabarini ota-jarayonga (`stream_manager`)
yetkazish kerak. Hozir bu vaqtinchalik papkadagi bo'sh bayroq fayllar orqali.

Nega shu yerda: ilgari yo'l IKKI joyda mustaqil quriolardi — proxy'da va
`stream_manager` da. Ikki nusxa bir-biridan sezdirmay uzoqlashishi mumkin
edi (masalan nom qolipi o'zgarsa bayroq umuman ko'rinmay qolardi), shuning
uchun kontrakt bitta joyda.
"""
from __future__ import annotations

import os
import tempfile

KEY_ERROR = "keyerr"      # tasdiqlash kodi noto'g'ri
OFFLINE = "offline"       # kamera/kanal oqim bermayapti


def flag_path(kind: str, serial: str, channel: int = 1) -> str:
    """Bayroq faylining yo'li. `kind` — [[KEY_ERROR]] yoki [[OFFLINE]]."""
    return os.path.join(tempfile.gettempdir(),
                        f"ezviz_{kind}_{serial}_{channel}.flag")


def raise_flag(kind: str, serial: str, channel: int = 1) -> None:
    """Hodisani belgilaydi (xato bo'lsa jim o'tadi — bu diagnostika kanali)."""
    try:
        with open(flag_path(kind, serial, channel), "w") as f:
            f.write("1")
    except OSError:
        pass


def take_flag(kind: str, serial: str, channel: int = 1) -> bool:
    """Bayroq bormi (o'chirmaydi)."""
    return os.path.exists(flag_path(kind, serial, channel))


def clear_flags(serial: str, channel: int = 1) -> None:
    """Yangi urinishdan oldin eski bayroqlarni tozalaydi."""
    for kind in (KEY_ERROR, OFFLINE):
        try:
            os.remove(flag_path(kind, serial, channel))
        except OSError:
            pass
