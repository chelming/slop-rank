FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    CONTENT_DIR=/content

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/
# Sample content; mount your own items.json + img/ over /content.
COPY content.example/ /content/

# Runs unprivileged; /data is the only path it writes to, so the bind mount must
# be writable by this uid (uid/gid 1000 matches the host user here).
RUN useradd -u 1000 -m app && mkdir -p /data && chown app:app /data
USER app

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
