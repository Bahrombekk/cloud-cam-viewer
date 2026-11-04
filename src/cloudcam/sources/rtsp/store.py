"""Saved RTSP cameras: ``rtsp_cameras.json`` in the data directory (0600).

    {"gate": {"url": "rtsp://admin:...@192.168.1.64:554/Streaming/Channels/101",
              "sub_url": "rtsp://admin:...@192.168.1.64:554/Streaming/Channels/102"}}

The URLs carry the camera password, so the file is written atomically with
owner-only permissions, like ``cam_keys.json``; anything printed goes through
:func:`cloudcam.sources.rtsp.probe.redact_url`.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from cloudcam.core.fsutil import harden, write_private
from cloudcam.i18n import t
from cloudcam.settings import default_rtsp_file, get_active
from cloudcam.sources.rtsp.probe import split_rtsp_url

NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


class RtspStoreError(ValueError):
    """The file exists but cannot be read: it is never overwritten."""


@dataclass(frozen=True)
class RtspCamera:
    name: str
    url: str
    sub_url: str = ""


def _path(path: str | None) -> str:
    return path or get_active().rtsp_file or default_rtsp_file()


def check_name(name: str) -> str:
    if not NAME_RE.fullmatch(name or ""):
        raise ValueError(t(f"camera name {name!r}: use 1-64 of A-Z a-z 0-9 _ -",
                           f"kamera nomi {name!r}: 1-64 ta A-Z a-z 0-9 _ - belgisi"))
    return name


def load(path: str | None = None, *, strict: bool = False) -> list[RtspCamera]:
    """Saved cameras, sorted by name. ``strict`` raises on an unreadable file."""
    p = _path(path)
    if not os.path.exists(p):
        return []
    harden(p)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        cams = []
        for name, v in data.items():
            if not NAME_RE.fullmatch(str(name)):
                continue
            url = v.get("url", "") if isinstance(v, dict) else str(v)
            sub = v.get("sub_url", "") if isinstance(v, dict) else ""
            split_rtsp_url(url)                     # validates the scheme and host
            if sub:
                split_rtsp_url(sub)
            cams.append(RtspCamera(str(name), url, sub))
    except (OSError, ValueError, AttributeError) as err:
        if strict:
            raise RtspStoreError(t(f"{p} is unreadable ({err}): fix or remove it; "
                                   "not overwriting",
                                   f"{p} o'qilmadi ({err}): tuzating yoki o'chiring; "
                                   "ustidan yozilmaydi")) from err
        return []
    return sorted(cams, key=lambda c: c.name)


def save(cams: list[RtspCamera], path: str | None = None) -> None:
    data = {c.name: ({"url": c.url, "sub_url": c.sub_url} if c.sub_url else {"url": c.url})
            for c in sorted(cams, key=lambda c: c.name)}
    write_private(_path(path), json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def add(name: str, url: str, sub_url: str = "", path: str | None = None) -> RtspCamera:
    """Add or replace a camera by name."""
    check_name(name)
    split_rtsp_url(url)
    if sub_url:
        split_rtsp_url(sub_url)
    cams = [c for c in load(path, strict=True) if c.name != name]
    cam = RtspCamera(name, url, sub_url)
    save(cams + [cam], path)
    return cam


def remove(name: str, path: str | None = None) -> bool:
    cams = load(path, strict=True)
    left = [c for c in cams if c.name != name]
    if len(left) == len(cams):
        return False
    save(left, path)
    return True
