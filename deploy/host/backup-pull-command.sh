#!/bin/sh
# Forced command for the backup-pull user's SSH key (see docs/server-setup.md).
# That key can do one thing: copy the backup files, read-only, with rsync.
case "$SSH_ORIGINAL_COMMAND" in
  "rsync --server --sender "*)
    # Read by the health panel ("Backup copied to the shop's backup server").
    date -u +%Y-%m-%dT%H:%M:%SZ > /srv/graphnix/backups/state/last_pull
    exec /usr/bin/rrsync -ro /srv/graphnix/backups/files/
    ;;
  *)
    echo "This key can only pull backups with rsync." >&2
    exit 1
    ;;
esac
