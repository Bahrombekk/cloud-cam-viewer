# Cloud CCTV Viewer (EZVIZ / Hik-Connect)

View **EZVIZ** and **Hikvision (Hik-Connect)** cloud cameras on your PC — including
**encrypted** streams — using your own account. Multi-camera grid, 24/7 auto-reconnect,
optional NVIDIA GPU decoding.

> ⚠️ **For personal use with your own cameras and account only.** This talks to the
> EZVIZ/Hik-Connect cloud the same way the official apps do (reverse-engineered protocol),
> then decrypts the video locally with your camera's verification code. Respect the
> providers' Terms of Service.

## Features

- 🔐 Login to **EZVIZ** or **Hik-Connect** (auto region detection)
- 📷 Lists all devices & **NVR channels** as separate cameras (e.g. 8 NVRs → 32 cameras)
- 🔓 **Decrypts encrypted streams** with the device verification code (AES) — handles both
  **RTP/HEVC** (NVR channels) and **MPEG-PS** (IP cameras) automatically
- 🟢 Auto-detects clear vs encrypted vs wrong-code per camera
- ⚡ Optional **GPU (NVIDIA NVDEC)** decoding with software fallback
- ♻️ 24/7 — auto-reconnect, periodic token refresh
- 🖥️ Grid view, fullscreen (keys `1`–`9`), live status (FPS, reconnects)
- 🛠️ Tools to **save** (`set_code.py`) and **verify** (`check_code.py`) verification codes

## How it works

```
Cloud login (your account)  →  device/stream metadata  →  connect to cloud VTM relay
   →  receive RTP/HEVC or MPEG-PS packets  →  decrypt NAL bodies (AES, key = verification code)
   →  remux to MPEG-TS  →  decode & display
```

The camera **verification code** (the code you set when adding the device to the app, usually
on the device label) is the AES key. Live viewing requires no subscription.

## Requirements

- Python 3.10+
- [FFmpeg](https://ffmpeg.org/) on your `PATH`
- (Optional) NVIDIA GPU + drivers for hardware decoding
- Python packages: see `requirements.txt`

## Setup

```bash
git clone <your-repo-url>
cd cloud-cam-viewer

python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt

cp config.example.py config.py     # then edit config.py
```

Edit `config.py`:

```python
PLATFORM = "hikconnect"   # or "ezviz"
EMAIL    = "you@example.com"
PASSWORD = "your_password"
```

## Usage

```bash
python app.py                       # list cameras, pick, view (grid/fullscreen)
python single.py <SERIAL>           # one camera, fullscreen

# Encrypted cameras — manage verification codes:
python check_code.py <SERIAL> <CODE>        # verify a code (saves it if correct)
python check_code.py <SERIAL> <CODE> all    # all 4 NVR channels
python set_code.py   <SERIAL> <CODE>        # save a code directly
```

**Controls (in the video window):** `q` quit · `g` grid · `1`–`9` fullscreen camera · `s` status

For an NVR, the verification code is per-device and applies to all its channels.

## Project layout

```
cloud-cam-viewer/
├── cloudcam/                # core library (package)
│   ├── client.py            # cloud login (EZVIZ & Hik-Connect), device list
│   ├── stream_manager.py    # per-camera threads, reconnect, token refresh, GPU
│   └── decrypt_proxy.py     # RTP/HEVC + MPEG-PS depacketize, AES decrypt, MPEG-TS
├── app.py                   # main multi-camera viewer (grid / fullscreen)
├── single.py                # single-camera viewer
├── set_code.py              # save a verification code
├── check_code.py            # verify a verification code
├── config.example.py        # settings template (copy to config.py)
├── requirements.txt
├── README.md  ·  LICENSE  ·  .gitignore
```

> Run all scripts from the project root (`python app.py`, `python check_code.py ...`).

## Notes & limits

- First frame takes ~7–10s (cloud handshake + waiting for a keyframe) — normal.
- Source FPS is camera/bandwidth-limited (often ~9–15 fps for HD cloud streams).
- Simultaneous viewing is limited by your CPU/GPU, bandwidth, and the cloud's concurrent-stream limits.
- Built on [`pyezvizapi`](https://pypi.org/project/pyezvizapi/) for the cloud stream transport.

## Disclaimer

This is an independent, unofficial project. Use only with cameras and accounts you own or are
authorized to access. The authors are not affiliated with EZVIZ or Hikvision.

## License

MIT — see [LICENSE](LICENSE).
