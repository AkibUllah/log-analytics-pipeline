FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LOGPIPE_DB_PATH=/data/logpipe.duckdb \
    LOGPIPE_CHECKPOINT_PATH=/data/logpipe.checkpoint.json

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /data && chown app:app /data

COPY pyproject.toml README.md ./
COPY logpipe ./logpipe
RUN pip install --upgrade pip && pip install ".[api]"

USER app
VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["logpipe"]
CMD ["--help"]
