"""cloudcam — EZVIZ / Hik-Connect bulutli kameralarga kirish va deshifrlash.

Yuqori darajali API (tavsiya etiladi):

    from cloudcam import CloudCam
    cam = CloudCam(email="...", password="...")
    cam.login()
    stream = cam.open(serial)
    img = stream.snapshot()

Quyi darajali qismlar ham ochiq: CloudClient, StreamManager.
"""

__version__ = "0.2.0"

from .api import Camera, CloudCam, Stream
from .client import CloudClient, MfaRequired
from .settings import Settings
from .stream_manager import StreamManager, load_cam_keys

__all__ = [
    "CloudCam", "Stream", "Camera", "Settings",
    "CloudClient", "MfaRequired",
    "StreamManager", "load_cam_keys",
    "__version__",
]
