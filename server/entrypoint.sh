#!/bin/sh
set -e

# Run Alembic migrations before starting the app. Handles three database states:
#   1. Fresh DB (no tables):      `upgrade head` creates everything from migrations.
#   2. Pre-Alembic DB (tables exist, no alembic_version):  stamp head first,
#      then `upgrade head`. Retrofits an existing deployment into Alembic.
#   3. Already-migrated DB:        `upgrade head` is a no-op or runs new migrations.

cd /app/server

python3 - <<'PY'
import sys
import psycopg2
from server.config import settings

dsn = settings.database_url_sync.replace("postgresql+psycopg2://", "postgresql://", 1)

try:
    conn = psycopg2.connect(dsn)
except psycopg2.OperationalError as e:
    # Database not ready yet. The admin container depends_on postgres but that
    # only waits for the container to start, not for postgres to accept queries.
    # Fail loudly; Docker will restart us.
    print(f"entrypoint: cannot reach database: {e}", file=sys.stderr)
    sys.exit(1)

cur = conn.cursor()
cur.execute("SELECT to_regclass('public.alembic_version')")
alembic_version_exists = cur.fetchone()[0] is not None
cur.execute("SELECT to_regclass('public.users')")
users_exists = cur.fetchone()[0] is not None
cur.close()
conn.close()

if users_exists and not alembic_version_exists:
    print("entrypoint: pre-Alembic database detected; stamping to head")
    sys.exit(2)
elif not users_exists and not alembic_version_exists:
    print("entrypoint: fresh database; will run all migrations")
    sys.exit(0)
else:
    print("entrypoint: database already under Alembic control")
    sys.exit(0)
PY
RC=$?

if [ "$RC" = "2" ]; then
    alembic stamp head
fi

alembic upgrade head

exec uvicorn server.main:app --host 0.0.0.0 --port 8000
