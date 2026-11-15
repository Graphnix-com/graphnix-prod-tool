#!/bin/sh
# Runs on the shop's Synology NAS, from DSM Task Scheduler as root, nightly at 04:00 local.
# Pulls the encrypted Graphnix backups from the VPS and keeps 30 days of copies.
# See docs/server-setup.md, step 7.
#
#   sh pull-graphnix-backups.sh production
set -eu

ENV="${1:-production}"
BASE="${BASE:-/volume1/graphnix-backups}"
CONF="$BASE/_config"
DEST="$BASE/$ENV"
KEEP_DAYS=30

mkdir -p "$DEST"

# No --delete: the VPS keeps 7 days, this copy keeps 30.
rsync -a --no-owner --no-group --exclude '*.part' \
  -e "ssh -i $CONF/id_ed25519 -o UserKnownHostsFile=$CONF/known_hosts -o StrictHostKeyChecking=yes -o BatchMode=yes" \
  "backup-pull@jobs.graphnix.com:$ENV/" "$DEST/"

find "$DEST" -type f -name '*.age' -mtime +"$KEEP_DAYS" -delete
echo "$(date -u +%FT%TZ) pull ok: $ENV"
