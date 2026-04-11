FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for zeroc-ice and psycopg2
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/server/

# Create non-root user
RUN useradd -r -s /bin/false pttadmin
USER pttadmin

EXPOSE 8000

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
