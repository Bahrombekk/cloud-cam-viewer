# Internals: what the code does for you

← [Back to README](../README.md)

These are not knobs you need to tune — they are behaviours worth knowing about,
each one measured against real cameras:

- **Per-camera encryption auto-detection** — on both the RTP and the MPEG-PS
  path. Some cameras encrypt only IRAP +
  parameter sets (inter P/B slices stay clear), others encrypt *everything*.
  Guessing wrong corrupts every P-frame — the picture recovers on each I-frame
  and breaks up in between. The decryptor decides from the stream itself using a
  *structure* test (slice headers repeat, ciphertext is random; measured
  separation on a real camera: clear **1.00** vs decrypted **0.12**).
  A/B on the same camera (`AB2345678`, 25 s of live stream, same account,
  decoded with `ffmpeg -f hevc`):

  | | assumed "inter is clear" | detected |
  |---|---|---|
  | decoder errors | **439** (`PPS id out of range`, `PPS changed between slices`) | **0** |
  | frames decoded | **8** (0.53 s) | **279** (18.6 s) |

  Two more cameras from the same account after the change: HEVC `AB3456789`
  181 frames / 0 errors, H.264 `AB1234567` 192 frames / 1 error.
  Only real P/B slices are sampled: SEI/AUD bodies are not slice headers, and a
  clear SEI mixed into the sample can drag an encrypted camera to a "clear" tie.
- **Lost RTP fragments do not smear the picture.** A NAL split over several RTP
  packets (FU) is dropped if a fragment is missing, instead of being glued
  together — with AES-ECB a missing piece shifts every following 16-byte block,
  so the rest of the NAL would decode as garbage. Sequence numbers are tracked
  per **(SSRC, payload type)**: Hik streams carry video (96), metadata (112) and
  audio (0) in one SSRC, each with its own counter (tracking per SSRC alone
  reported 706 false gaps in 2318 packets on a live camera; per (SSRC, PT): 0).
- **The picture starts clean, not grey.** The cloud stream begins mid-GOP and Hik
  NVRs resend parameter sets periodically, so the first VPS/SPS/PPS is *not*
  followed by an IDR. Forwarding from there leaves the decoder without reference
  frames and the viewer watches grey or smeared frames until the next keyframe.
  Slices before the first IRAP are dropped (parameter sets still pass, the decoder
  needs them); after 400 slices the gate opens anyway so a camera that never marks
  an IRAP still shows something.
- **The encryption decision is remembered per camera.** Whether inter (P/B) slices
  are encrypted is a fixed property of the camera, but it was re-derived on every
  open, and output was held back until the decision landed — measured at
  **0.65–0.85 s** of the time to first frame. It is now cached to disk and the
  cached answer is still verified in the background, so a firmware change corrects
  itself instead of silently corrupting the picture.
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
- **Account-level metadata cache** (`sources/cloud/vtm_cache.py`). The device page
  list and the VTDU token are per *account*, not per camera, yet a naive client
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

- **No orphaned processes.** Each camera runs a proxy and an ffmpeg process.
  They are stopped *and waited for* (no zombies, the port is free for the next
  start), the proxy is closed as soon as a wrong code is detected, everything is
  stopped on normal exit, and the decrypting proxy exits by itself when its
  parent dies — even on `kill -9` — because it watches its stdin pipe for EOF.
  The verification code reaches the proxy through `CLOUDCAM_CAM_KEY`, not the
  command line, so it does not show up in the process list.

## Project layout

The package is split into layers; each may import only the ones below it.
`tests/test_layout.py` enforces this by parsing every import.

```
core  <  codec  <  media  <  sources  <  api / cli / viewer / stream_manager
```

```
cloud-cam-viewer/
├── src/cloudcam/
│   ├── api.py                 # high-level facade: CloudCam / Stream / Camera
│   ├── cli.py, __main__.py    # `cloudcam` command / `python -m cloudcam`
│   ├── settings.py            # Settings: config.py / CLOUDCAM_* env / defaults
│   ├── i18n.py                # t("English", "O'zbekcha") — CLOUDCAM_LANG=uz
│   ├── stream_manager.py      # per-camera processes, reconnect, watchdog, GPU
│   ├── viewer.py              # OpenCV grid viewer (optional `viewer` extra)
│   ├── core/                  # no knowledge of video or the cloud
│   │   ├── fsutil.py          #   0600 atomic writes for secrets
│   │   ├── identity.py        #   per-install terminal id (featureCode)
│   │   ├── ipc.py             #   parent <-> camera-process contract (env, flag files)
│   │   └── paths.py           #   where per-account cache files live
│   ├── codec/                 # bytes in, bytes out: no sockets, no HTTP, no cloud
│   │   ├── nal.py             #   NAL AES decryption + encryption-variant detection
│   │   ├── rtp.py             #   RTP depacketising (H.264/HEVC, FU/AP, seq tracking)
│   │   ├── ps.py              #   MPEG-PS demux + decryption
│   │   └── enc_cache.py       #   cached per-camera inter-slice encryption decision
│   ├── media/
│   │   └── paced.py           #   jitter buffer (PacedWriter)
│   └── sources/               # where video comes from
│       ├── isup.py            #   ISUP 5.0 bridge client (cloud-free cameras)
│       └── cloud/             #   EZVIZ / Hik-Connect cloud
│           ├── client.py      #     login, session reuse, verification-code fetch
│           ├── keys.py        #     verification-code store + cloud fetch (2FA)
│           ├── devices.py     #     camera list (device + NVR channel)
│           ├── stream.py      #     open a VTM stream for one camera
│           ├── proxy.py       #     per-camera decrypting HTTP proxy (subprocess)
│           ├── vtm_cache.py   #     account-level metadata cache + channel gate
│           ├── rest.py        #     status, disks, events, records, PTZ
│           ├── push.py        #     MQTT push events
│           └── vtm/           #     stream transport, adapted from pyezvizapi (Apache-2.0)
├── scripts/                   # app.py (grid), single.py, set_code.py, check_code.py
├── tests/                     # pytest (cloud is faked — no account needed)
├── docs/                      # this file and other long-form docs
├── config.example.py          # settings template (copy to config.py)
├── cam_keys.example.json      # verification-code file format
└── README.md · CHANGELOG.md · CONTRIBUTING.md · SECURITY.md · LICENSE
```

> Run the scripts from the project root (`python scripts/app.py`, `python scripts/check_code.py ...`)
> so they find `config.py`.
