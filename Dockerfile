FROM python:3.12-slim

# Flush logs immediately and skip .pyc files for cleaner container output.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first so this layer is cached unless requirements change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY . .

# Run as an unprivileged user; keep the SQLite database on a mounted volume.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data \
    && chown app:app /data
ENV DB_PATH=/data/games.db
USER app
VOLUME ["/data"]

CMD ["python", "bot.py"]
