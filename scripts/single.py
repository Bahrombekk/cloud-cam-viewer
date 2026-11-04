"""View one camera with automatic reconnect.

    python scripts/single.py <SERIAL> [CHANNEL]
"""
import sys

import _bootstrap  # noqa: F401  (sys.path, stdout)

from cloudcam import Camera, CloudCam


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 1
    from cloudcam import viewer

    serial = sys.argv[1]
    channel = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    cam = CloudCam()
    cam.login()
    return viewer.run(cam, cameras=[Camera(serial, channel, serial)])


if __name__ == "__main__":
    raise SystemExit(main())
