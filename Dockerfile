# NIFTY options system. Paper mode unless you deliberately go live (see README).
# Base image is pinned to a specific Python line and Debian release (not "latest").
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    TZ=Asia/Kolkata

# tzdata so log timestamps are in Indian time. Nothing else is installed system-wide.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

# Run as an ordinary user, never root.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin trader
WORKDIR /app

# 1) Dependencies, at the exact tested versions (cached unless requirements.lock changes).
COPY requirements.lock ./
RUN pip install -r requirements.lock

# 2) The program itself.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps .

# 3) Default rules. docker-compose.yml mounts ./config read-only over this, so edits are made on the
#    host (and committed to git), never inside the container. Secrets are NOT baked in: .env is
#    passed at run time and is excluded by .dockerignore.
COPY config ./config

# Writable places (compose mounts volumes here so data survives rebuilds).
RUN mkdir -p /app/data /app/logs && chown -R trader:trader /app/data /app/logs
USER trader

EXPOSE 8000

# "Is the program answering?" Docker marks the container unhealthy (and, with an orchestrator, can restart it).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "trader"]
