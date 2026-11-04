"""Docs and examples must only use obviously fake serials.

Real device serials paired with real verification codes once ended up in the
README; the code is the AES key of the video, so that is a real leak. Any
serial-looking string in a tracked file must be one of these placeholders.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PLACEHOLDERS = {
    "AA1234567", "AB1234567", "AB2345678", "AB3456789", "AB4567890",
    "AB5678901", "BD1234567",
}
SERIAL = re.compile(r"\b[A-Z]{1,2}\d{7,9}\b")


def _tracked():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]
    return [ROOT / f for f in out.stdout.split()]


def test_only_placeholder_serials_in_tracked_files():
    bad = []
    for p in _tracked():
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in set(SERIAL.findall(text)) - PLACEHOLDERS:
            bad.append(f"{p.relative_to(ROOT)}: {m[:2]}…")
    assert not bad, "serial-like strings that are not placeholders:\n" + "\n".join(bad)
