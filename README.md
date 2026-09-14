# Cloud CCTV Viewer (EZVIZ / Hik-Connect)

View **EZVIZ** and **Hikvision (Hik-Connect)** cloud cameras on your PC — including
**encrypted** streams — using your own account. Multi-camera grid, 24/7 auto-reconnect,
optional NVIDIA GPU decoding.

> ⚠️ **For personal use with your own cameras and account only.** This talks to the
> EZVIZ/Hik-Connect cloud the same way the official apps do (reverse-engineered protocol),
> then decrypts the video locally with your camera's verification code. Respect the
> providers' Terms of Service.

## Features

- 🔐 Login to **EZVIZ** or **Hik-Connect** (auto region detection), with the
  session **reused across restarts** — no repeated CAPTCHA / "new device" 2FA
- 🔑 **Fetches verification codes from the cloud** (`cloudcam keys`) — no more
  reading codes off device labels
- 📷 Lists all devices & **NVR channels** as separate cameras (e.g. 8 NVRs → 32 cameras)
- 🔓 **Decrypts encrypted streams** with the device verification code (AES). Auto-detects
  transport (**RTP**, **MPEG-PS**, MPEG-TS) and codec (**H.264** & **H.265/HEVC**) per camera —
  no per-device configuration
- 🟢 Auto-detects clear vs encrypted vs wrong-code per camera
- 📉 **Substream for grids**, with an `auto` mode that falls back to the main
  stream on cameras that have no substream
- ⚡ Optional **GPU (NVIDIA NVDEC)** decoding with software fallback
- ♻️ 24/7 — auto-reconnect, frozen-stream watchdog, age-based token refresh
- 🖥️ Grid view, fullscreen (keys `1`–`9`), live status (FPS, reconnects)
- 🛠️ Tools to **save** (`set_code.py`) and **verify** (`check_code.py`) codes by hand

## How it works

```
Cloud login (session reused)  →  device/stream metadata (cached per account)
   →  connect to cloud VTM relay  →  receive RTP/HEVC, MPEG-PS or MPEG-TS packets
   →  decrypt NAL bodies (AES, key = verification code)  →  Annex-B over local HTTP
   →  jitter buffer (paced at the measured frame rate)  →  ffmpeg  →  decode & display
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

pip install -e .            # also installs the `cloudcam` command
pip install -e ".[viewer]"  # ...add OpenCV if you want the video window

cp config.example.py config.py     # then edit config.py
```

Edit `config.py`:

```python
PLATFORM = "hikconnect"   # or "ezviz"
EMAIL    = "you@example.com"
PASSWORD = "your_password"
```

Credentials can also come from `CLOUDCAM_EMAIL` / `CLOUDCAM_PASSWORD` (or
`--email` / `--password`) instead of `config.py` — every setting has a
`CLOUDCAM_*` environment variable.

## Usage

```bash
python app.py                       # list cameras, pick, view (grid/fullscreen)
python single.py <SERIAL>           # one camera, fullscreen

cloudcam cameras                    # list cameras
cloudcam snapshot <SERIAL>          # save one JPEG
cloudcam view                       # grid viewer (needs the `viewer` extra)
```

**Encrypted cameras — get the verification codes.** The cloud knows them, so
ask it instead of reading codes off device labels:

```bash
cloudcam keys                       # all devices; prints what it saved
```

The first run asks the cloud to elevate the session and emails you a 2FA code.
Enter it once — every remaining camera is then fetched without another code:

```bash
cloudcam keys --code 123456
```

Codes land in `cam_keys.json`. Manual entry still works when you already know a
code, or when the account cannot elevate:

```bash
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
│   ├── api.py               # high-level facade: CloudCam / Stream / Camera
│   ├── cli.py               # `cloudcam` command (cameras, keys, snapshot, view)
│   ├── client.py            # cloud login, session reuse, verification-code fetch
│   ├── identity.py          # per-install terminal id (featureCode)
│   ├── keys.py              # verification-code store + cloud fetch (2FA flow)
│   ├── settings.py          # Settings: config.py / CLOUDCAM_* env / defaults
│   ├── stream_manager.py    # per-camera processes, reconnect, watchdog, GPU
│   ├── vtm_cache.py         # account-level metadata cache + channel gate
│   ├── viewer.py            # OpenCV grid viewer (optional `viewer` extra)
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

## Library API

The package works without `config.py` and without OpenCV:

```python
from cloudcam import CloudCam

cam = CloudCam(email="you@example.com", password="…", platform="hikconnect")
cam.login()                      # resumes a saved session when there is one

for c in cam.cameras():
    print(c.serial, c.channel, c.name)

cam.fetch_keys()                 # verification codes from the cloud (once)

stream = cam.open(serial, channel=1)
stream.wait_first_frame()
jpeg = stream.snapshot()         # bytes
for frame in stream.frames():    # numpy BGR
    ...
stream.close()
cam.close()
```

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
- **Substream for grids** (`STREAM_MODE = "sub"`). Asks the cloud for the
  camera's small profile — measured **640×360 at ~0.11 Mbit/s** versus ~4 Mbit/s
  for the main stream (36× less traffic and CPU) — which is what a grid tile
  actually needs. Use the main stream for fullscreen.
- **`STREAM_MODE = "auto"`.** Not every camera has a substream (battery models,
  some NVR channels): asking for one gets you a black tile. Auto asks for the
  substream first and falls back to the main stream if nothing arrives within
  6 s, so a mixed account needs no per-camera configuration.
- **The session is reused, not re-created.** Logging in on every start is what
  makes the cloud demand a CAPTCHA (code 1015) or "new device" 2FA — a 24/7
  process that restarts hits this constantly. The saved session is resumed,
  refreshed by *age* when stale (not on a fixed hourly timer, which is wrong
  after sleep/reconnect), and a full login is the last resort; failed logins
  back off 60 s → 30 min instead of hammering.
- **Per-install terminal id.** `featureCode` identifies the "terminal" to the
  cloud, and every copy of this project used to send the *same* hardcoded one —
  so unrelated users shared one terminal, and one person's activity triggered
  the other's rate limits. It is now derived from the machine and persisted to
  `feature_code.txt`, so it is stable across restarts but unique per install.

## Development

```bash
pip install -e ".[dev]"
pytest                 # no cloud account needed: the tests stub pyezvizapi
```

The test suite deliberately guards the failure modes above, including ones that
are invisible at runtime: `-fflags nobuffer` silently discarding the first
parameter sets, a cache invalidation losing its guard, the shared hardcoded
`featureCode` creeping back in, the 2FA code being re-sent per camera instead of
once per session, or settings not reaching the per-camera child processes.

## Notes & limits

- First frame takes ~7–10s (cloud handshake + waiting for a keyframe) — normal.
- Playback runs about one GOP (~1–2 s) behind live: that is the jitter buffer,
  and it is the trade the mobile apps make too.
- Source FPS is camera/bandwidth-limited (often ~9–15 fps for HD cloud streams).
- Simultaneous viewing is limited by your CPU/GPU, bandwidth, and the cloud's concurrent-stream limits.
- `cloudcam keys` needs the account to allow 2FA elevation. Some accounts
  (shared devices, sub-accounts) will not return codes — enter them manually then.
- Selective-encryption auto-detection currently applies to the **RTP** path.
  On the MPEG-PS path every NAL body is decrypted.
- Built on [`pyezvizapi`](https://pypi.org/project/pyezvizapi/) for the cloud stream transport.

## Disclaimer

This is an independent, unofficial project. Use only with cameras and accounts you own or are
authorized to access. The authors are not affiliated with EZVIZ or Hikvision.

## License

MIT — see [LICENSE](LICENSE).
