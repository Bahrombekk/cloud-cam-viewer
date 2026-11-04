# cloudcam: bulut kameralari -> lokal RTSP / WebRTC / HLS (MediaMTX), bitta image'da.
#
#   docker compose -f docker/docker-compose.yml up -d --build     (qarang: docs/docker.md)
#
# Qurish konteksti — repo ildizi; .dockerignore faqat pyproject.toml, README, LICENSE,
# NOTICE va src/ ni kiritadi (ildizdagi token.json / cam_keys.json / config.py KIRMAYDI).
FROM python:3.12-slim-bookworm

ARG MEDIAMTX_VERSION=1.20.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/cloudcam
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
# MediaMTX — o'zimizning o'rnatuvchi bilan: rasmiy GitHub relizi, SHA-256 tekshiriladi,
# platforma (amd64 / arm64 / armv7) o'zi aniqlanadi.
RUN pip install --no-cache-dir . \
 && python -c "from cloudcam import mtx_install; mtx_install.install('${MEDIAMTX_VERSION}', bin_dir='/usr/local/bin')" \
 && mediamtx --version \
 && rm -rf /opt/cloudcam/build /opt/cloudcam/src

# Root emas. Sessiya, kodlar, terminal ID va MediaMTX konfiguratsiyasi — /data da.
RUN useradd -r -u 10001 -m -d /home/cloudcam cloudcam \
 && mkdir -p /data && chown cloudcam /data
USER cloudcam
WORKDIR /data
ENV CLOUDCAM_HOME=/data \
    PYTHONUNBUFFERED=1
VOLUME /data

# RTSP, HLS, WebRTC (HTTP) va WebRTC media (UDP).
EXPOSE 8554 8888 8889 8189/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8554), 3)" || exit 1

ENTRYPOINT ["cloudcam"]
# Konteyner ichida hamma interfeysda tinglash shart; tashqariga nima ochilishini
# docker-compose.yml dagi `ports` hal qiladi (standart: faqat shu kompyuter).
CMD ["serve", "--listen", "0.0.0.0"]
