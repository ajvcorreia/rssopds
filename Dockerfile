FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# lxml needs libxml2/libxslt; Pillow needs jpeg/zlib; fonts-dejavu is what the
# generated category covers are drawn with.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 libxslt1.1 libjpeg62-turbo zlib1g fonts-dejavu-core \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libxml2-dev libxslt1-dev libjpeg-dev zlib1g-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y gcc libxml2-dev libxslt1-dev libjpeg-dev zlib1g-dev \
    && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY app ./app

ENV RSSOPDS_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
