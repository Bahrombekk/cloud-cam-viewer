# Using cloudcam streams in Frigate and Home Assistant

← [Back to README](../README.md)

`cloudcam serve` (or the [Docker image](docker.md)) exposes every camera as RTSP:

| Path | Stream |
|---|---|
| `rtsp://<host>:8554/cam/<SERIAL>_<CHANNEL>` | Main stream |
| `rtsp://<host>:8554/cam/<SERIAL>_<CHANNEL>/sub` | Substream (main stream on cameras without one) |

The video is copied, not re-encoded; cloud cameras usually send H.265. A stream opens
when the first client connects and takes several seconds (cloud handshake and keyframe),
so give consumers a generous connection timeout.

> These configurations follow the cloudcam stream layout. They have not been run against
> Frigate or Home Assistant as part of cloudcam's own tests; reports are welcome.

## Frigate

Detect on the substream, record the main stream. Keeping both open all the time also
avoids a short freeze some NVRs show when one stream of a channel closes (see the
README's limitations).

`docker-compose.yml` next to cloudcam's (same network, no published ports needed):

```yaml
services:
  cloudcam:
    image: cloudcam:local
    env_file: docker/.env
    volumes:
      - cloudcam-data:/data
    restart: unless-stopped

  frigate:
    image: ghcr.io/blakeblackshear/frigate:stable
    shm_size: "256mb"
    volumes:
      - ./frigate/config:/config
      - ./frigate/media:/media/frigate
    ports:
      - "8971:8971"
    restart: unless-stopped
    depends_on:
      - cloudcam

volumes:
  cloudcam-data:
```

`frigate/config/config.yml`:

```yaml
cameras:
  warehouse:
    ffmpeg:
      input_args: preset-rtsp-restream
      inputs:
        - path: rtsp://cloudcam:8554/cam/AB1234567_1/sub
          roles: [detect]
        - path: rtsp://cloudcam:8554/cam/AB1234567_1
          roles: [record]
    detect:
      width: 640
      height: 360
```

Set `detect.width/height` to the substream's size (`ffprobe rtsp://.../sub` shows it).
If `cloudcam serve` runs with `--read-user/--read-pass`, put the credentials in the URL:
`rtsp://viewer:secret@cloudcam:8554/cam/AB1234567_1`.

## Home Assistant

Add the camera with the **Generic Camera** integration (Settings → Devices & services →
Add integration → Generic Camera):

| Field | Value |
|---|---|
| Stream source | `rtsp://<host>:8554/cam/AB1234567_1/sub` |
| RTSP transport protocol | TCP |
| Still image URL | leave empty (Home Assistant takes stills from the stream) |
| Username / password | the `--read-user/--read-pass` values, if set |

Use the substream for dashboard tiles and the main stream for a fullscreen view.
Browsers play H.265 only on some platforms; if the dashboard stays black, the camera's
codec is the usual cause.

## Any other RTSP client

```bash
ffplay -rtsp_transport tcp rtsp://127.0.0.1:8554/cam/AB1234567_1
vlc rtsp://127.0.0.1:8554/cam/AB1234567_1
```

For WebRTC in a browser, open `http://<host>:8889/cam/AB1234567_1`.
