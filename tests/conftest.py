"""Testlar uchun umumiy tayyorgarlik.

`cloudcam` bulut kutubxonasiga (`pyezvizapi`) bog'liq, lekin deshifrlash
mantiqini sinash uchun bulut KERAK EMAS. Shuning uchun testlarda o'sha modullar
"qo'g'irchoq" (stub) bilan almashtiriladi: shunda testlar tarmoqsiz, hisobsiz va
bog'liqliklarni o'rnatmasdan ishlaydi.
"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _install_pyezvizapi_stub() -> None:
    if "pyezvizapi" in sys.modules:
        return
    try:
        import pyezvizapi  # noqa: F401  — haqiqiysi bor bo'lsa, uni ishlatamiz
        return
    except Exception:
        pass
    _stub("pyezvizapi")
    _stub("pyezvizapi.cloud_stream",
          get_vtm_page_list=lambda *a, **k: {},
          get_vtdu_token_v2=lambda *a, **k: {},
          open_cloud_stream=lambda *a, **k: None,
          build_vtm_url=lambda *a, **k: "")
    _stub("pyezvizapi.client", EzvizClient=object)
    _stub("pyezvizapi.stream", rtp_payload=lambda pkt: pkt)
    _stub("pyezvizapi.exceptions", PyEzvizError=Exception)


_install_pyezvizapi_stub()
