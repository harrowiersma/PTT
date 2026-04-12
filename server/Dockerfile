FROM python:3.11-slim

WORKDIR /app

# Install libopus (required by pymumble for Mumble protocol)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libopus0 && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/server/

# Create non-root user
RUN useradd -r -s /bin/false pttadmin
USER pttadmin

EXPOSE 8000

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
