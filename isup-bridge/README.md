# isup-bridge

A small C++ service that lets Hikvision devices stream to you over **ISUP 5.0**
instead of the vendor cloud, and republishes each stream as RTSP without
transcoding. `cloudcam` controls it through its HTTP API (`cloudcam isup ...`,
`CloudCam.open_isup()`), but any client that speaks the API below can use it.

```
device --ISUP register 17660 (tcp+udp)--> isup-bridge <--video 17661 (tcp)-- device
                                               |  ffmpeg -c copy
                                               v
                      RTSP server (MediaMTX): isup/<DeviceID>/<channel>[/sub]
                                               |
                                               v
                                   cloudcam, VLC, Frigate, ...
```

The device dials out to the bridge, so nothing has to be opened on the camera's
network. Measured on a DS-TCG406-E (firmware V5.4.0): first packet 0.23 s after the
stream was requested, about 0.04 CPU cores and 100 MB of memory for bridge plus
ffmpeg per stream.

> **Status: experimental.** Verified with one device model and one SDK build
> (HCISUPCMS 2.5.0.101). NVR channel discovery, substreams and per-device keys are
> implemented; reports from other devices are welcome.

## Hikvision ISUP SDK (required, not included)

The bridge is built against Hikvision's ISUP SDK for Linux x86-64. The SDK is
licensed by Hikvision and **may not be redistributed**, so it is not part of this
repository. Request it from Hikvision (Technology Partner Program, "ISUP SDK"), then:

1. Copy the SDK's shared libraries into `sdk/`:
   `libHCISUPCMS.so libHCISUPStream.so libhpr.so libHCNetUtils.so libcrypto.so(.1.0.0)
   libssl.so(.1.0.0) libz.so libsqlite3.so libNPQos.so libHCISUPAlarm.so libHCISUPSS.so`
   and the `HCAapSDKCom/` directory.
2. Copy the SDK headers into `inc/`. They are GBK-encoded; convert them to UTF-8 so g++
   can read them:
   ```bash
   for f in /path/to/sdk/include/*.h; do iconv -f GBK -t UTF-8 -c "$f" > inc/"$(basename "$f")"; done
   ```

`sdk/` and `inc/` are git-ignored, so the SDK cannot be committed by accident.

## Run

```bash
cd isup-bridge
cp .env.example .env          # set PUBLIC_IP, and ISUP_KEY or per-device keys
docker compose up -d --build
docker compose logs -f isup-bridge
```

The compose file starts the bridge and a MediaMTX RTSP server. Expected log line:
`ISUP ko'prik tayyor: ro'yxat 17660 (tcp/udp), video 17661 (tcp), API 8091, ...`
("bridge ready").

Open **17660/tcp, 17660/udp and 17661/tcp** inbound to this host (firewall and, behind
NAT, port forwarding). The control API (8091) and RTSP (8554) are bound to `127.0.0.1`
in the compose file; keep them off the internet.

### Environment

| Variable | Default | Meaning |
|---|---|---|
| `PUBLIC_IP` | — (required) | Address the devices can reach this host on |
| `ISUP_KEY` | — | Fallback Encryption Key (8-32 letters/digits) for devices without their own key |
| `API_TOKEN` | — | Shared secret; required as `X-Bridge-Token` on every API call except `/health` |
| `ACCESS_SECURITY` | `0` | SDK access mode: `0` compatible, `2` secure (ISUP 4.0+ only) |
| `CMS_PORT` / `STREAM_PORT` / `API_PORT` | `17660` / `17661` / `8091` | Ports |
| `RTSP_BASE` | `rtsp://mediamtx:8554` | Where streams are published (may include credentials) |
| `MTX_API` | — | MediaMTX API URL, only if your `mediamtx.yml` does not allow publishing to `isup/` |
| `CHANNELS`, `STREAM_TYPE` | `1`, `0` | Standalone mode (no client): channels opened for every device, main or sub |
| `SDK_LOG_LEVEL` | `2` | SDK log level, written to `logs/` |

## Configure the device

Device web interface, **Configuration → Network → Advanced → Platform Access → ISUP**
(the menu name varies by model):

| Field | Value |
|---|---|
| Enable | on |
| Protocol version | 5.0 |
| Server address | `PUBLIC_IP` |
| Server port | 17660 |
| Device ID | unique, only `A-Z a-z 0-9 _ -` (it becomes the RTSP path) |
| Encryption key | the device's key (see below) |

After saving, the registration status should turn **Online** within 10-20 s. After many
failed attempts a device may back off; disabling and re-enabling ISUP makes it retry
immediately. Devices sending H.264 can be consumed without transcoding anywhere;
H.265 needs a consumer that supports it.

## Use with cloudcam

```bash
cloudcam isup keys CAM-001=Secret1234 CAM-002=Secret5678 --no-default
cloudcam isup devices
```

```python
from cloudcam import CloudCam

cam = CloudCam()                                   # no cloud account needed
stream = cam.open_isup("CAM-001", channel=1)       # asks the bridge to open it
frame = stream.frame()
```

`cloudcam` reads `isup_url`, `isup_token` and `isup_rtsp_base` from its settings
(`CLOUDCAM_ISUP_URL` etc.); the defaults match the compose file. Streams can also be
played directly: `ffplay rtsp://127.0.0.1:8554/isup/CAM-001/1`.

## Control API

| Request | Body / response |
|---|---|
| `GET /health` | `{"ok":true}`; no token required (used by the Docker health check) |
| `GET /devices` | JSON: `want_set`, `keys_set`, `default_key`, `rejected[]`, `devices[]` with `id`, `serial`, `ip`, `firmware`, `channels_total`, `start_channel`, `ip_channels[]`, `online_seconds`, `key_source`, `streams[]` |
| `PUT /want` | Text, one line per stream to keep open: `<DeviceID> <channel> <0\|1>` (0 main, 1 sub). Replaces the previous list |
| `PUT /keys` | Text, one line each: `dev <ID> <KEY>`, `block <ID>`, `prefix <PREFIX> <KEY>`, `default on\|off`. Replaces the previous list; devices whose key changed or that are blocked are disconnected |

Key lookup for a registering device, in order: blocked → own key → longest matching
prefix → `ISUP_KEY` (if `default on`) → rejected. Keys live only in memory; a client
should resend them when `/devices` reports `"keys_set": false` (after a restart).

Until the first `PUT /want` the bridge is in standalone mode and opens `CHANNELS`
with `STREAM_TYPE` for every connected device, 24/7.

## Security notes

- Device IDs and other device data are treated as untrusted: IDs are limited to
  `A-Z a-z 0-9 _ -` before they reach paths, JSON or logs.
- A device must pass the key check (`ENUM_DEV_AUTH`) in the same session before it is
  accepted; registrations without it are rejected even in compatible mode. Set
  `ACCESS_SECURITY=2` once your devices work with it to reduce the attack surface.
- The control API has no TLS. Bind it to localhost or a private network, and set
  `API_TOKEN` if other hosts can reach it.
- The container runs as an unprivileged user.

## Logs

The bridge logs in Uzbek. The lines that matter:

| Log line | Meaning |
|---|---|
| `QURILMA ULANDI: id=...` | Device registered |
| `RAD ETILDI: id=... (kalit topilmadi ...)` | Rejected: no key for this Device ID |
| `RAD ETILDI: ... Device ID'da ruxsat etilmagan belgi` | Rejected: invalid characters in the Device ID |
| `KALIT XATO: id=...` | The device's Encryption Key does not match |
| `[api] want yangilandi: N ta oqim` | A client sent a want list with N streams |
| `[vaqt] birinchi video paket: so'rovdan X s` | First video packet X s after the request |
| `oqim to'xtadi — keyinroq qayta urinamiz` | No video (missing channel or substream); retried with backoff 3-60 s |
| `endi kerak emas — yopildi` | Stream no longer wanted, closed |
| `EHOME50_ERROR` with `Accept` | Something other than a device connected (port scanner, curl); harmless |

## Known limitations

- Video only; audio is dropped (`-an`). No alarm/ANPR events yet.
- ISUP and Hik-Connect running at the same time on one NVR has not been tested.
- `StartPushRealStream` returns after about 5 s although video already flows, so each
  stream is opened on its own thread.

## License

`bridge.cpp`, the Dockerfile and the compose file are MIT-licensed like the rest of the
repository. The Hikvision ISUP SDK is not, and is not distributed here.
