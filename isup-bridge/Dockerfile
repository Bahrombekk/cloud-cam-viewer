# isup-bridge: Hikvision ISUP 5.0 SDK (Linux x86-64) + ffmpeg (stream copy, no transcoding).
#
# sdk/ and inc/ are NOT in the repository: the Hikvision ISUP SDK may not be
# redistributed. Put the SDK's .so files into sdk/ and its headers (converted to UTF-8)
# into inc/ before building; see README.md.
FROM ubuntu:22.04 AS build
RUN apt-get update && apt-get install -y --no-install-recommends g++ && rm -rf /var/lib/apt/lists/*
COPY inc/ /src/inc/
COPY sdk/ /opt/isup/lib/
COPY bridge.cpp /src/
RUN test -f /opt/isup/lib/libHCISUPCMS.so || { echo "sdk/ is empty: add the Hikvision ISUP SDK .so files (see README.md)"; exit 1; }
RUN test -f /src/inc/HCISUPCMS.h || { echo "inc/ is empty: add the ISUP SDK headers converted to UTF-8 (see README.md)"; exit 1; }
RUN g++ -O2 -std=c++17 -Wall -I/src/inc /src/bridge.cpp -o /src/isup-bridge \
    -L/opt/isup/lib -lHCISUPCMS -lHCISUPStream -lhpr -lHCNetUtils -lpthread \
    -Wl,-rpath,/opt/isup/lib

FROM ubuntu:22.04
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY sdk/ /opt/isup/lib/
COPY --from=build /src/isup-bridge /opt/isup/lib/isup-bridge
ENV LD_LIBRARY_PATH=/opt/isup/lib:/opt/isup/lib/HCAapSDKCom TZ=UTC
WORKDIR /opt/isup/lib
# Not root: the bridge parses data from devices on the internet. All ports are above 1024.
RUN useradd -r -u 10001 -d /opt/isup isup && mkdir -p /opt/isup/logs && chown -R isup /opt/isup/logs
USER isup
EXPOSE 17660/tcp 17660/udp 17661/tcp 8091/tcp
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD curl -fsS -m 2 http://localhost:${API_PORT:-8091}/health || exit 1
CMD ["/opt/isup/lib/isup-bridge"]
