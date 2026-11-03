#!/usr/bin/env bash
# Docker restarts a container that exits, but not one that is running and unhealthy
# (hung). Cron runs this every minute to restart those (NFR-08).
set -euo pipefail

docker ps --filter health=unhealthy --filter label=com.graphnix.autoheal=true \
  --format '{{.Names}}' | while read -r name; do
  logger -t graphnix-autoheal "Restarting unhealthy container ${name}"
  docker restart "${name}" >/dev/null
done
