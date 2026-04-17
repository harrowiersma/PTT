FROM python:3.11-slim

# Build-time deps for PJSIP + pjsua2 Python SWIG binding, plus runtime
# libs that pjsua2 links against. Keep runtime footprint modest — we
# strip the build chain out at the end of the Dockerfile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential swig git pkg-config \
        libssl-dev libasound2-dev \
        libopus0 libopus-dev \
        libspeex-dev libspeexdsp-dev \
        libsrtp2-dev \
        python3-dev \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Build PJSIP 2.14. We disable video + v4l2 (not needed for a phone
# bridge) and the built-in sound devices (Docker containers usually have
# no ALSA hardware; we use pjsua2's memory-based AudioMedia instead).
ENV PJ_VERSION=2.14
RUN git clone --depth 1 --branch ${PJ_VERSION} https://github.com/pjsip/pjproject /tmp/pjproject && \
    cd /tmp/pjproject && \
    ./configure --enable-shared \
                --disable-video --disable-v4l2 \
                --disable-sound \
                --disable-ext-sound \
                CFLAGS="-O2 -fPIC" && \
    make dep && make -j"$(nproc)" && make install && \
    ldconfig

# Build the Python SWIG wrapper (pjsua2).
RUN cd /tmp/pjproject/pjsip-apps/src/swig && \
    make python && \
    cd python && python3 setup.py install && \
    rm -rf /tmp/pjproject

# Runtime Python deps.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Shrink image — drop build tooling we don't need at runtime.
RUN apt-get purge -y --auto-remove \
        build-essential swig git pkg-config python3-dev \
        libssl-dev libasound2-dev libopus-dev libspeex-dev libspeexdsp-dev \
        libsrtp2-dev \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

WORKDIR /app
COPY bridge.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# The SIP port and RTP range we advertise to DIDWW. Must match what's
# published in docker-compose.yml and opened on the host firewall.
EXPOSE 5060/udp
EXPOSE 10000-10200/udp

ENTRYPOINT ["/app/entrypoint.sh"]
