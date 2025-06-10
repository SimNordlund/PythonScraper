FROM python:3.12-slim

# ───── Playwright & system libraries ─────────────────────────────────
RUN apt-get update && apt-get install -y \
        curl gnupg ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_current.x | bash - \
    && apt-get install -y nodejs \
       libnss3 libatk1.0-0 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
       libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpangocairo-1.0-0 \
       libgtk-3-0 libpango-1.0-0 libasound2 \
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
CMD sh -c "gunicorn horseproj.wsgi:application --bind 0.0.0.0:$PORT"
