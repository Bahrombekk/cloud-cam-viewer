"""Verify a camera verification code against the live stream.

Equivalent to `cloudcam code check`; kept for the old command line:

    python scripts/check_code.py <SERIAL>               # is the camera encrypted?
    python scripts/check_code.py <SERIAL> <CODE>        # verify, save if correct
    python scripts/check_code.py <SERIAL> <CODE> all    # NVR channels 1-4
"""
import sys

import _bootstrap  # noqa: F401  (sys.path, stdout)

from cloudcam.cli import main


def _argv(args):
    out = ["code", "check"] + args[:2]
    if len(args) > 2:
        out += ["--channel", args[2]]
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__.strip())
        raise SystemExit(1)
    raise SystemExit(main(_argv(sys.argv[1:])))
