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
COPY schema.sql .

# Render provides $PORT; default for local runs
ENV PORT=8000

# On boot: ensure schema exists, then serve.
# (schema.sql is idempotent — safe to run on every deploy.)
CMD sh -c 'psql "$DATABASE_URL" -f schema.sql >/dev/null 2>&1; exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"'
