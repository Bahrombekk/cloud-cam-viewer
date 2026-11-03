# cloudcam — EZVIZ / Hik-Connect cloud cameras in Python

[![CI](https://github.com/Bahrombekk/cloud-cam-viewer/actions/workflows/ci.yml/badge.svg)](https://github.com/Bahrombekk/cloud-cam-viewer/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Release](https://img.shields.io/github/v/release/Bahrombekk/cloud-cam-viewer?include_prereleases)](https://github.com/Bahrombekk/cloud-cam-viewer/releases)
[![Stars](https://img.shields.io/github/stars/Bahrombekk/cloud-cam-viewer?style=social)](https://github.com/Bahrombekk/cloud-cam-viewer/stargazers)

**Watch and decrypt your EZVIZ / Hik-Connect cameras without the vendor app, even
when the stream is encrypted and the camera has no RTSP port open.**
`cloudcam` logs in to the cloud the way the official app does, decrypts the video
on your machine with the camera's verification code, and hands you plain
H.264/H.265 frames as a CLI, a multi-camera viewer or a Python library.
Hikvision devices can skip the cloud completely over **ISUP 5.0**.

<!-- DEMO: replace with a 20–40 s recording of `python app.py` (grid → fullscreen).
<p align="center"><img src="docs/demo.gif" alt="cloudcam grid viewer" width="800"></p>
-->

> ⚠️ **For your own cameras and account only.** Unofficial, reverse-engineered
> client; not affiliated with EZVIZ or Hikvision. Respect the providers' Terms of Service.

## Why this exists

Millions of EZVIZ and Hikvision cameras are only reachable **through the cloud**:
they sit behind CGNAT or someone else's router, RTSP is disabled, or the camera is
a cloud-only model. The vendor app is the only way to watch them, and its stream is
AES-encrypted with the camera's verification code. So you cannot:

- record the camera in your own NVR, Frigate or Home Assistant;
- run your own computer-vision / AI on the video;
- watch 30 cameras from 8 NVRs on one screen without the app.

`cloudcam` closes that gap: cloud stream → local decryption → frames you control.

| | Vendor app | HA EZVIZ integration | go2rtc / MediaMTX | **cloudcam** |
|---|:---:|:---:|:---:|:---:|
| Camera with no local RTSP (cloud only) | ✅ | ❌ needs LAN RTSP | ❌ needs a source | ✅ |
| Encrypted stream decrypted locally | ✅ (inside the app) | ❌ | ❌ | ✅ |
| Video as frames in your own code | ❌ | ❌ | ✅ (from RTSP) | ✅ |
| Hikvision ISUP 5.0, no cloud at all | ❌ | ❌ | ❌ | ✅ (via bridge) |
| RTSP / WebRTC restream | ❌ | ✅ | ✅ | 🚧 [v0.4](#roadmap) |

*`cloudcam` is meant to sit **in front of** go2rtc / MediaMTX / Frigate, not to replace them.*

## Features

- 🔐 **EZVIZ and Hik-Connect** login with auto region detection; the session is
  **reused across restarts**, so you don't hit repeated CAPTCHA or "new device" 2FA
- 🔓 **Decrypts encrypted streams** locally (AES, key = verification code).
  **RTP, MPEG-PS and MPEG-TS** transports, **H.264 and H.265/HEVC**, all encryption
  variants. Everything is detected per camera, so there is nothing to configure.
- 🔑 **Gets verification codes from the cloud** (`cloudcam keys`), so you don't have to read device labels
- 📷 Every device **and every NVR channel** listed as its own camera
- 📡 **ISUP 5.0**: Hikvision devices dial out to *your* server; first frame in **0.33 s**
- 📉 **Substream for grids** (36× less traffic) with automatic fallback to the main stream
- ⚡ Optional **NVIDIA NVDEC** decoding with software fallback
- ♻️ Built for 24/7: auto-reconnect, frozen-stream watchdog, a jitter buffer that smooths cloud bursts
- 🧪 150+ tests, and none of them need a cloud account

## Quick start

Needs **Python 3.10+** and **[FFmpeg](https://ffmpeg.org/download.html)** on your `PATH`.

```bash
pip install "cloudcam[viewer] @ git+https://github.com/Bahrombekk/cloud-cam-viewer"

export CLOUDCAM_PLATFORM=hikconnect        # or ezviz
export CLOUDCAM_EMAIL=you@example.com
export CLOUDCAM_PASSWORD='your_password'   # Windows: set CLOUDCAM_EMAIL=... etc.

cloudcam cameras      # 1. what does the account have?
cloudcam keys         # 2. fetch verification codes (encrypted cameras need them)
cloudcam view         # 3. grid viewer: 1–9 fullscreen · g grid · s status · q quit
```

```
$ cloudcam cameras
BD7793665  ch1  Warehouse
BD8712447  ch1  Office - Entrance
BD8712447  ch2  Office - Yard
BF1916346  ch1  Storage
```

Grab a single frame without a window:

```bash
cloudcam snapshot BD8712447 --channel 2 --out yard.jpg
```

Or use it from Python, with no config file and no OpenCV needed:

```python
from cloudcam import CloudCam

cam = CloudCam(email="you@example.com", password="…", platform="hikconnect")
cam.login()
stream = cam.open("BD8712447", channel=1)
for frame in stream.frames():      # numpy BGR arrays
    ...                            # your detector / recorder here
```

## How it works

```
cloud login (session reused) ─▶ device + stream metadata (cached per account)
  ─▶ cloud VTM relay ─▶ RTP / MPEG-PS / MPEG-TS packets
  ─▶ AES-decrypt NAL bodies (key = verification code) ─▶ Annex-B over local HTTP
  ─▶ jitter buffer (paced at the measured frame rate) ─▶ ffmpeg ─▶ frames
```

Live viewing needs no cloud subscription. The details, with measurements from real
cameras, are in **[docs/internals.md](docs/internals.md)**.

## Compatibility

What has been verified on real hardware so far. **[Add your camera →](https://github.com/Bahrombekk/cloud-cam-viewer/issues/new?template=camera_compat.yml)**

| Source | Device | Transport / codec | Encryption | Status |
|---|---|---|---|:---:|
| Hik-Connect cloud | NVR, multiple channels | RTP · H.265 | all slices encrypted | ✅ |
| Hik-Connect cloud | IP camera | RTP · H.265 | encrypted | ✅ |
| Hik-Connect cloud | IP camera | RTP · H.264 | encrypted | ✅ |
| Hik-Connect cloud | IP camera | MPEG-PS · H.264/H.265 | selective / whole-NAL | ✅ |
| ISUP 5.0 | DS-TCG406-E, fw V5.4.0 | RTSP via bridge | none | ✅ |
| EZVIZ cloud | — | — | — | ❔ reports wanted |

## Usage in detail

### Configuration

Every setting can come from a `CLOUDCAM_*` environment variable, a CLI flag
(`--email`, `--password`, `--platform`) or a `config.py` in the working directory:

```bash
git clone https://github.com/Bahrombekk/cloud-cam-viewer.git && cd cloud-cam-viewer
pip install -e ".[viewer]"
cp config.example.py config.py      # then edit PLATFORM / EMAIL / PASSWORD
python app.py                       # grid viewer   ·   python single.py <SERIAL>
```

Set `CLOUDCAM_LANG=uz` for Uzbek messages.

### Encrypted cameras: getting the verification codes

Without a code an encrypted camera shows **"Wrong verification code! Check
cam_keys.json"** and a black tile: the stream is AES-encrypted and the verification
code is the key. The cloud knows the codes, so ask it rather than reading them
off device labels.

**Worked example.** First run — the cloud wants the session elevated, so it
emails a 2FA code and stops:

```
$ cloudcam keys
2FA code sent to b***@gmail.com. Run again:
  cloudcam keys --code <CODE>
```

Enter that code once. It elevates the session, and every remaining camera is
fetched without another code:

```
$ cloudcam keys --code 123456
BD7793665  KJWQTZ
BD8712447  SCXYKW
BF1916346  ABCDEF
```

Re-running is cheap — codes already stored are not re-fetched:

```
$ cloudcam keys
(3 codes already stored — use --overwrite to re-fetch)
```

A camera that is offline fails on its own without stopping the rest:

```
$ cloudcam keys
BD8712447  SCXYKW
BF1916346  ERROR: Device offline (2009)
```

The result is `cam_keys.json` — a flat serial → code map (see
[`cam_keys.example.json`](cam_keys.example.json)):

```json
{
  "BD7793665": "KJWQTZ",
  "BD8712447": "SCXYKW",
  "BF1916346": "ABCDEF"
}
```

For an NVR the code belongs to the **device**, so one entry covers all of its
channels — you do not add `ch1`, `ch2`, … separately.

Now `python app.py` decodes those cameras. Manual entry still works when you
already know a code, or when the account cannot elevate to 2FA:

```bash
python check_code.py <SERIAL> <CODE>        # verify a code (saves it if correct)
python check_code.py <SERIAL> <CODE> all    # all 4 NVR channels
python set_code.py   <SERIAL> <CODE>        # save a code directly
```

Codes are the AES key, so they are **case-sensitive** and stored exactly as
typed. `check_code.py` tries the code as given and, if that is wrong, once more in
upper case (label codes are upper case; a code changed in the app may not be).

`check_code.py` tells you which case you are in (correct, wrong code, not
encrypted, or no keyframe yet):

```
$ python check_code.py BD8712447 SCXYKW
🔎 Checking: BD8712447 ch1 (code: SCXYKW) ...
✅ BD8712447 ch1: CODE IS CORRECT! Stream decrypted.
   💾 Saved to cam_keys.json: BD8712447 -> SCXYKW
```

**Viewer controls:** `q` quit · `g` grid · `1`–`9` fullscreen camera · `s` status


### ISUP 5.0: cameras that dial out to you

The cloud path works, but it runs on the cloud's terms: 4–14 s to the first
frame, a cap on how many streams you may open, and occasional corruption. With
**ISUP 5.0** the Hikvision device skips the cloud and connects *to your server*
instead — no VTM relay, no verification code, no decryption.

Measured on a live camera (DS-TCG406-E fw V5.4.0):

| | Cloud | ISUP |
|---|---|---|
| "send video" → first packet | 4–14 s | **0.23 s** |
| First clean frame (with transcode) | ~7–10 s | **0.33–0.38 s** |
| Concurrent-stream cap | 16 | none |

**The ISUP protocol itself cannot be spoken from Python** — registration,
heartbeat and stream negotiation live inside Hikvision's closed SDK. So it is
handled by an external bridge process, the same way video decoding is handled by
an external `ffmpeg`. This library drives that bridge and consumes the plain
RTSP it publishes:

```
camera ──ISUP 17660/17661──▶ isup-bridge ──RTSP (no transcode)──▶ cloudcam
```

The bridge is optional. Without it, everything above still works — the cloud
path does not depend on it.

```bash
cloudcam isup devices                       # what has connected
cloudcam isup keys AA1234567=SECRET --no-default
```

```
$ cloudcam isup devices
AA1234567  10.0.0.8        V5.4.0       channels:2 online:93s
    ch1 main live  2.5 MB
    ch2 sub  idle  0.0 MB
```

From Python:

```python
cam = CloudCam()                      # no cloud account needed for ISUP
cam.isup.set_keys({"AA1234567": "SECRET"}, default=False)

for c in cam.isup_cameras():
    print(c.serial, c.channel, c.name)

stream = cam.open_isup("AA1234567", channel=1)   # asks the bridge to open it
frame = stream.frame()
```

`open_isup()` adds the stream to the bridge's "want" list and `close()` clears
it, so the device only pushes video while someone is watching — the main stream
is ~4 Mbit/s against ~0.3 Mbit/s for the substream, and at 0.23 s to open there
is no reason to keep it running.

Point the library at your bridge with `ISUP_URL`, `ISUP_TOKEN` and
`ISUP_RTSP_BASE` in `config.py` (or `CLOUDCAM_ISUP_*`). Building the bridge
needs Hikvision's ISUP SDK, which cannot be redistributed.

### Python API

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

## Notes & limits

- First frame takes ~7–10s (cloud handshake + waiting for a keyframe) — normal.
- Playback runs about one GOP (~1–2 s) behind live: that is the jitter buffer,
  and it is the trade the mobile apps make too.
- Source FPS is camera/bandwidth-limited (often ~9–15 fps for HD cloud streams).
- Simultaneous viewing is limited by your CPU/GPU, bandwidth, and the cloud's concurrent-stream limits.
- `cloudcam keys` needs the account to allow 2FA elevation. Some accounts
  (shared devices, sub-accounts) will not return codes — enter them manually then.


## Roadmap

- **v0.4: `cloudcam serve`.** Every camera becomes a local **RTSP / WebRTC** stream
  through go2rtc / MediaMTX, with one upstream cloud session shared by any number of
  viewers. You can then use it as a source in Frigate or Home Assistant, plus a Docker image.
- **v0.5: reproducible benchmarks.** First-frame p50/p95, recovery after network loss, and CPU per camera.
- Published on PyPI (`pip install cloudcam`).

⭐ **Star the repo** to follow along, and [open an issue](https://github.com/Bahrombekk/cloud-cam-viewer/issues)
if your setup needs something else. See [CHANGELOG.md](CHANGELOG.md) for what has shipped.

## Contributing

Compatibility reports, bug reports and PRs are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
pip install -e ".[dev]"
pytest          # no cloud account or network needed
ruff check .
```

The test suite guards failure modes that are invisible at runtime, for example:
- `-fflags nobuffer` silently discarding the first parameter sets;
- a cache invalidation losing its guard;
- the 2FA code being re-sent per camera instead of once per session;
- the verification code leaking onto the command line;
- an audio packet between video fragments being mistaken for packet loss;
- a proxy outliving its parent.

Security issues: please report privately, see [SECURITY.md](SECURITY.md).

## Disclaimer

This is an independent, unofficial project. Use it only with cameras and accounts you
own or are authorized to access. The authors are not affiliated with EZVIZ or Hikvision.
The cloud stream transport (`cloudcam/vtm/`) is adapted from
[`pyezvizapi`](https://github.com/RenierM26/pyEzvizApi) 1.0.5.0 by Renier Moorcroft,
Apache License 2.0, see [`cloudcam/vtm/LICENSE-pyezvizapi`](cloudcam/vtm/LICENSE-pyezvizapi).

## License

MIT, see [LICENSE](LICENSE). `cloudcam/vtm/` contains code adapted from `pyezvizapi`
under the Apache License 2.0 (notice and full text in that directory).
