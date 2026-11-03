"""Where cloudcam keeps its per-account files."""

from __future__ import annotations

import os
import tempfile

from cloudcam.settings import get_active


def cache_dir() -> str:
    """Kesh fayllari joyi — token fayli yonida (u ham hisobga tegishli)."""
    tok = get_active().token_file or "token.json"
    d = os.path.dirname(os.path.abspath(tok))
    return d or tempfile.gettempdir()
