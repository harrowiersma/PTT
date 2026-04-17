#!/bin/sh
set -e

# Run Alembic migrations before starting the app. Handles three database states:
#   1. Fresh DB (no tables):      `upgrade head` creates everything from migrations.
#   2. Pre-Alembic DB (tables exist, no alembic_version):  stamp head first,
#      then `upgrade head`. Retrofits an existing deployment into Alembic.
#   3. Already-migrated DB:        `upgrade head` is a no-op or runs new migrations.

cd /app/server
# Ensure `from server.config import ...` resolves both in the probe below
# and in Alembic's env.py (which also lives under /app/server/alembic).
export PYTHONPATH="/app:${PYTHONPATH}"

STATE=$(python3 - <<'PY'
import psycopg2
from server.config import settings

dsn = settings.database_url_sync.replace("postgresql+psycopg2://", "postgresql://", 1)
conn = psycopg2.connect(dsn)
cur = conn.cursor()
cur.execute("SELECT to_regclass('public.alembic_version')")
alembic_version_exists = cur.fetchone()[0] is not None
cur.execute("SELECT to_regclass('public.users')")
users_exists = cur.fetchone()[0] is not None
cur.close()
conn.close()

if users_exists and not alembic_version_exists:
    print("pre_alembic")
elif not users_exists and not alembic_version_exists:
    print("fresh")
else:
    print("migrated")
PY
)

case "$STATE" in
    pre_alembic)
        echo "entrypoint: pre-Alembic database detected; stamping to head"
        alembic stamp head
        ;;
    fresh)
        echo "entrypoint: fresh database; running all migrations"
        ;;
    migrated)
        echo "entrypoint: database already under Alembic control"
        ;;
    *)
        echo "entrypoint: could not determine database state ($STATE); aborting" >&2
        exit 1
        ;;
esac

alembic upgrade head

exec uvicorn server.main:app --host 0.0.0.0 --port 8000
