# Running cloudcam in Docker

← [Back to README](../README.md)

The image bundles cloudcam, ffmpeg and MediaMTX and runs `cloudcam serve`: every camera
on the account becomes an on-demand RTSP / WebRTC / HLS stream. MediaMTX is installed at
build time from the official GitHub release with its SHA-256 checked
(`cloudcam serve --install-mediamtx`), for the build platform (amd64, arm64, armv7).

Verified on 2026-11-04 (Docker 29.7, linux/amd64 image): a live Hik-Connect camera read
through the container's RTSP port at a steady 12 fps (median frame gap 0.082 s over 15 s).

## Quick start

From the repository root:

```bash
cp docker/.env.example docker/.env        # set CLOUDCAM_PLATFORM, CLOUDCAM_EMAIL, CLOUDCAM_PASSWORD
docker compose -f docker/docker-compose.yml build

# Verification codes for encrypted cameras (once). The first run emails a 2FA code:
docker compose -f docker/docker-compose.yml run --rm cloudcam keys
docker compose -f docker/docker-compose.yml run --rm cloudcam keys --code 123456

docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml logs -f cloudcam     # lists the stream URLs
```

Any `cloudcam` command runs the same way, for example
`docker compose -f docker/docker-compose.yml run --rm cloudcam cameras --check`.

Streams are at `rtsp://127.0.0.1:8554/cam/<SERIAL>_<CHANNEL>` (main) and
`.../cam/<SERIAL>_<CHANNEL>/sub` (substream). Other containers on the same compose network
use `rtsp://cloudcam:8554/cam/...` and need no published ports.

## Data

Everything cloudcam keeps (session, verification codes, terminal id, the generated
`mediamtx.yml`) lives in the `cloudcam-data` volume at `/data`. Keep the volume: a new one
means a new terminal id, and the cloud may ask for "new device" 2FA again. The account
password is only needed for the first login; it is not passed to the stream processes.

The image is built from `pyproject.toml`, `README.md`, `LICENSE`, `NOTICE` and `src/` only
(`.dockerignore` is an allow-list), so `token.json`, `cam_keys.json` or `config.py` in your
checkout never end up in the image.

## Exposing the streams

The compose file publishes the ports on `127.0.0.1` only. To read the streams from other
machines, publish them on all interfaces and require credentials for reading:

```yaml
services:
  cloudcam:
    command: ["serve", "--listen", "0.0.0.0",
              "--read-user", "viewer", "--read-pass", "${CLOUDCAM_READ_PASS:?set it in .env}"]
    ports:
      - "8554:8554"
      - "8889:8889"
      - "8189:8189/udp"
```

Only processes inside the container can publish; other hosts can only read. The read
password appears in the container's command line (`docker inspect`), so treat it as a
viewer password, not an account password.

## WebRTC from a browser

WebRTC needs the browser to reach the container's UDP port 8189 at an address MediaMTX
advertises. Inside a bridge network MediaMTX only knows the container address, so
browser playback from other machines usually needs host networking (Linux):

```yaml
services:
  cloudcam:
    network_mode: host
    command: ["serve", "--listen", "0.0.0.0",
              "--read-user", "viewer", "--read-pass", "${CLOUDCAM_READ_PASS:?set it in .env}"]
```

(`ports:` is ignored with host networking.) RTSP and HLS work in bridge mode.

## Updating

```bash
git pull
docker compose -f docker/docker-compose.yml up -d --build
```

The MediaMTX version is a build argument: `--build-arg MEDIAMTX_VERSION=1.20.0`.

## Other architectures

```bash
docker buildx build -f docker/Dockerfile --platform linux/arm64 -t cloudcam:arm64 .
```

The installer picks the matching MediaMTX build (`linux_arm64`, `linux_armv7`,
`linux_armv6`) for the target platform.

## Limitations

- The first frame takes several seconds (cloud handshake and keyframe); MediaMTX holds
  the first viewer for up to 25 s.
- See [Limitations](../README.md#limitations) in the README for cloud-side behaviour.
