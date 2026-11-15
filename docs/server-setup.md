# Server setup (one time)

Picks up after the VPS is hardened, Docker is installed and the `jobs` and
`staging.jobs` DNS records resolve. Run commands on the VPS as your admin user
unless a step says otherwise.

## 1. Folders and the deploy user

The `deploy` user is what GitHub Actions signs in as. Being in the `docker` group gives it
root-level control of the server, so its SSH key lives only in GitHub's secrets.

```bash
sudo adduser --disabled-password --gecos "" deploy
sudo usermod -aG docker deploy
sudo mkdir -p /srv/graphnix
sudo chown deploy:deploy /srv/graphnix
```

## 2. Let the server read the repository

```bash
sudo -iu deploy bash -c 'mkdir -p -m 700 ~/.ssh && ssh-keygen -t ed25519 -N "" -f ~/.ssh/github -C "graphnix-vps deploy key"'
sudo cat /home/deploy/.ssh/github.pub
```

In GitHub: repository → **Settings → Deploy keys → Add deploy key**, paste the key,
leave **Allow write access** off. Then:

```bash
sudo -iu deploy bash -c 'printf "Host github.com\n  IdentityFile ~/.ssh/github\n" >> ~/.ssh/config'
sudo -iu deploy git clone git@github.com:Graphnix-com/graphnix-prod-tool.git /srv/graphnix/production
sudo -iu deploy git clone git@github.com:Graphnix-com/graphnix-prod-tool.git /srv/graphnix/staging
```

## 3. Environment files

Create `/srv/graphnix/production/.env` and `/srv/graphnix/staging/.env` from
`.env.example` (`sudo -iu deploy cp .env.example .env`, then edit), and
`chmod 600` both. The values that differ:

| Setting | production | staging |
| --- | --- | --- |
| `APP_ENV` | `production` | `staging` |
| `COMPOSE_PROJECT_NAME` | `graphnix-production` | `graphnix-staging` |
| `DEBUG` | `false` | `false` |
| `SECRET_KEY` | its own random value | its own random value |
| `ALLOWED_HOSTS` | `jobs.graphnix.com` | `staging.jobs.graphnix.com` |
| `CSRF_TRUSTED_ORIGINS` | `https://jobs.graphnix.com` | `https://staging.jobs.graphnix.com` |
| `POSTGRES_PASSWORD` | its own random value | its own random value |
| `DATABASE_URL` | `postgres://graphnix:<that password>@db:5432/graphnix` | same pattern |
| `M365_SENDER` | `info@graphnix.com` | `tracker-test@graphnix.com` |

Generate random values with `openssl rand -base64 36 | tr -d '/+='`. Store every
value in the password manager.

## 4. Caddy (HTTPS front door)

```bash
sudo -iu deploy bash -c '
  docker network create graphnix-edge
  mkdir -p /srv/graphnix/caddy
  cp /srv/graphnix/production/deploy/caddy/* /srv/graphnix/caddy/
  echo "ACME_EMAIL=josh@graphnix.com" > /srv/graphnix/caddy/.env
  cd /srv/graphnix/caddy && docker compose up -d'
```

Until the app is deployed, the two sites answer with a 502 error. That is expected.

## 5. First deploy (by hand)

```bash
sudo -iu deploy bash -c 'cd /srv/graphnix/staging && ./deploy/deploy.sh'
sudo -iu deploy bash -c 'cd /srv/graphnix/staging && docker compose exec web python manage.py createsuperuser'
```

Check `https://staging.jobs.graphnix.com/ops/health/`. Repeat for production when ready.

## 6. Hung-container restarts and backups

**Encryption key.** On a trusted PC (not the server), install
[age](https://github.com/FiloSottile/age) and run `age-keygen -o graphnix-backup.key`.
Store that file in the password manager; without it, no backup can be restored. Copy the
`Public key: age1...` line it prints.

```bash
sudo apt install -y age rsync
sudo groupadd --system backup-pull
sudo mkdir -p /srv/graphnix/backups/files /srv/graphnix/backups/state
sudo chown root:backup-pull /srv/graphnix/backups/files
sudo chmod 2750 /srv/graphnix/backups/files
echo "age1...your public key..." | sudo tee /srv/graphnix/backups/recipients.txt
sudo cp /srv/graphnix/production/deploy/host/graphnix.cron /etc/cron.d/graphnix
```

Test a backup now: `sudo /srv/graphnix/production/deploy/host/backup.sh production`.

**Restore test** (on the trusted PC, with a downloaded backup):

```bash
age -d -i graphnix-backup.key db-<stamp>.dump.age > restore.dump
pg_restore --list restore.dump | head
```

## 7. The shop's backup server pulls the backups

The pull user can only copy `/srv/graphnix/backups/files`, read-only, with rsync over SSH.
The shop's server connects out to the VPS, so the shop firewall needs no changes.

```bash
sudo adduser --system --ingroup backup-pull --shell /bin/sh --home /var/lib/backup-pull backup-pull
sudo install -d -m 700 -o backup-pull -g backup-pull /var/lib/backup-pull/.ssh
sudo install -m 755 /srv/graphnix/production/deploy/host/backup-pull-command.sh /usr/local/bin/backup-pull-command
sudo touch /srv/graphnix/backups/state/last_pull
sudo chown backup-pull /srv/graphnix/backups/state/last_pull
which rrsync   # expect /usr/bin/rrsync
```

If `/etc/ssh/sshd_config` (or a file in `sshd_config.d/`) has an `AllowUsers` or
`AllowGroups` line from hardening, add `backup-pull` (and `deploy`) to it and run
`sudo systemctl reload ssh`.

### On the shop's Synology NAS

The NAS pulls nightly with `deploy/shop-nas/pull-graphnix-backups.sh` and keeps 30 days.
The paths below assume `volume1`; adjust if the NAS uses another volume.

1. **DSM → Control Panel → Shared Folder → Create** `graphnix-backups`. Tick
   *Hide this shared folder in "My Network Places"*. Permissions: administrators
   Read/Write, everyone else No access.
2. **Control Panel → Terminal & SNMP**: tick *Enable SSH service* (you can turn it off
   again at the end; the nightly task does not need it).
3. From your PC, `ssh <dsm admin>@<nas address>`, then `sudo -i`, and:
   ```bash
   mkdir -m 700 /volume1/graphnix-backups/_config
   cd /volume1/graphnix-backups/_config
   ssh-keygen -t ed25519 -N "" -f id_ed25519 -C shop-backup-server
   ssh-keyscan -t ed25519 jobs.graphnix.com > known_hosts
   ssh-keygen -lf known_hosts
   cat id_ed25519.pub
   ```
   The fingerprint must match `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the
   VPS. The key can only download already-encrypted files, so it stays on the NAS.
4. On the VPS, authorize that public key (all on one line):
   ```bash
   echo 'command="/usr/local/bin/backup-pull-command",restrict ssh-ed25519 AAAA... shop-backup-server' | sudo tee /var/lib/backup-pull/.ssh/authorized_keys
   sudo chown backup-pull:backup-pull /var/lib/backup-pull/.ssh/authorized_keys
   sudo chmod 600 /var/lib/backup-pull/.ssh/authorized_keys
   ```
5. Back on the NAS, create `/volume1/graphnix-backups/_config/pull-graphnix-backups.sh`
   with the contents of `deploy/shop-nas/pull-graphnix-backups.sh` (`vi` is built in),
   then test it:
   ```bash
   sh /volume1/graphnix-backups/_config/pull-graphnix-backups.sh staging
   ls -l /volume1/graphnix-backups/staging
   ```
   The health panel's "Backup copied to the shop's backup server" row turns OK.
6. **Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script**:
   - General: task `Pull Graphnix backups`, user `root`.
   - Schedule: daily, **04:00** (NAS local time; that is after the 09:30 UTC backup in
     both summer and winter).
   - Task Settings: tick *Send run details by email* and *only when the script
     terminates abnormally* (needs DSM's email notifications set up). Run command:
     `sh /volume1/graphnix-backups/_config/pull-graphnix-backups.sh production`
     (use `staging` until production is live).
7. If the NAS has a Hyper Backup task to the cloud or a USB drive, add the
   `graphnix-backups` folder to it for an off-site copy.

**Restore test:** copy one `db-*.dump.age` from the share to your PC and run the restore
test from step 6.

## 8. Turn on automatic deploys

1. On the VPS, make a key for GitHub Actions and allow it for `deploy`:
   ```bash
   sudo -iu deploy bash -c 'ssh-keygen -t ed25519 -N "" -f ~/.ssh/actions -C github-actions && cat ~/.ssh/actions.pub >> ~/.ssh/authorized_keys'
   sudo -iu deploy cat ~deploy/.ssh/actions        # private key, for GitHub only
   ssh-keyscan -t ed25519 jobs.graphnix.com        # for DEPLOY_KNOWN_HOSTS
   ```
   Then delete `~deploy/.ssh/actions` (the private key) from the server.
2. In GitHub: **Settings → Environments**, create `staging` and `production`. For
   `production`, add yourself as a required reviewer. In each, add the secrets
   `DEPLOY_HOST` (`jobs.graphnix.com`), `DEPLOY_USER` (`deploy`), `DEPLOY_SSH_KEY`
   and `DEPLOY_KNOWN_HOSTS`.
3. **Settings → Secrets and variables → Actions → Variables**: add `DEPLOY_ENABLED` =
   `true`.

## 9. Microsoft 365 app (crash emails, later customer email)

1. entra.microsoft.com → **App registrations → New registration**: name
   `Graphnix Tracker`, single tenant. Note the **Application (client) ID** and
   **Directory (tenant) ID**.
2. **Certificates & secrets → New client secret**, 24 months. Copy the value (shown
   once) into the password manager and both `.env` files. Put the expiry date in your
   calendar.
3. Give it permission to send only from `info@` and `tracker-test@`, using Exchange
   RBAC for Applications. Do **not** also add `Mail.Send` under API permissions in
   Entra: that grant covers every mailbox and would override the limit. In Exchange
   Online PowerShell (`Connect-ExchangeOnline -Device`, PowerShell 7):
   ```powershell
   New-ServicePrincipal -AppId <client id> -ObjectId <enterprise app object id> -DisplayName "Graphnix Tracker"
   New-ManagementScope -Name "Graphnix tracker mailboxes" -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'info@graphnix.com' -or PrimarySmtpAddress -eq 'tracker-test@graphnix.com'"
   New-ManagementRoleAssignment -App <client id> -Role "Application Mail.Send" -CustomResourceScope "Graphnix tracker mailboxes"
   Test-ServicePrincipalAuthorization -Identity <client id> -Resource tracker-test@graphnix.com
   ```
   The object ID comes from **Enterprise applications** (not App registrations). Changes
   can take up to an hour to apply.
4. Fill `M365_TENANT_ID`, `M365_CLIENT_ID`, `M365_CLIENT_SECRET` in both `.env` files and
   redeploy. Test from the server:
   ```bash
   cd /srv/graphnix/staging && docker compose exec web python manage.py sendtestemail --admins
   ```
