FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AIS_DATA_DIR=/data \
    AIS_MAX_UPLOAD_BYTES=67108864

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data/originals \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin ais \
    && chown -R ais:ais /data /srv
USER ais

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log"]
