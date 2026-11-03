#!/usr/bin/env bash
# Nightly backup of one environment: database dump + uploaded files, encrypted with age.
# The server can encrypt but not decrypt; Graphnix holds the private key offline.
# The shop's backup server pulls /srv/graphnix/backups/files and keeps 30 days.
#
#   backup.sh production
set -euo pipefail
umask 027

ENV="${1:?usage: backup.sh production|staging}"
ROOT=/srv/graphnix
APP_DIR="$ROOT/$ENV"
OUT="$ROOT/backups/files/$ENV"
STATE="$ROOT/backups/state"
RECIPIENTS="$ROOT/backups/recipients.txt"
KEEP_DAYS="${KEEP_DAYS:-7}"

mkdir -p "$OUT" "$STATE"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
cd "$APP_DIR"

# Written as .part and renamed at the end, so a pull never copies a half-written file.
docker compose exec -T db pg_dump -U graphnix -d graphnix --format=custom \
  | age -R "$RECIPIENTS" > "$OUT/db-$stamp.dump.age.part"
mv "$OUT/db-$stamp.dump.age.part" "$OUT/db-$stamp.dump.age"

docker compose exec -T web tar -C /app -cf - media \
  | age -R "$RECIPIENTS" > "$OUT/media-$stamp.tar.age.part"
mv "$OUT/media-$stamp.tar.age.part" "$OUT/media-$stamp.tar.age"

find "$OUT" -name '*.age' -mtime +"$KEEP_DAYS" -delete
find "$OUT" -name '*.part' -mmin +120 -delete

# Read by the health panel.
date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE/last_backup_$ENV"
chmod 644 "$STATE/last_backup_$ENV"
echo "$(date -u +%FT%TZ) backup ok: $ENV $stamp"
