FROM python:3.11-slim

WORKDIR /app

# libopus for pymumble; libsndfile1 for soundfile (not required by Piper,
# kept so any future WAV I/O works without another apt layer).
RUN apt-get update && \
    apt-get install -y --no-install-recommends libopus0 libsndfile1 curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the Piper voice model into the image so first-request latency is
# just ONNX graph warmup, not a network download. en_US-lessac-medium is
# ~63MB; clear American English, widely used default.
ENV PIPER_VOICE_DIR=/opt/piper
ENV PIPER_VOICE=en_US-lessac-medium
RUN mkdir -p "$PIPER_VOICE_DIR" && \
    curl -fsSL "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx" \
        -o "$PIPER_VOICE_DIR/en_US-lessac-medium.onnx" && \
    curl -fsSL "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json" \
        -o "$PIPER_VOICE_DIR/en_US-lessac-medium.onnx.json"

COPY . /app/server/
RUN chmod +x /app/server/entrypoint.sh

RUN useradd -r -m -d /home/pttadmin -s /bin/false pttadmin && \
    chown -R pttadmin:pttadmin /home/pttadmin && \
    # Add pttadmin to the murmur group (GID 10000) so it can read
    # mumble-server.sqlite on the shared `murmur-data` volume — needed
    # for admin_sqlite's direct-edit path (create channel / reset user
    # registration). GID matches the mumblevoip/mumble-server image.
    groupadd -g 10000 murmur && \
    usermod -a -G murmur pttadmin && \
    # And the docker group so the container can call the mounted
    # /var/run/docker.sock. GID 988 matches the host we deploy to; if
    # that drifts, re-enumerate here. The fallback is running admin as
    # root, which we want to avoid.
    groupadd -g 988 docker-sock || true && \
    usermod -a -G docker-sock pttadmin
USER pttadmin
ENV HOME=/home/pttadmin

EXPOSE 8000

ENTRYPOINT ["/app/server/entrypoint.sh"]
