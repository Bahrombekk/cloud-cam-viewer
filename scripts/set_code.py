"""Store verification codes without verifying them (case is kept).

Equivalent to `cloudcam code set`:

    python scripts/set_code.py AB2345678 ABCDEF [AB4567890 GHIJKL ...]

Use `scripts/check_code.py` (or `cloudcam code check`) if you are not sure of
the code: it tries the code as given and in upper case.
"""
import sys

import _bootstrap  # noqa: F401  (sys.path, stdout)

from cloudcam.cli import main

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__.strip())
        raise SystemExit(1)
    raise SystemExit(main(["code", "set", *sys.argv[1:]]))
