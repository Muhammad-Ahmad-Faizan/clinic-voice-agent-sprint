FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# For `docker run` / docker-compose. python:3.12-slim has no curl, so use
# urllib. Render/Railway use their own health checks and set $PORT (handled
# by the CMD below), so the HEALTHCHECK port adjusts to the same env var.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'))"

# Render/Railway set PORT; default to 8000 for plain `docker run`.
# --workers 1 is intentional: the booking idempotency store is in-memory and
# would be sharded across multiple uvicorn processes.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
