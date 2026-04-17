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

RUN useradd -r -m -d /home/pttadmin -s /bin/false pttadmin && \
    chown -R pttadmin:pttadmin /home/pttadmin
USER pttadmin
ENV HOME=/home/pttadmin

EXPOSE 8000

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
