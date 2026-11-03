# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [Semantic Versioning](https://semver.org/). Until 1.0.0 the public API
(`cloudcam.CloudCam`, `Stream`, `Camera`, the `cloudcam` CLI) may change between
minor versions; such changes are always listed here.

## [Unreleased]

### Added
- **Device status** (`cloudcam status`, `CloudCam.status()`): online/offline, NVR disk
  state and capacity, video encryption, privacy mode, in one request for the whole account.
- **Events** (`cloudcam events`, `CloudCam.events()`, `CloudCam.event_image()`): recent
  motion/alarm messages. Encrypted event pictures are decrypted with the camera's
  verification code; the session id is never sent to the picture host.
- **Live push events** (`cloudcam events --watch`, `CloudCam.push()`) over MQTT, optional
  extra `cloudcam[events]` (paho-mqtt). The push host is checked against the cloud domains.
- **Recordings on the device** (`cloudcam records`, `CloudCam.records()`): SD-card / NVR
  segments with paging; list of **cloud-storage videos** (`cloud_api.cloud_videos`).
- **PTZ** (`cloudcam ptz`, `CloudCam.ptz()`) with the NVR channel passed through
  (pyezvizapi always sends channel 1), and `cloud_api.capture()`.
- Endpoints, payload shapes and the picture/push code are adapted from pyezvizapi 1.0.5.0
  (Apache-2.0). Status, events, recordings and cloud videos were verified on a live
  Hik-Connect account; PTZ, capture and push registration were not exercised live.
- English is now the default language for every user-facing message (CLI, viewer
  overlay, helper scripts). Set `CLOUDCAM_LANG=uz` for Uzbek.
- GitHub Actions CI: `ruff` + `pytest` on Linux and Windows, Python 3.10 and 3.13.
- Release workflow: pushing a `v*` tag builds sdist/wheel and publishes a GitHub
  Release with the matching section of this changelog.
- `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, issue and pull-request templates.

### Changed
- **BREAKING — package layout.** The code moved to a `src/` layout and is split into
  layers that may only import downwards (`core < codec < media < sources < app`),
  enforced by `tests/test_layout.py`. `decrypt_proxy.py` (1290 lines, six jobs) is now
  `codec/{nal,rtp,ps,enc_cache}`, `media/paced`, `core/{ipc,paths}` and
  `sources/cloud/{stream,devices,proxy}`; `client`, `keys`, `vtm_cache`, `vtm/`,
  `rest` (was `cloud_api`) and `push` live in `sources/cloud/`, `isup` in `sources/`,
  `fsutil` and `identity` in `core/`. The per-camera process is now
  `python -m cloudcam.sources.cloud.proxy`. The public API (`cloudcam.CloudCam`,
  `Stream`, `Camera`, `Settings`, the `cloudcam` CLI) is unchanged.
- The helper scripts moved to `scripts/` (`python scripts/app.py`, …); `python -m cloudcam`
  runs the CLI.
- The version has a single source of truth, `cloudcam/__init__.py`;
  `pyproject.toml` reads it dynamically.
- **`pyezvizapi` is no longer a dependency.** The part of it we used — the VTM/VTDU
  cloud stream transport (packet framing, StreamInfo/keepalive protobuf, `ysproto://`
  URL, RTP payload, VTDU token, page list) — now lives in `cloudcam/vtm/`, adapted from
  pyezvizapi 1.0.5.0 (Apache License 2.0, (c) Renier Moorcroft; notice and full
  license text in `cloudcam/vtm/LICENSE-pyezvizapi`). Before, the project replaced five
  of the library's internal functions at import time, so an upstream release could
  break it silently. The vendored code was checked byte-for-byte against upstream output.
- The substream is a `stream_type` argument of the URL builder instead of a regex over
  a monkeypatched library function: no longer racy between proxy threads, and an
  upstream URL format change can no longer silently turn it into the main stream.
- Unencrypted cameras go through `cloudcam.decrypt_proxy` (clear streams are detected
  and passed through) instead of a separate `python -m pyezvizapi ... stream proxy`.
- `list_cameras()` / `CloudCam.cameras()` use the session's own `CloudClient` (and the
  account metadata cache) instead of building a second client.
- `requirements.txt` now just installs the package (`-e .[viewer]`) instead of
  duplicating, and contradicting, the dependency list in `pyproject.toml`.
- README rewritten for first-time visitors: quick start, compatibility table,
  comparison with alternatives. The deep-dive moved to `docs/internals.md`.

### Fixed
- The `cloudcam` command ignored `config.py`: a console script has no working
  directory on `sys.path`, so it reported "Account required" with a valid config next
  to it. `config.py` is now also loaded from the working directory.
- A page-list response with an error (`meta.code` ≠ 200, e.g. an expired session) was
  stored in the metadata cache as an empty device list, making every camera on the
  account look missing for up to an hour. It is now an error and is not cached.
- The VTDU auth address taken from the cloud's service URLs is checked against the
  allowed cloud domains before the session id is sent to it.
- Lint errors (unused imports/variables, over-long lines) — `ruff check .` is clean.

## [0.2.0] - 2026-11-03

First tagged version. Summarises everything since the initial commit.

### Added
- **Cloud streaming** from EZVIZ and Hik-Connect accounts (auto region detection),
  every device and NVR channel listed as its own camera.
- **Local decryption** of encrypted streams with the device verification code (AES):
  RTP, MPEG-PS and MPEG-TS transports; H.264 and H.265/HEVC; whole-NAL, header-clear
  and selective (IRAP-only) encryption variants — all auto-detected per camera.
- **ISUP 5.0** as a second, cloud-free camera source through an external bridge
  (`cloudcam isup devices|keys`, `CloudCam.open_isup()`).
- **Verification codes from the cloud** (`cloudcam keys`, one 2FA per session).
- **Session reuse** across restarts and a per-install terminal id (`featureCode`),
  avoiding repeated CAPTCHA / "new device" 2FA.
- **Config-free library API** (`CloudCam`, `Stream`, `Camera`) and the `cloudcam`
  CLI (`cameras`, `keys`, `snapshot`, `view`, `isup`).
- `STREAM_MODE = "sub" | "main" | "auto"` — substream for grids with fallback.
- Account-level metadata cache, kept warm in the background; channel gate for
  channels that do not exist.
- Jitter buffer (`PacedWriter`) that turns the cloud's GOP bursts into an even
  frame rate; IRAP gate so the picture starts clean; cached encryption decision.
- Optional NVIDIA NVDEC decoding, 24/7 auto-reconnect and frozen-stream watchdog,
  OpenCV grid viewer.

### Security
- The cloud domain is validated (settings, login response, `token.json`).
- `token.json`, `cam_keys.json` and `feature_code.txt` are written atomically with
  `0600` permissions; the verification code reaches child processes through the
  environment, never the command line.

[Unreleased]: https://github.com/Bahrombekk/cloud-cam-viewer/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Bahrombekk/cloud-cam-viewer/releases/tag/v0.2.0
