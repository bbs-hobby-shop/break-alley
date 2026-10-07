FROM python:3.12-slim

WORKDIR /srv/app

# System deps (postgresql-client for schema init; curl for healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY templates/ templates/
COPY static/ static/
COPY schema.sql .

# Render provides $PORT; default for local runs
ENV PORT=8000

# On boot: ensure schema exists (with timeout, non-fatal), then serve.
# (schema.sql is idempotent — safe to run on every deploy.)
# Brian 2026-10-07: psql must never block uvicorn startup. If the DB is
# unreachable, log it and start anyway — a 502 from no port is worse than
# a 500 from no DB.
CMD sh -c 'timeout 15 psql "$DATABASE_URL" -f schema.sql 2>&1 | head -20; exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"'
