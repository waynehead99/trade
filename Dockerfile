FROM python:3.11-slim

# tzdata so ZoneInfo("America/New_York") resolves; curl for the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so layer is cached when only code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY app.py trade.py capitol.py db.py place_stops.py ./
COPY templates/ ./templates/
COPY static/ ./static/

# Persistent data dir — mount a volume here so trades.db survives restarts.
RUN mkdir -p /data
ENV DB_PATH=/data/trades.db \
    HOST=0.0.0.0 \
    PORT=5000 \
    PYTHONUNBUFFERED=1

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fs http://127.0.0.1:5000/api/status || exit 1

CMD ["python", "app.py"]
