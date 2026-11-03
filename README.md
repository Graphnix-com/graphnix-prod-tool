# Graphnix production tracker

Tracks every Graphnix job from quote request to shipping. Square stays the system of
record for money; this app is the system of record for where each job is.

Stack: Django 5.2 LTS · PostgreSQL 16 · Procrastinate (Postgres-backed job queue) ·
Docker Compose · Caddy · Microsoft Graph for email.

## Layout

| Path | What |
| --- | --- |
| `config/` | Django settings and URLs. Every deploy-specific value comes from `.env`. |
| `apps/accounts/` | Staff user model (email sign-in, table `app_user`). |
| `apps/core/` | `/healthz`, worker heartbeat, health panel at `/ops/health/`. |
| `apps/m365/` | Microsoft Graph client and the email backend (crash emails, later notifications). |
| `compose.yaml` | The app stack (db, web, worker), one copy per environment. |
| `deploy/caddy/` | The HTTPS front door shared by production and staging. |
| `deploy/deploy.sh` | Build, migrate and restart the checked-out commit. |
| `deploy/host/` | Server scripts: nightly backup, backup pull, hung-container restart, cron file. |
| `docs/server-setup.md` | One-time setup of the VPS, Microsoft 365 app and deploys. |

## Local development

Needs [uv](https://docs.astral.sh/uv/) and Docker.

```bash
cp .env.example .env              # then set SECRET_KEY to any long random string
docker compose -f compose.dev.yaml up -d
uv sync
uv run python manage.py migrate
uv run python manage.py createcachetable
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

In a second terminal, run the background worker:

```bash
uv run python manage.py procrastinate worker
```

Sign in at http://localhost:8000/admin/, then open http://localhost:8000/ops/health/.
With the `M365_*` values blank, emails print to the console instead of being sent.

## Checks (CI runs the same on every pull request)

```bash
uv run ruff check . && uv run ruff format --check .
uv run python manage.py makemigrations --check --dry-run
uv run pytest
```

## Deploys

- Merge to `main` → deploys to **staging** (`https://staging.jobs.graphnix.com`).
- Push a tag like `v0.1.0` → deploys to **production** (`https://jobs.graphnix.com`).

Both are off until the server and GitHub secrets are set up
(`docs/server-setup.md`, step 8).

## Safeguards (NFR-08)

There is no third-party monitoring. Instead:

1. **Crash emails.** Unhandled errors in web requests and failed background jobs are
   emailed to `CRASH_EMAIL_TO` through Microsoft Graph, at most once per fault every
   10 minutes.
2. **Automatic restarts.** Every container has a health check and restarts if it exits;
   a cron job restarts any that are running but unhealthy.
3. **Health panel** at `/ops/health/`: database, worker heartbeat, last nightly backup
   and last pull by the shop's backup server. Later steps add the Square webhook backlog
   and failed emails. Visible to superusers and anyone with "Can view the system health
   panel".
