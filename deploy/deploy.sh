#!/usr/bin/env bash
# Build and start the commit that is currently checked out in this environment's folder.
#
#   cd /srv/graphnix/staging
#   git fetch origin && git checkout --detach <commit or tag>
#   ./deploy/deploy.sh
#
# CI does exactly this over SSH. To roll back, check out the previous commit and run
# it again (a database migration that already ran is not undone).
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "No .env here. Copy .env.example to .env and fill it in first." >&2
  exit 1
fi

GIT_SHA="$(git rev-parse --short HEAD)"
export GIT_SHA
echo "Deploying ${GIT_SHA} ($(grep '^APP_ENV=' .env | cut -d= -f2))"

docker network inspect graphnix-edge >/dev/null 2>&1 || docker network create graphnix-edge

docker compose build --pull web
docker compose up -d --wait db
docker compose run --rm --no-deps web python manage.py migrate --noinput
docker compose run --rm --no-deps web python manage.py createcachetable
docker compose up -d --remove-orphans --wait --wait-timeout 300
docker image prune -f >/dev/null

echo "Deployed ${GIT_SHA}"
