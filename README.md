# cloudcam

[![CI](https://github.com/Bahrombekk/cloud-cam-viewer/actions/workflows/ci.yml/badge.svg)](https://github.com/Bahrombekk/cloud-cam-viewer/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT%20AND%20Apache--2.0-green)](LICENSE)
[![Release](https://img.shields.io/github/v/release/Bahrombekk/cloud-cam-viewer?include_prereleases)](https://github.com/Bahrombekk/cloud-cam-viewer/releases)

Python client for EZVIZ and Hik-Connect cloud cameras. It logs in to the cloud the
way the official app does, decrypts the video locally with the camera's verification
code, and delivers plain H.264/H.265 frames through a CLI, a multi-camera viewer or a
Python API. Hikvision devices can also stream without the cloud over ISUP 5.0 through
the included [`isup-bridge`](isup-bridge/), which needs Hikvision's ISUP SDK (see
[ISUP 5.0](#isup-50)).

> **Note.** This is an unofficial, reverse-engineered client, not affiliated with
> EZVIZ or Hikvision. Use it only with cameras and accounts you own or are authorized
> to access, and in accordance with the providers' Terms of Service.

## Contents

- [Overview](#overview)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Verification codes](#verification-codes)
- [Command-line reference](#command-line-reference)
- [Python API](#python-api)
- [ISUP 5.0](#isup-50)
- [Architecture](#architecture)
- [Compatibility](#compatibility)
- [Limitations](#limitations)
- [Security](#security)
- [Roadmap](#roadmap)
- [Development](#development)
- [License](#license)

## Overview

Many EZVIZ and Hikvision cameras are reachable only through the vendor cloud: they
sit behind CGNAT or a third-party router, RTSP is disabled, or the model is
cloud-only. The vendor app is then the only way to watch them, and its stream is
AES-encrypted with the camera's verification code. As a result the camera cannot be
recorded by your own NVR, Frigate or Home Assistant, cannot feed your own
computer-vision pipeline, and cannot be shown alongside cameras from other NVRs.

`cloudcam` removes that restriction: cloud stream, local decryption, frames under
your control.

| Capability | Vendor app | go2rtc / MediaMTX | cloudcam |
|---|:---:|:---:|:---:|
| Cloud-only camera (no local RTSP) | Yes | No (needs a source) | Yes |
| Encrypted stream decrypted locally | Inside the app | No | Yes |
| Frames available to your own code | No | Yes (from RTSP) | Yes |
| RTSP / WebRTC restream | No | Yes | Planned ([v0.4](#roadmap)) |

`cloudcam` is designed to sit in front of go2rtc, MediaMTX or Frigate, not to
replace them.

## Features

- **Login** to EZVIZ and Hik-Connect with automatic region detection. The session is
  saved and reused across restarts, which avoids repeated CAPTCHA and "new device"
  2FA prompts.
- **Local decryption** of encrypted streams (AES, key = verification code) over RTP,
  MPEG-PS and MPEG-TS, for H.264 and H.265/HEVC. Transport, codec and encryption
  variant are detected per camera; nothing needs to be configured.
- **Verification codes from the cloud** (`cloudcam keys`), so codes do not have to be
  read from device labels, and `cloudcam code check` to verify a code by hand.
- **Device status** for up to 50 devices per request: online state, NVR disk state and
  capacity, encryption. Also motion events with decrypted pictures, live push events
  (MQTT), recordings on the SD card or NVR, and PTZ control.
- **NVR channels** are listed as individual cameras; `cloudcam cameras --check` probes
  which channels actually stream.
- **Substream** (about 36 times less traffic than the main stream) with
  `stream_mode = "auto"`, which falls back to the main stream when a camera has none.
- **NVIDIA NVDEC** decoding when available, with automatic software fallback.
- **24/7 operation**: automatic reconnect, frozen-stream watchdog, and a jitter buffer
  that evens out the cloud's burst delivery.
- **ISUP 5.0** (experimental): a C++ bridge in [`isup-bridge/`](isup-bridge/) and its
  client (`IsupBridge`, `cloudcam isup`, `CloudCam.open_isup()`).
- **Test suite** of more than 250 tests that run without a cloud account or network.

## Requirements

- Python 3.10 or newer
- [FFmpeg](https://ffmpeg.org/download.html) on `PATH`, for anything that opens a
  stream (`snapshot`, `view`, `CloudCam.open()`); listing, keys, status and events do
  not need it
- Optional: OpenCV (`viewer` extra) for the viewer and JPEG snapshots
- Optional: paho-mqtt (`events` extra) for live push events

## Installation

The package is not on PyPI yet; install it from GitHub:

```bash
pip install "cloudcam[viewer] @ git+https://github.com/Bahrombekk/cloud-cam-viewer"
```

| Extra | Installs | Needed for |
|---|---|---|
| `viewer` | `opencv-python` | `cloudcam view`, `cloudcam snapshot`, `Stream.snapshot()` |
| `events` | `paho-mqtt` | `cloudcam events --watch`, `CloudCam.push()` |
| `dev` | `pytest`, `ruff` | running the tests and the linter |

Without extras the package depends on `numpy`, `requests` and `pycryptodome` only.

## Quick start

```bash
export CLOUDCAM_PLATFORM=hikconnect        # or: ezviz
export CLOUDCAM_EMAIL=you@example.com
export CLOUDCAM_PASSWORD='your_password'

cloudcam cameras      # list the cameras on the account
cloudcam keys         # fetch verification codes (asks for an emailed 2FA code once)
cloudcam view         # grid viewer with every camera
```

On Windows use `set CLOUDCAM_EMAIL=...` (cmd) or `$env:CLOUDCAM_EMAIL = "..."`
(PowerShell).

```
$ cloudcam cameras
AB1234567  ch1   Warehouse
AB2345678  ch1   Office - Entrance
AB2345678  ch2   Office - Yard
AB3456789  ch1   Storage
```

Save a single frame without opening a window:

```bash
cloudcam snapshot AB2345678 --channel 2 --out yard.jpg
```

Republish a camera to an RTSP server you already run (MediaMTX, go2rtc), so that
Frigate, VLC or Home Assistant can read it. The video is copied, not re-encoded:

```bash
cloudcam stream AB2345678 --channel 2 --rtsp rtsp://127.0.0.1:8554/cam/AB2345678_2
```

## Configuration

Settings are resolved in this order, highest priority first:

1. Arguments passed in code (`CloudCam(email=..., ...)`, `Settings(...)`) or the global
   CLI options `--email`, `--password`, `--platform`, which go before the command
   (`cloudcam --platform ezviz cameras`)
2. Environment variables `CLOUDCAM_<NAME>`
3. A `config.py` in the working directory (`<NAME>` in upper case; see
   [`config.example.py`](config.example.py))
4. Built-in defaults

| Setting | Default | Description |
|---|---|---|
| `platform` | `hikconnect` | `hikconnect` or `ezviz` |
| `email`, `password` | — | Cloud account credentials |
| `region` | auto | API domain; detected at login when not set |
| `token_file` | `<data dir>/token.json` | Saved session; the terminal id and caches are kept next to it |
| `camkey_file` | `<data dir>/cam_keys.json` | Verification codes |
| `stream_mode` | `main` | `main`, `sub` or `auto` (substream first, main as fallback) |
| `substream` | `false` | Shorthand for `stream_mode=sub` |
| `use_gpu` | `true` | NVDEC decoding; falls back to software after repeated failures |
| `display_width`, `display_height` | `1280`, `720` | Size of decoded frames |
| `proxy_start_port` | `8700` | First local port for the per-camera proxies |
| `warm_cache` | `true` | Refresh account metadata in the background |
| `isup_url` | `http://127.0.0.1:8091` | ISUP bridge control API |
| `isup_token` | — | `X-Bridge-Token` for the bridge |
| `isup_rtsp_base` | `rtsp://127.0.0.1:8554` | RTSP server the bridge publishes to |

The data directory is `%APPDATA%\cloudcam` on Windows,
`~/Library/Application Support/cloudcam` on macOS and `~/.config/cloudcam` (or
`$XDG_CONFIG_HOME/cloudcam`) elsewhere; set `CLOUDCAM_HOME` to use another one. Relative
paths in `config.py` are resolved next to `config.py`, and relative paths in environment
variables against the working directory. Files left in the working directory by earlier
versions are copied to the data directory on the first login, including the terminal id,
so the cloud does not ask for "new device" 2FA again.

Additional environment variables: `CLOUDCAM_LANG=uz` for Uzbek messages,
`CLOUDCAM_VTM_TTL` for the metadata cache lifetime in seconds (default 3600), and
`CLOUDCAM_FEATURE_CODE` to override the per-install terminal id.

The account commands (`cameras`, `keys`, `snapshot`, `view`, `status`, `events`,
`records`, `ptz`) need the email and password to be configured even when a saved
session exists: the session is resumed first and the password is used only if a new
login is required. `cloudcam code check` and `cloudcam isup` do not need them.

For a source checkout:

```bash
git clone https://github.com/Bahrombekk/cloud-cam-viewer.git
cd cloud-cam-viewer
pip install -e ".[viewer]"
cp config.example.py config.py      # set PLATFORM, EMAIL, PASSWORD
python scripts/app.py               # pick cameras from a list, then the grid viewer
python scripts/single.py <SERIAL>   # one camera
```

## Verification codes

An encrypted camera cannot be decoded without its verification code, which is the
AES key of the stream. Without it the viewer shows "No verification code - run
`cloudcam keys`" or "Wrong verification code! Check cam_keys.json" on a black tile. The
cloud stores the codes, so `cloudcam keys` retrieves them instead of requiring them to
be read from labels.

On the first run the cloud requires the session to be elevated. It emails a 2FA code
and the command stops with exit code 2:

```
$ cloudcam keys
2FA code sent to b***@gmail.com. Run again:
  cloudcam keys --code <CODE>
```

The code is entered once. It elevates the session, and the remaining cameras are
fetched without further codes:

```
$ cloudcam keys --code 123456
AB1234567  QWERTY
AB2345678  ABCDEF
AB3456789  ABCDEF
```

Codes already stored are not fetched again (use `--overwrite` to force it), and an
offline device is reported without stopping the others:

```
$ cloudcam keys
AB2345678  ABCDEF
(1 codes already stored — use --overwrite to re-fetch)
AB3456789  ERROR: Device offline (2009)
```

The codes are stored in `cam_keys.json` as a flat serial-to-code map (see
[`cam_keys.example.json`](cam_keys.example.json)). For an NVR the code belongs to the
device, so one entry covers all of its channels.

```json
{
  "AB1234567": "QWERTY",
  "AB2345678": "ABCDEF"
}
```

If a code is already known, or the account cannot be elevated (shared devices,
sub-accounts), it can be entered manually:

```bash
cloudcam code check <SERIAL> <CODE>                 # verify against the stream, save if correct
cloudcam code check <SERIAL> <CODE> --channel all   # check NVR channels 1-4
cloudcam code check <SERIAL>                        # only detect whether it is encrypted
cloudcam code set   <SERIAL> <CODE>                 # save without verifying
```

Codes are case-sensitive and stored exactly as entered. `code check` tries the code
as given and, if it is wrong, once more in upper case; it exits with 1 unless every
checked channel is correct or unencrypted. It uses the saved session, so run an account
command such as `cloudcam cameras` once first. `scripts/check_code.py` and
`scripts/set_code.py` accept the old arguments (`<SERIAL> <CODE> [CHANNEL|all]`). If
`cam_keys.json` cannot be parsed, every command refuses to overwrite it rather than
discard the stored codes.

## Command-line reference

Global options, placed before the command: `--email`, `--password`,
`--platform {hikconnect,ezviz}`. Every command has `--help`.

| Command | Description |
|---|---|
| `cloudcam cameras [--check]` | List cameras. `--check` probes each channel and marks it `live`, `NO CODE`, `offline` or `?`; exits with 1 if any channel is not live |
| `cloudcam keys [SERIAL ...] [--code CODE] [--overwrite]` | Fetch verification codes for all or the given devices |
| `cloudcam code check SERIAL [CODE] [--channel N\|all]` | Verify a code on the live stream; saves it if correct |
| `cloudcam code set SERIAL CODE [SERIAL CODE ...]` | Store codes without verifying |
| `cloudcam snapshot SERIAL [--channel N] [--key CODE] [--out FILE] [--quality Q] [--timeout S]` | Save one JPEG frame (default `<SERIAL>_ch<N>.jpg`, quality 85, timeout 15 s) |
| `cloudcam view` | Grid viewer with every camera on the account |
| `cloudcam stream SERIAL --rtsp URL [--channel N] [--stream main\|sub\|auto] [--key CODE]` | Publish one camera to an RTSP server (MediaMTX, go2rtc, ...) without transcoding, until Ctrl+C |
| `cloudcam status [SERIAL ...]` | Online state, disks, encryption and privacy mode; exits with 1 if a device is offline or a disk is not OK |
| `cloudcam events [SERIAL ...] [--limit N] [--save-images DIR]` | Recent events (default 20), optionally saving decrypted pictures |
| `cloudcam events --watch [--save-images DIR]` | Live push events until Ctrl+C (`events` extra) |
| `cloudcam records SERIAL [--channel N] [--hours H]` | Recording segments on the SD card or NVR (default last 24 h) |
| `cloudcam ptz SERIAL COMMAND [--channel N] [--speed 1-7] [--duration S]` | Move a PTZ camera: `UP`, `DOWN`, `LEFT`, `RIGHT`, `UPLEFT`, `UPRIGHT`, `DOWNLEFT`, `DOWNRIGHT`, `ZOOMIN`, `ZOOMOUT`, `FOCUSNEAR`, `FOCUSFAR` (default speed 5, 0.5 s) |
| `cloudcam isup [devices]` | Devices and streams connected to the ISUP bridge |
| `cloudcam isup keys DEVICE_ID=KEY ... [--no-default]` | Send device keys to the bridge (replaces the previous list) |

Viewer keys: `q` quit, `g` grid, `1`-`9` fullscreen camera, `s` status line. Closing
the window also exits.

Example output of `cloudcam status`, showing a failed NVR disk:

```
L3473xxxx  online   clear     #3:ok/1.8TB #4:ERR1
F1803xxxx  online   clear     #1:ok/5.5TB
```

Status is reported per device: an NVR can be online while one of its cameras is
disconnected. Use `cloudcam cameras --check` for the per-channel state.

## Python API

The package does not require `config.py` or OpenCV:

```python
from cloudcam import CloudCam

with CloudCam(email="you@example.com", password="...", platform="hikconnect") as cam:
    cam.login()                      # resumes a saved session when available

    for c in cam.cameras():
        print(c.serial, c.channel, c.name)

    res = cam.fetch_keys()           # verification codes from the cloud
    if res.needs_mfa:
        cam.send_key_2fa()           # emails a code; then cam.fetch_keys(mfa_code="...")

    stream = cam.open("AB2345678", channel=1)
    if stream.wait_first_frame(timeout=15):
        jpeg = stream.snapshot()     # JPEG bytes (requires OpenCV)
        for frame in stream.frames(timeout=10):   # numpy BGR arrays
            ...
    stream.close()
```

`CloudCam` (all methods return plain data; network errors are raised):

| Method | Returns |
|---|---|
| `login(force=False)` | Resumes the saved session, refreshes it, or logs in. `auth_mode` tells which |
| `cameras()` | `[Camera(serial, channel, name)]`, empty NVR slots excluded |
| `open(serial, channel=1, *, key=None, width=None, height=None)` | `Stream`; the code is taken from `cam_keys.json` or fetched |
| `fetch_keys(serials=None, *, mfa_code=None, overwrite=False)` | `FetchResult(fetched, skipped, failed, needs_mfa)` |
| `send_key_2fa()` | Emails the 2FA code needed by `fetch_keys` |
| `check(cameras=None)` | `{(serial, channel): "ok" \| "nokey" \| "offline" \| "unknown"}` |
| `status(serials=None)` | `{serial: DeviceStatus(online, encrypted, privacy, disks, disks_ok)}` |
| `events(serials=None, *, limit=20, cursor="")` | `([Event], next_cursor)` |
| `event_image(event)` | Picture bytes, decrypted with the stored code |
| `records(serial, channel=1, start=None, end=None)` | `[Record(start, end, type)]`, default last 24 h |
| `ptz(serial, command, *, channel=1, speed=5, duration=0.5)` | Moves for `duration` seconds, then stops |
| `push(on_event)` | Context manager delivering `Event` objects (`events` extra) |
| `publish(serial, target, channel=1, *, key=None, stream_mode=None)` | Publishes the camera to the RTSP URL `target` without transcoding; returns a `Stream` whose `status()` reports progress (`frame()` is always `None`) |
| `start_token_refresh()` | Keeps the session fresh in a background thread |
| `isup`, `isup_cameras()`, `open_isup(device_id, channel=1, *, sub=False)` | ISUP bridge client, cameras and streams |
| `close()` | Stops all streams and background work |

`Stream`: `frame()` (latest frame or `None`), `frames(poll=0.03, timeout=None)`,
`snapshot(fmt="jpeg" | "numpy", quality=85)`, `wait_first_frame(timeout=15)`,
`status()`, `connected`, `fps`, `local_url` (the decrypted stream on a local HTTP
port, readable by ffmpeg) and `close()`.

Streams opened through one `CloudCam` keep its settings, so a second `CloudCam` for
another account in the same process does not change them.

## ISUP 5.0

The cloud path depends on the cloud: 4-14 s to the first frame, a limit on
concurrent streams, and occasional corruption. With ISUP 5.0 the Hikvision device
connects directly to your server, so no relay, verification code or decryption is
involved.

The ISUP protocol is implemented in Hikvision's closed SDK, so it runs in a separate
process built against that SDK, in the same way that decoding runs in an external
`ffmpeg`. The bridge's source, Dockerfile and a compose file with MediaMTX are in
[`isup-bridge/`](isup-bridge/); **the SDK itself may not be redistributed and is not
included**, so you have to obtain it from Hikvision before building (see
[isup-bridge/README.md](isup-bridge/README.md)). `cloudcam` controls the bridge over
HTTP and reads the RTSP stream it publishes.

```
camera --ISUP 17660/17661--> isup-bridge --RTSP (no transcode)--> cloudcam
```

The bridge API (all bodies are plain text, one item per line; `X-Bridge-Token` is sent
when `isup_token` is set):

| Request | Purpose |
|---|---|
| `GET /health` | `{"ok": true}` when the bridge is running |
| `GET /devices` | Connected devices, their channels and open streams, rejected devices |
| `PUT /keys` | Device keys: `dev <ID> <KEY>`, `block <ID>`, `prefix <PREFIX> <KEY>`, `default on\|off` |
| `PUT /want` | Streams to keep open: `<ID> <CHANNEL> <0=main\|1=sub>` |

Streams are published at `<isup_rtsp_base>/isup/<ID>/<CHANNEL>` (substream: `/sub`
appended). The cloud path does not depend on the bridge, and the Python package does
not contain it.

Measured with a bridge on a DS-TCG406-E, firmware V5.4.0:

| | Cloud | ISUP |
|---|---|---|
| "Send video" to first packet | 4-14 s | 0.23 s |
| First clean frame (with transcode) | about 7-10 s | 0.33-0.38 s |
| Concurrent-stream limit | 16 | none |

```
$ cloudcam isup devices
AA1234567  10.0.0.8        V5.4.0       channels:2 online:93s
    ch1 main live  2.5 MB
    ch2 sub  idle  0.0 MB
```

```python
cam = CloudCam()                                  # no cloud account needed
cam.isup.set_keys({"AA1234567": "SECRET"}, default=False)
stream = cam.open_isup("AA1234567", channel=1)   # asks the bridge to open the stream
frame = stream.frame()
```

`open_isup()` adds the stream to the bridge's "want" list and `CloudCam.close()`
clears the list, so devices only send video while they are being watched. Use
`cam.isup.unwant((device_id, channel))` to release a single stream earlier.

## Architecture

```
cloud login (session reused)
  -> device and stream metadata (cached per account)
  -> cloud VTM relay: RTP / MPEG-PS / MPEG-TS packets
  -> AES decryption of NAL units (key = verification code)
  -> jitter buffer, paced at the measured frame rate
  -> local HTTP (Annex-B) -> ffmpeg -> frames
```

Each camera runs in its own decrypting proxy process on `127.0.0.1`, and its own
ffmpeg process. Live viewing does not require a cloud storage subscription.
Implementation details, with measurements from real cameras, are documented in
[docs/internals.md](docs/internals.md).

## Compatibility

Verified on real hardware so far. Reports for other devices are welcome via the
[camera compatibility form](https://github.com/Bahrombekk/cloud-cam-viewer/issues/new?template=camera_compat.yml).

| Source | Device | Transport / codec | Encryption | Status |
|---|---|---|---|---|
| Hik-Connect cloud | NVR, multiple channels | RTP, H.265 | all slices | Verified |
| Hik-Connect cloud | IP camera | RTP, H.265 | encrypted | Verified |
| Hik-Connect cloud | IP camera | RTP, H.264 | encrypted | Verified |
| Hik-Connect cloud | IP camera | MPEG-PS, H.264/H.265 | selective / whole NAL | Verified |
| ISUP 5.0 (`isup-bridge`) | DS-TCG406-E, fw V5.4.0 | RTSP via bridge | none | Verified |
| EZVIZ cloud | — | — | — | Not yet verified, reports wanted |

Status, events, recordings and cloud-video listing were verified on a live Hik-Connect
account. PTZ, `capture` and push registration are implemented from pyezvizapi but have
not been exercised on a live account yet.

## Limitations

- The first frame takes about 7-10 s (cloud handshake and waiting for a keyframe).
- Playback runs about one GOP (1-2 s) behind live because of the jitter buffer; the
  mobile apps make the same trade-off.
- Frame rate is limited by the camera and bandwidth, typically 9-15 fps for HD cloud
  streams.
- The number of simultaneous streams is limited by CPU/GPU, bandwidth and the cloud's
  concurrent-stream limit. Each camera uses two processes.
- `cloudcam keys` requires an account that allows 2FA elevation. Shared devices and
  sub-accounts may not return codes; enter those with `cloudcam code`.
- The CLI messages are in English or Uzbek (`CLOUDCAM_LANG=uz`); many source comments
  are still in Uzbek.

## Security

- `token.json`, `cam_keys.json` and the terminal id are written atomically with
  owner-only permissions (`0600` on POSIX).
- Session tokens are only sent to the EZVIZ / Hik-Connect cloud domains; a token file
  that points elsewhere is not used.
- The verification code reaches the proxy process through an environment variable,
  never the command line. The account password and ISUP token are not passed to
  child processes.
- The local proxy listens on `127.0.0.1` only and rejects requests with a foreign
  `Host` header. It does not authenticate local users, so avoid running it on a
  machine shared with untrusted accounts.
- Prefer environment variables or `config.py` over `--password`, which is visible in
  the process list and shell history.
- `config.py` is executed as Python; do not run `cloudcam` in a directory whose
  `config.py` you do not trust.

Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Roadmap

- **v0.4, `cloudcam serve`** (in progress; `cloudcam stream` is the first part): every
  camera as a local RTSP / WebRTC stream through
  go2rtc / MediaMTX, with one upstream cloud session shared by any number of viewers,
  usable as a source in Frigate or Home Assistant. Includes a Docker image.
- **v0.5, benchmarks**: reproducible first-frame p50/p95, recovery after network loss,
  and CPU usage per camera.
- Publication on PyPI.

The detailed engineering backlog is in [docs/roadmap.md](docs/roadmap.md); released
changes are listed in [CHANGELOG.md](CHANGELOG.md).

## Development

```bash
pip install -e ".[dev]"
pytest          # no cloud account or network required
ruff check .
```

The tests cover failure modes that are not visible at runtime, for example
`-fflags nobuffer` discarding the first parameter sets, a 2FA code being re-sent for
every camera, the verification code leaking onto the command line, an audio packet
between video fragments being treated as packet loss, and a proxy outliving its
parent process. The package layers (`core < codec < media < sources < app`) are
enforced by `tests/test_layout.py`; see [docs/internals.md](docs/internals.md).

Bug reports, compatibility reports and pull requests are welcome; see
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

Released under the MIT License; see [LICENSE](LICENSE).

The cloud stream transport (`src/cloudcam/sources/cloud/vtm/`), the REST client
(`rest.py`) and push events (`push.py`) are adapted from
[pyezvizapi](https://github.com/RenierM26/pyEzvizApi) 1.0.5.0 by Renier Moorcroft and
remain under the Apache License 2.0; see [NOTICE](NOTICE) and
[LICENSE-pyezvizapi](src/cloudcam/sources/cloud/vtm/LICENSE-pyezvizapi).
