FROM python:3.12-slim-bookworm

# ───── System packages (keep minimal) ───────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# ───── Python dependencies ──────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps               

# ───── Project code ─────────────────────────────────────────────────
COPY . .

# ───── Runtime env vars ─────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1
ENV DJANGO_SETTINGS_MODULE=horseproj.settings

# ───── Launch Django via Gunicorn (Render sets $PORT) ───────────────
CMD ["sh", "-c", "gunicorn horseproj.wsgi:application --bind 0.0.0.0:$PORT"]
