"""Tiny message localisation: English by default, Uzbek with ``CLOUDCAM_LANG=uz``.

Every user-visible string is written inline as ``t("English", "O'zbekcha")`` so
both texts sit next to each other in the source. The environment is read on each
call, which keeps it trivially testable and lets a running process switch.
"""
from __future__ import annotations

import os

LANG_ENV = "CLOUDCAM_LANG"


def lang() -> str:
    """Current UI language: ``"uz"`` or ``"en"``."""
    return "uz" if os.environ.get(LANG_ENV, "").strip().lower().startswith("uz") else "en"


def t(en: str, uz: str) -> str:
    """Return the message in the current UI language."""
    return uz if lang() == "uz" else en
