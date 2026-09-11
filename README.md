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
- 🔓 **Decrypts encrypted streams** with the device verification code (AES). Auto-detects
  transport (**RTP**, **MPEG-PS**, MPEG-TS) and codec (**H.264** & **H.265/HEVC**) per camera —
  no per-device configuration
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
│   ├── vtm_cache.py         # account-level metadata cache + channel gate
│   └── decrypt_proxy.py     # RTP/HEVC + MPEG-PS depacketize, AES decrypt, paced output
├── tests/                   # pytest (cloud is stubbed — no account needed)
├── app.py                   # main multi-camera viewer (grid / fullscreen)
├── single.py                # single-camera viewer
├── set_code.py              # save a verification code
├── check_code.py            # verify a verification code
├── config.example.py        # settings template (copy to config.py)
├── requirements.txt
├── README.md  ·  LICENSE  ·  .gitignore
```

> Run all scripts from the project root (`python app.py`, `python check_code.py ...`).

## Stream quality & speed (what the code does for you)

These are not knobs you need to tune — they are behaviours worth knowing about,
each one measured against real cameras:

- **Per-camera encryption auto-detection.** Some cameras encrypt only IRAP +
  parameter sets (inter P/B slices stay clear), others encrypt *everything*.
  Guessing wrong corrupts every P-frame — the picture recovers on each I-frame
  and breaks up in between. The decryptor decides from the stream itself using a
  *structure* test (slice headers repeat, ciphertext is random; measured
  separation on a real camera: clear **1.00** vs decrypted **0.12**).
  A/B on the same camera (`BD8712447`, 25 s of live stream, same account,
  decoded with `ffmpeg -f hevc`):

  | | assumed "inter is clear" | detected |
  |---|---|---|
  | decoder errors | **439** (`PPS id out of range`, `PPS changed between slices`) | **0** |
  | frames decoded | **8** (0.53 s) | **279** (18.6 s) |

  Two more cameras from the same account after the change: HEVC `BF1916346`
  181 frames / 0 errors, H.264 `BD7793665` 192 frames / 1 error.
- **Jitter buffer.** The cloud does not deliver frames evenly — it sends a whole
  GOP at once and then goes quiet (measured: 491 of 500 frames arrived <10 ms
  apart, the remaining 9 gaps were ~2000 ms). Displaying frames on arrival looks
  like a 2 s freeze followed by fast-forward. `PacedWriter` buffers them *while
  still compressed* (~0.2 MB per GOP) and releases them at the measured frame
  rate. This is also why the phone apps look smooth. Measured at the proxy output
  on a live camera: inter-frame gaps **p50 66 ms, p90 77 ms, max 138 ms, zero
  gaps under 10 ms and zero over 300 ms** — i.e. no bursts and no stalls.
  End-to-end through `stream_manager` + ffmpeg the same camera delivers a steady
  **14.8 fps at 1280×720, 0 reconnects**, first frame in 1.8 s on a warm cache.
- **The cloud reader is never blocked.** Output goes through a bounded queue on
  its own thread; if the consumer stalls, whole frames are dropped **at keyframe
  boundaries** instead of the cloud socket backing up (a blocked socket makes the
  cloud drop packets, which is what corrupted the picture).
- **Account-level metadata cache** (`cloudcam/vtm_cache.py`). The device page
  list and the VTDU token are per *account*, not per camera, yet the library
  re-fetches them on every open. They are now cached to disk (shared across the
  per-camera processes, 1 h TTL). Measured on a live account: page list 1.34 s +
  VTDU token 0.98 s = **2.31 s cold → 0.011 s warm**.
  The cache is invalidated **only when no packet ever arrived** — a mid-stream
  blip must not wipe the cache for every other camera on the account.
- **Channel gate.** An NVR can be online while the channel you asked for does not
  exist; that stream never starts. The cached page list is checked first, so you
  get an immediate, clear error instead of a 20 s timeout.
- **Substream for grids** (`Settings(substream=True)`). Asks the cloud for the
  camera's small profile — measured **640×360 at ~0.11 Mbit/s** versus ~4 Mbit/s
  for the main stream (36× less traffic and CPU) — which is what a grid tile
  actually needs. Use the main stream for fullscreen.

## Development

```bash
pip install -e ".[dev]"
pytest                 # no cloud account needed: the tests stub pyezvizapi
```

The test suite deliberately guards the failure modes above, including ones that
are invisible at runtime (e.g. `-fflags nobuffer` silently discarding the first
parameter sets, or a cache invalidation losing its guard).

## Notes & limits

- First frame takes ~7–10s (cloud handshake + waiting for a keyframe) — normal.
- Playback runs about one GOP (~1–2 s) behind live: that is the jitter buffer,
  and it is the trade the mobile apps make too.
- Source FPS is camera/bandwidth-limited (often ~9–15 fps for HD cloud streams).
- Simultaneous viewing is limited by your CPU/GPU, bandwidth, and the cloud's concurrent-stream limits.
- Built on [`pyezvizapi`](https://pypi.org/project/pyezvizapi/) for the cloud stream transport.

## Disclaimer

This is an independent, unofficial project. Use only with cameras and accounts you own or are
authorized to access. The authors are not affiliated with EZVIZ or Hikvision.

## License

MIT — see [LICENSE](LICENSE).
