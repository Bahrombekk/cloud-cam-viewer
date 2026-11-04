"""Shared setup for the helper scripts: find `config.py` and the package.

`config.py` is looked up in the working directory, then in the project root, so
`python scripts/<name>.py` works from either place. The scripts are thin
wrappers; the logic lives in the `cloudcam` package and CLI.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "src"), ROOT, os.getcwd()):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 — eski/yo'naltirilgan stdout
    pass
