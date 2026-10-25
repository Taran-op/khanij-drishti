FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    KD_DATA_DIR=/var/lib/khanij-drishti/data \
    KD_MODELS_DIR=/var/lib/khanij-drishti/models

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl libgomp1 libpq5 postgresql-client && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# Install the CPU wheel explicitly; the default Linux PyPI wheel may pull several GB of CUDA
# libraries even though this deployment performs CPU inference/training.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch>=2.3" && \
    pip install --no-cache-dir -r requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 && \
    rm -rf /var/lib/apt/lists/*
COPY kd ./kd
COPY seed /seed
COPY docker-entrypoint.sh /usr/local/bin/kd-entrypoint

RUN useradd --create-home --uid 10001 kd && \
    mkdir -p /var/lib/khanij-drishti/data /var/lib/khanij-drishti/models && \
    chmod 755 /usr/local/bin/kd-entrypoint && \
    chown -R kd:kd /app /var/lib/khanij-drishti
USER kd

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/ready || exit 1
ENTRYPOINT ["kd-entrypoint"]
CMD ["sh", "-c", "exec uvicorn kd.api:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
