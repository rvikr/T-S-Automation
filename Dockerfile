# Sentinel moderation service. Build from the repo root:
#   docker build -t sentinel .
# Runs the FastAPI service by default; override the command for the UI:
#   docker compose up            # api + ui (see docker-compose.yml)
FROM python:3.11-slim

# ffmpeg backs moviepy/pydub audio extraction (same as packages.txt / CI).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY sentinel/requirements.txt sentinel/requirements.txt
RUN pip install --no-cache-dir -r sentinel/requirements.txt

COPY sentinel sentinel
COPY pyproject.toml pytest.ini ./

# State lives on the /app/sentinel/db and /app/sentinel/data mount points
# (see docker-compose.yml). A non-root user owns them so named volumes inherit
# writable ownership on first use.
RUN useradd --create-home sentinel && chown -R sentinel:sentinel /app
USER sentinel

ENV SENTINEL_DB_PATH=/app/sentinel/db/audit.sqlite

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["uvicorn", "sentinel.api:app", "--host", "0.0.0.0", "--port", "8000"]
