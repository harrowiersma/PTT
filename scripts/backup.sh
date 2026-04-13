#!/usr/bin/env bash
# =============================================================================
# PTT Server Daily Backup
# Backs up PostgreSQL + Murmur data to /opt/ptt/backups/
# Keeps 30 days of daily backups, auto-prunes older ones.
#
# Usage: Run via cron: 0 3 * * * /opt/ptt/scripts/backup.sh
# =============================================================================
set -euo pipefail

PTT_DIR="${PTT_DIR:-/opt/ptt}"
BACKUP_DIR="$PTT_DIR/backups"
DATE=$(date +%Y-%m-%d)
BACKUP_PATH="$BACKUP_DIR/$DATE"
KEEP_DAYS=30

mkdir -p "$BACKUP_PATH"

echo "[$(date)] Starting PTT backup..."

# Backup PostgreSQL
echo "  Backing up PostgreSQL..."
docker compose -f "$PTT_DIR/docker-compose.yml" exec -T postgres \
    pg_dump -U ptt ptt > "$BACKUP_PATH/postgres.sql" 2>/dev/null

if [ -s "$BACKUP_PATH/postgres.sql" ]; then
    echo "  PostgreSQL: $(wc -c < "$BACKUP_PATH/postgres.sql") bytes"
else
    echo "  WARNING: PostgreSQL backup is empty!"
fi

# Backup Murmur SQLite
echo "  Backing up Murmur data..."
docker compose -f "$PTT_DIR/docker-compose.yml" cp \
    murmur:/data/mumble-server.sqlite "$BACKUP_PATH/murmur.sqlite" 2>/dev/null || true

# Backup .env (contains secrets)
cp "$PTT_DIR/.env" "$BACKUP_PATH/env.backup" 2>/dev/null || true

# Compress
echo "  Compressing..."
tar -czf "$BACKUP_DIR/ptt-backup-$DATE.tar.gz" -C "$BACKUP_DIR" "$DATE" 2>/dev/null
rm -rf "$BACKUP_PATH"

echo "  Backup: $BACKUP_DIR/ptt-backup-$DATE.tar.gz ($(du -h "$BACKUP_DIR/ptt-backup-$DATE.tar.gz" | cut -f1))"

# Prune old backups
echo "  Pruning backups older than $KEEP_DAYS days..."
find "$BACKUP_DIR" -name "ptt-backup-*.tar.gz" -mtime +$KEEP_DAYS -delete 2>/dev/null || true

REMAINING=$(ls -1 "$BACKUP_DIR"/ptt-backup-*.tar.gz 2>/dev/null | wc -l | tr -d ' ')
echo "  Backups retained: $REMAINING"

echo "[$(date)] Backup complete."
