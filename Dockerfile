FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CHAT_DB=/data/chat.db

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py db.py ./
COPY templates ./templates
COPY static ./static

# Run as a non-root user; /data holds the SQLite database (mount a volume here).
RUN useradd --system --uid 10001 --no-create-home app \
    && mkdir /data \
    && chown app /data
USER app
VOLUME /data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# One worker (SQLite + a single process) with threads, so streaming replies don't block each other.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", \
     "--timeout", "120", "--access-logfile", "-", "app:app"]
