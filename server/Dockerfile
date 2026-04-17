FROM python:3.11-slim

WORKDIR /app

# Install libopus (pymumble) and libsndfile1 (soundfile, used by tiny_tts)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libopus0 libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# tiny_tts -> g2p-en -> nltk needs averaged_perceptron_tagger + cmudict
# installed at a shared path. Doing this at build time avoids a runtime
# download (and its writable-home-directory requirement).
ENV NLTK_DATA=/opt/nltk_data
RUN python3 -c "import nltk; nltk.download('averaged_perceptron_tagger', download_dir='/opt/nltk_data'); nltk.download('averaged_perceptron_tagger_eng', download_dir='/opt/nltk_data'); nltk.download('cmudict', download_dir='/opt/nltk_data')"

COPY . /app/server/

# Create non-root user with a writable HOME so huggingface_hub, torch, and
# any other library that caches under $HOME can work at runtime.
RUN useradd -r -m -d /home/pttadmin -s /bin/false pttadmin && \
    chown -R pttadmin:pttadmin /home/pttadmin
USER pttadmin
ENV HOME=/home/pttadmin

EXPOSE 8000

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
