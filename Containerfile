FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for Playwright + uv
RUN apt-get update && apt-get install -y \
    wget \
    curl \
    ca-certificates \
    gnupg \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager / venv tool)
COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /usr/local/bin/uv

# Create non-root runtime user. Playwright + Firefox happily run unprivileged
# and there is no reason to expose the host root namespace to a scraper.
# Also chown /app to the new user: WORKDIR created it as root, and `COPY
# --chown` below only chowns the copied files (not the directory itself),
# so without this `uv sync` would fail trying to mkdir /app/.venv.
RUN useradd --create-home --uid 1000 --shell /bin/bash app \
    && chown app:app /app

# Sync dependencies (production-only — no dev extras) into a venv at /app/.venv
# owned by the app user.
COPY --chown=app:app pyproject.toml uv.lock ./
USER app
ENV HOME=/home/app \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"
RUN uv sync --frozen --no-dev

# Install Playwright Firefox as the runtime user so its browser cache lives
# in the user's home dir.
RUN playwright install firefox

USER root
COPY --chown=app:app app/ ./app/
COPY --chown=app:app templates/ ./templates/
COPY --chown=app:app static/ ./static/
# Alembic configuration + migration scripts. init_db() calls
# `alembic upgrade head` on startup so these MUST be present in the image,
# or the lifespan crashes with "No 'script_location' key found in
# configuration." Don't rely on the data volume mount for these.
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations/ ./migrations/
RUN mkdir -p /app/data /app/browser-profile \
    && chown -R app:app /app

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
