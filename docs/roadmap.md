# Roadmap and engineering backlog

Status as of 2026-11-04. Latest release: **0.3.0** (see [CHANGELOG.md](../CHANGELOG.md)).

This document is the working plan behind the short roadmap in the
[README](../README.md#roadmap). Each open item states the problem, where it lives and
the intended approach. Locations are given as module and function names rather than
line numbers, so they stay valid when files change.

Priorities: **P0** affects correctness or 24/7 stability, **P1** performance, tests or
operability, **P2** hardening and polish.

## Principles

- **Measure before optimizing.** Performance claims come with numbers and the setup
  they were measured on (camera, codec, cold or warm cache).
- **Regression test before refactor.** The following are not rewritten without a
  fixture or test that pins the current behaviour: encryption-variant detection,
  RTP/NAL/PS parsing, jitter buffer pacing, VTM cache invalidation, the ffmpeg
  command line and process lifecycle, and the cloud request sequence.
- **The cloud path must keep working without optional parts** (OpenCV, paho-mqtt,
  the ISUP bridge).

## 1. Release 0.3.0

- [x] Live check on the account's cameras (grid viewer, proxy `Host` check).
- [x] Release workflow runs `ruff` and `pytest` before building.
- [x] Version 0.3.0, changelog dated, tag `v0.3.0`.

## 2. Reliability (P0)

- [x] **Concurrent session refresh** (0.4): `core.fsutil.file_lock` around every
  refresh/login path, token file re-read after the lock is taken.
- [ ] **Watchdog vs. `auto` stream mode.** `CameraStream.FREEZE_TIMEOUT` is 20 s; the
  proxy may spend 6 s probing the substream and up to 20 s on the main stream, so a
  slow camera can be killed before its first frame and loop. Approach: measure, then
  derive the first-frame deadline from the proxy's timeouts.
- [ ] **Child output is discarded.** The proxy and ffmpeg run with `stderr=DEVNULL`;
  when they fail, the reason is lost. Approach: structured one-line events on the
  proxy's stdout (see §6), ffmpeg stderr kept in a small ring buffer and attached to
  `CameraStream.error`.
- [ ] **Library code prints.** `StreamManager.start_token_refresh` prints to stdout.
  Approach: `logging` in the library, printing only in the CLI and scripts.
- [ ] **Broad exception handling.** 30 `except Exception` sites; the one around the
  whole `CameraStream._reader_loop` turns programming errors into endless reconnects.
  Approach: catch expected errors explicitly, log the rest with `logger.exception`.
- [ ] **Ports are never reused.** `StreamManager._next_port` only increases; opening
  and closing cameras from a UI eventually runs out of ports. Approach: free list.
- [ ] **`StreamManager.add` ignores new arguments** for an already open
  `(serial, channel)` (key, size, decrypt). Approach: document it, or `reopen=True`.

## 3. First-frame latency and performance (P1)

Measure first: cold and warm first-frame p50/p90 per stage (metadata, VTM handshake,
first IRAP, ffmpeg probe) so it is known which item dominates.

- [ ] **Single-flight metadata fetch.** Several cameras opening on a cold cache each
  request the page list and VTDU token (`vtm_cache._through`). One in-flight request
  per account, the others wait for it.
- [ ] **Fixed waits.** `CameraStream._wait_port` polls every 0.3 s and is followed by
  a fixed `sleep(0.3)`; together 0.3-0.6 s per open. Poll at 50 ms and drop the sleep.
- [ ] **ffmpeg probe size** is 200 KB (`CameraStream._ffmpeg_cmd`). Annex-B parameter
  sets arrive in the first kilobytes; try 64 KB once measured
  (`test_ffmpeg_flags` guards the upper bound).
- [ ] **Substream for grids by default.** `stream_mode` defaults to `main`. The viewer
  should request the substream for tiles and the main stream for fullscreen.
- [ ] **Proxy start cost.** Every open starts a new interpreter (imports plus cloud
  connection, about 0.5-2 s). Option: keep warm proxies for likely cameras.
- [ ] **Camera GOP.** Configuring an I-frame interval of 1-2 s on the camera bounds the
  wait for the first keyframe. Documentation only, no code change.
- [ ] **AES object reuse.** `_NalDecryptBase._aes16` and `_decrypt_body` create a new
  AES-ECB cipher per call; ECB is stateless, one instance per decryptor is enough.
- [ ] **Frame memory.** Each `CameraStream` holds a full BGR frame
  (1280x720x3, 2.7 MB) and `get_frame` copies it. For large grids decode at tile size.

## 4. Observability (P1)

- [ ] Stream lifecycle states: starting, connecting, buffering, playing, stalled,
  reconnecting, stopped; exposed through `Stream.status()`.
- [ ] Counters already collected but not exposed: `PacedWriter.dropped`, RTP
  `seq_gaps` / `late_packets` / `bad_packets`, `CameraStream.reconnect_count`,
  ffmpeg restarts. Then first-frame latency, bytes in/out, decoder errors.
- [ ] Aggregated health snapshot for all cameras; Prometheus/OpenTelemetry later.
- [ ] Secrets masked in all log output (email, session id, verification codes).

## 5. Tests (P1)

- [ ] **`CameraStream._reader_loop` and `_watchdog_loop`** with fake `Popen`/ffmpeg:
  reconnect backoff, offline backoff, frozen-stream kill, GPU-to-software fallback,
  empty-channel detection, error cleared when frames return. Only the ISUP path and
  isolated helpers are covered today.
- [ ] **Shared media fixtures** in `tests/fixtures/` (RTP, H.264/HEVC NAL units, clear
  and encrypted, MPEG-PS/TS), replacing the per-file synthetic builders.
- [ ] **MPEG-PS coverage:** `PsStreamDecryptor._decide` for H.264 and the whole-NAL
  variant.
- [ ] **Layout:** mirror the package (`tests/codec/`, `tests/media/`,
  `tests/sources/cloud/`, ...); split `test_lifecycle_and_misc.py`.

## 6. Process model and IPC (P1-P2)

- [ ] **Event-based IPC.** The proxy reports wrong code and offline through flag files
  (`core.ipc`). They are per-user since 0.3.0, but names contain only serial and
  channel, so two accounts with the same device on one host share them; they are also
  polled and carry no reason. Approach: one-line JSON events on the proxy's stdout
  (`KEY_ERROR`, `OFFLINE`, `AUTH_ERROR`, `FIRST_FRAME`, `STALLED`). This also solves
  the discarded-output item in §2.
- [ ] Supervisor with bounded concurrency; suspend hidden grid tiles while one camera
  is fullscreen.
- [ ] Load benchmark at 10 / 50 / 100 cameras (CPU, RAM, network, processes, file
  descriptors) to define a per-camera budget. The current model (two processes and a
  raw frame per camera) suits "watch N cameras", not thousands of permanent decodes.

## 7. Security hardening (P2)

- [ ] **Local proxy access.** Since 0.3.0 the proxy rejects foreign `Host` headers
  (DNS rebinding), but any local user can still read a stream. Approach: random token
  in the URL path, passed to ffmpeg by the parent.
- [ ] **`--password` on the command line** is visible in the process list and shell
  history. Approach: prompt with `getpass` when a TTY is attached and no password is
  configured; use the saved session without asking for a password where possible.
- [ ] **`config.py` is executed from the working directory**
  (`settings._load_config_module`). Running `cloudcam` in an untrusted directory runs
  its `config.py`. Approach: load it only from the data directory or an explicit
  `CLOUDCAM_CONFIG` path, with a deprecation period.
- [ ] Optional OS keyring / Windows DPAPI for `token.json` and `cam_keys.json`.
- [ ] CI: pin GitHub Actions by commit SHA.

## 8. API and code quality (P2)

- [ ] `Stream` as a context manager (`with cam.open(...) as s:`); `CloudCam` already is.
- [ ] Domain exceptions instead of `RuntimeError` for cloud, session and key errors.
- [ ] `CloudClient.get_devices` and `devices._device_names` duplicate the paging logic;
  keep one.
- [ ] `PacedWriter._nal_kind` takes an unused `b1` argument.
- [ ] `vtm_cache.channel_missing` computes `real` (channels without 0) but decides on
  `have`; document the intent or use `real`.
- [ ] paho-mqtt `CallbackAPIVersion.VERSION1` is deprecated and removed in paho 3.0;
  move `push.PushClient` to `VERSION2`.
- [ ] Ship `py.typed` and run a type checker in CI.
- [ ] Source comments and docstrings are mostly Uzbek while the public docs are
  English; translate incrementally, module by module, so contributors can follow.

## 9. Features

- [x] **0.4, step 1 — `cloudcam stream` / `CloudCam.publish()`**: one camera to an RTSP
  server without transcoding, arrival-time timestamps, connect timeout.
- [x] **0.4, step 2 — `cloudcam serve`**: MediaMTX config with an on-demand main and
  `/sub` path per camera; verified with MediaMTX 1.20.0 and a live camera.
- [x] **Sibling sessions** (0.4): the stream that "ended" was ours (probe timeout kept
  for the session); fixed. What remains is device behaviour: on some NVRs (DS-7608NI-Q1)
  closing one session of a channel freezes the other ~9-10 s, independent of token and
  close method (graceful / RST); other channels and DS-7732NXI-K4(E) are not affected.
  Viewers stay connected (readTimeout 30 s, session timeout 20 s).
- [x] **`cloudcam serve` killed hard** (0.4): `core.procutil.popen_bound` (Windows job
  object, Linux PDEATHSIG). macOS has neither; a watcher process would be needed there.
- [ ] ISUP paths in `cloudcam serve` (MediaMTX `source: rtsp://` to the bridge).
- [x] **0.4**: `cloudcam serve --install-mediamtx` (official release, SHA-256 checked).
- [ ] **0.4, step 3**: Docker image (cloudcam + ffmpeg + MediaMTX), Frigate and Home
  Assistant examples.
- [ ] **0.4 — `cloudcam serve` (overall).** Every camera as a local RTSP / WebRTC stream through
  MediaMTX or go2rtc, with one upstream cloud session per camera shared by any number
  of viewers (fan-out in the media layer, never one cloud stream per viewer). Usable
  as a Frigate or Home Assistant source; Docker image.
- [ ] **0.5 — benchmarks.** Reproducible first-frame p50/p95, recovery after network
  loss, CPU per camera (builds on §3 and §6).
- [ ] **PyPI** release (`pip install cloudcam`).
- [x] **ISUP bridge published** in `isup-bridge/` with build and deployment docs.
- [ ] **ISUP:** use the NVR channel list the bridge already reports (`ip_channels`)
  in `CloudCam.isup_cameras()`; an always-warm list of cameras kept open 24/7; a CI
  job that at least compiles the bridge against stub headers.

## 10. Done

Kept so that solved problems are not reopened. Details are in the changelog.

**0.3.0 (2026-11-04)**
- Vendored stream transport from pyezvizapi 1.0.5.0 (`sources/cloud/vtm/`); no
  dependency on pyezvizapi internals, no monkeypatching, substream as a parameter.
- Layered package (`core` < `codec` < `media` < `sources` < app), enforced by
  `test_layout.py`; the former `decrypt_proxy.py` split into those layers.
- CI on Linux and Windows, Python 3.10 and 3.13; `ruff`; release workflow.
- Device status, events with decrypted pictures, push events, recordings, PTZ.
- Cloud domain allow-list for every place a session is sent.
- Writers refuse to overwrite an unreadable `cam_keys.json`.
- Settings bound per `StreamManager` / `CameraStream`; several accounts per process.
- Busy proxy port detected; the port wait stops when the own proxy has exited.
- Proxy `Host` check; per-user `0700` flag directory with sanitised names.
- ISUP `want()` no longer poisoned by one invalid id; `stream_url` validates ids.
- Per-user data directory for session, codes and terminal id, with migration;
  relative paths in `config.py` resolve next to it.
- `cloudcam code check|set`; scripts are thin wrappers; one grid implementation,
  sized to the window.
- `isup-bridge/`: the ISUP 5.0 bridge published with build and deployment docs
  (SDK not included).
- Session refresh distinguishes a rejected refresh token (`SessionExpired`) from a
  network error, so a network blip no longer triggers a full login.

**0.2.0 and earlier**
- Session reuse (token, refresh, login as last resort), age-based refresh, login
  backoff from 60 s to 30 min.
- Per-install terminal id derived from the MAC address and persisted.
- Verification codes from the cloud with one-time 2FA elevation.
- Account-level VTM metadata cache with atomic writes and careful invalidation; cache
  warmer; channel gate for channels missing on an NVR.
- Encryption-variant detection on RTP and MPEG-PS, inter-slice decision cached per
  camera and re-verified, IRAP gate, MPEG-PS buffer limits, RTP sequence tracking per
  (SSRC, payload type).
- Jitter buffer with backpressure and keyframe-aligned drops.
- `auto` substream with main-stream fallback; frozen-stream watchdog; process cleanup
  on exit and when the parent dies.
- Verification code passed to the proxy through the environment, never argv; secrets
  written atomically with `0600`.
- OpenCV optional; `CloudCam` context manager; ISUP 5.0 bridge client.
