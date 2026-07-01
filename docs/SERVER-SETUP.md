# Server setup — operator runbook

End-to-end checklist for bringing a fresh Linux host into production. Companion to `docs/07-deployment.md` (which is normative for architecture); this file is procedural.

Read it top-to-bottom on first install. After that, only Parts D, E, F, G, H apply day-to-day. To move an existing prod to a new host, use `docs/07-deployment.md` section 15 «Migration to a new server».

> **Actual prod (since 2026-07-01).** Dedicated Hetzner server `49.12.189.77`, Ubuntu 26.04, domain `postapp.store` (A-record at reg.ru). Deploy SSH user is **`root`**, checkout path is **`/opt/mail-agregator`** (repo name — one `g`). The GitHub deploy secrets are `DEPLOY_HOST` / `DEPLOY_USER=root` / `DEPLOY_KEY_B64` (base64 PEM) / `DEPLOY_PATH=/opt/mail-agregator`. GHCR images are **public** — the host does **not** run `docker login`. TLS = **host-level certbot** (`/etc/letsencrypt` + `certbot.timer`, webroot `/var/www/certbot`) bind-mounted into the nginx container; there is **no** certbot container. The generic steps below still use placeholders like `mail.example.com` / `deploy` user — they document the pattern; substitute the actual values above.

---

## Part A. One-time host preparation

### A.1 Minimum requirements

- Ubuntu 22.04 LTS or newer (actual prod: Ubuntu 26.04 on Hetzner; Debian 12 also works; CentOS Stream/Rocky require minor tweaks for `ufw` → `firewalld`).
- 2 vCPU, 2 GiB RAM, 20 GiB disk (more if you store >5k messages with attachments).
- Public IPv4 address. IPv6 optional.
- Open firewall ports outward to the internet:
  - `22/tcp` SSH (restrict by source IP if possible).
  - `80/tcp` HTTP (Let's Encrypt http-01 + 301 redirect).
  - `443/tcp` HTTPS (real traffic).

### A.2 DNS

Create an `A` (and optionally `AAAA`) record. Actual prod: `postapp.store` → `49.12.189.77`, managed at **reg.ru**.

```
mail.example.com.  IN A  <server-public-ip>
# actual: postapp.store.  IN A  49.12.189.77
```

Wait for propagation:

```bash
dig +short mail.example.com
# actual: dig +short postapp.store  → must return 49.12.189.77
# must return the server IP before continuing — Let's Encrypt validates DNS
```

### A.3 Install Docker and host certbot

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker
docker --version
docker compose version

# host-level certbot (TLS is handled on the host, NOT in a container — see Part B)
sudo apt-get update && sudo apt-get install -y certbot
sudo install -d -m 755 /var/www/certbot
```

### A.4 Deploy user

The CI workflow logs in over SSH. The generic/recommended pattern is a dedicated non-root `deploy` user in the docker group:

```bash
sudo adduser --disabled-password --gecos "" deploy
sudo usermod -aG docker deploy
sudo install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
sudo install -m 600 -o deploy -g deploy /dev/null /home/deploy/.ssh/authorized_keys
```

> **Actual prod deviation.** The current prod deploys as **`root`** (`DEPLOY_USER=root`), so `authorized_keys` for the deploy public key lives in `~root/.ssh/authorized_keys` and no separate user is created. A dedicated `deploy` user remains the preferred hardening; the runbook keeps `deploy` in examples, but substitute `root` to match the live secret.

### A.5 Generate the deploy SSH key (on your laptop, not the server)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/mail-aggregator-deploy -C "github-actions-deploy" -N ""
```

Append the **public** key to the server:

```bash
ssh-copy-id -i ~/.ssh/mail-aggregator-deploy.pub deploy@<server-ip>
# OR manually:
# cat ~/.ssh/mail-aggregator-deploy.pub | ssh deploy@<ip> 'cat >> ~/.ssh/authorized_keys'
```

Test:

```bash
ssh -i ~/.ssh/mail-aggregator-deploy deploy@<server-ip> 'docker info | head -5'
```

The **private** key goes into the GitHub Secret **`DEPLOY_KEY_B64`** — the workflow expects it **base64-encoded** (single line, no whitespace the GitHub UI can mangle) and decodes it with `base64 -d` at runtime. Produce the value with:

```bash
base64 -w0 ~/.ssh/mail-aggregator-deploy   # Linux
# base64 -i ~/.ssh/mail-aggregator-deploy | tr -d '\n'   # macOS
```

Actual prod uses the key `~/.ssh/postapp-deploy` on the operator side. See Part C.

### A.6 Firewall (UFW)

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo ufw status verbose
```

If your provider supplies a security group / cloud firewall, replicate the same allow-list there too.

### A.7 Clone the repo to /opt/mail-agregator

Actual prod path is `/opt/mail-agregator` (repo name — one `g`; matches `DEPLOY_PATH`).

```bash
sudo install -d -o deploy -g deploy /opt/mail-agregator
sudo -u deploy git clone https://github.com/<owner>/<repo>.git /opt/mail-agregator
# actual prod (root user): git clone https://github.com/<owner>/<repo>.git /opt/mail-agregator
```

Replace `<owner>/<repo>` with the actual GitHub path.

### A.8 Create the production .env

```bash
cd /opt/mail-agregator
sudo -u deploy cp .env.example .env
sudo -u deploy chmod 600 .env
sudo -u deploy nano .env
```

Set, at a minimum:

| Key | Notes |
| --- | --- |
| `APP_ENV` | `prod` |
| `APP_BASE_URL` | `https://mail.example.com` (must match `SERVER_DOMAIN`; actual: `https://postapp.store`) |
| `SERVER_DOMAIN` | `mail.example.com` (actual: `postapp.store`) |
| `ACME_EMAIL` | a real address you monitor — Let's Encrypt sends expiry warnings |
| `POSTGRES_PASSWORD` | `openssl rand -base64 32 \| tr -d '/+=' \| head -c 32` |
| `MINIO_ROOT_USER` | `openssl rand -hex 16` |
| `MINIO_ROOT_PASSWORD` | `openssl rand -base64 48 \| tr -d '/+=' \| head -c 40` |
| `MINIO_APP_ACCESS_KEY` | `openssl rand -hex 16` |
| `MINIO_APP_SECRET_KEY` | `openssl rand -base64 48 \| tr -d '/+=' \| head -c 40` |
| `S3_ACCESS_KEY` | copy of `MINIO_APP_ACCESS_KEY` |
| `S3_SECRET_KEY` | copy of `MINIO_APP_SECRET_KEY` |
| `MAIL_ENCRYPTION_KEY` | `python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"` — **back this up off-host** |
| `ADMIN_PASSWORD` | strong (>= 16 chars) — used once to seed the super-admin |
| `IMAGE_REGISTRY` | `ghcr.io/<owner>/<repo>` (lowercase!) |
| `IMAGE_TAG` | `latest` for now; CI/deploy.yml will rewrite to a sha later |

`DATABASE_URL` defaults work — compose substitutes `${POSTGRES_PASSWORD}` automatically.

### A.9 GHCR access — no login needed (images are public)

The project's GHCR packages are **public**, so `docker compose pull` reads `ghcr.io/<owner>/<repo>/{api,worker}` **anonymously**. The deploy workflow does not log in, and neither must the host — there is **no** `~/.docker/config.json` on prod, and pulls work as-is.

Only if the packages are ever switched to **private** do you need to cache a credential on the host:

1. On github.com: profile → Developer settings → Personal access tokens (classic) → scope `read:packages`. Copy it.
2. On the server, as the deploy user (actual prod: `root`):

   ```bash
   echo "<token>" | docker login ghcr.io -u <github-username> --password-stdin
   ```

   Credentials land in `~/.docker/config.json` (`chmod 600`). Not required while packages stay public.

### A.10 (Optional) systemd unit for unattended host reboots

Compose already restarts containers on docker daemon restart (`restart: unless-stopped`). No extra unit is required for v1.

---

## Part B. First start + first TLS certificate

The server is now ready, but no images are pulled and no cert exists. Order matters — the nginx container refuses to start without a cert, and the **host** certbot can't bind port 80 while nginx is running. We bootstrap in three phases. TLS is handled by **host-level certbot** (installed in A.3), not by any container.

### B.1 Pull and start the data tier

```bash
cd /opt/mail-agregator
docker compose pull postgres redis minio                 # alpine images, fast
docker compose up -d postgres redis minio minio-bootstrap
docker compose run --rm mas-migrations
docker compose up -d api worker
docker compose ps
```

All five services should be reported `healthy` (or `exited (0)` for `mas-migrations` / `minio-bootstrap`). If `api` or `worker` won't start because the GHCR image isn't pulled yet:

```bash
docker compose --profile prod pull api worker            # pulls ghcr.io/<owner>/<repo>/{api,worker}:latest
docker compose up -d api worker
```

If `IMAGE_TAG=latest` doesn't exist yet (no merge to main has happened), build locally just for the bootstrap:

```bash
docker compose build api worker
```

### B.2 Acquire the first cert (host certbot, standalone mode)

The host certbot needs port 80. The nginx container is not started yet, so the port is free.

```bash
sudo certbot certonly --standalone \
  -d "$(grep '^SERVER_DOMAIN=' .env | cut -d= -f2)" \
  --email "$(grep '^ACME_EMAIL=' .env | cut -d= -f2)" \
  --agree-tos --no-eff-email --non-interactive
```

Expected output ends with `Successfully received certificate.` Cert files now live on the **host** at `/etc/letsencrypt/live/<domain>/` — the nginx container reads them via the read-only bind-mount `/etc/letsencrypt:/etc/letsencrypt:ro`.

If this fails:
- `Connection refused on port 80` — UFW or cloud firewall blocking. Re-check Part A.6.
- `DNS problem: NXDOMAIN looking up A for <domain>` — DNS not propagated. Re-run `dig` from Part A.2.
- `too many failed authorizations` — Let's Encrypt rate-limited you (5 fails/hour). Wait, then fix the underlying problem before retrying.

### B.3 Bring up nginx (the only prod-profile service)

```bash
docker compose --profile prod up -d nginx
docker compose ps
```

`nginx` should be `healthy` within 10s. There is **no** certbot container — renewal is the host `certbot.timer` (see Part G.1). Switch the domain's renewal to **webroot** so future renewals don't need nginx stopped: ensure `/etc/letsencrypt/renewal/<domain>.conf` has `authenticator = webroot` and `webroot_path = /var/www/certbot`, then install the deploy-hook from Part G.1.

### B.4 End-to-end smoke test

```bash
# Plain text /healthz behind TLS — should return 200.
curl -sSI https://mail.example.com/healthz | head -1
# Expect: HTTP/2 200

# HSTS header is present.
curl -sSI https://mail.example.com/healthz | grep -i strict-transport
# Expect: strict-transport-security: max-age=63072000; includeSubDomains; preload

# HTTP redirects to HTTPS.
curl -sSI http://mail.example.com/ | head -1
# Expect: HTTP/1.1 301 Moved Permanently

# Login page renders.
curl -sS https://mail.example.com/login | grep -o '<title>[^<]*</title>'
```

The web UI is now live at `https://mail.example.com/login`.

---

## Part C. GitHub Actions setup

### C.1 Add repo secrets

Repo on github.com → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value | Actual prod value |
| --- | --- | --- |
| `DEPLOY_HOST` | server public IP or DNS | `49.12.189.77` (the **only** secret changed on 2026-07-01; was `132.243.113.117`) |
| `DEPLOY_USER` | ssh login user | `root` |
| `DEPLOY_KEY_B64` | **base64** of the private SSH key (single line; workflow runs `base64 -d`). NOT the raw PEM, and the secret name is `DEPLOY_KEY_B64`, not `DEPLOY_KEY`. | base64 of operator key `~/.ssh/postapp-deploy` |
| `DEPLOY_PATH` | absolute checkout path on the server | `/opt/mail-agregator` |

> The workflow (`.github/workflows/deploy.yml`) reads exactly these four names. `echo "${{ secrets.DEPLOY_KEY_B64 }}" | base64 -d > ~/.ssh/deploy_key` is the actual decode step — a raw-PEM `DEPLOY_KEY` secret would not work.

### C.2 (Optional but recommended) Branch protection on main

Settings → Branches → Add rule for `main`:

- Require pull request reviews before merging.
- Require status checks before merging:
  - `Lint (ruff)`
  - `Type-check (mypy)`
  - `Test (pytest + coverage ≥ 75%)`
  - `Build images (api)`
  - `Build images (worker)`
- Optionally restrict who can push directly to main.

### C.3 (Optional) Required reviewers on prod environment

Settings → Environments → New environment → `prod`:
- Required reviewers: yourself / a teammate.

This pauses every `Deploy` run for manual approval — useful before you trust the pipeline.

### C.4 Trigger the first deploy

Push any change to main. The CI workflow runs lint/type/test/build and pushes images to GHCR. The Deploy workflow then waits for green CI, SSHes in, pulls the new images, and recreates `api` + `worker`. Watch in the **Actions** tab.

---

## Part D. Smoke-test after every deploy

Run on the server (or via SSH from your laptop):

```bash
cd /opt/mail-agregator

# 1. Every container reports healthy / exited(0)
docker compose ps

# 2. Public health endpoint returns 200 over TLS
curl -fsS https://mail.example.com/healthz
# expect: 200, body contains "ok" / status JSON

# 3. nginx access log — last 50 non-200 responses (should be empty or only 401/404)
docker logs mas-nginx --tail 50 | awk '$9 !~ /^(200|301|302|304)$/'

# 4. api error scan
docker logs mas-api --tail 100 2>&1 | grep -iE 'error|traceback' || echo "clean"

# 5. worker liveness — /tmp/worker_alive must be < 6 min old
docker compose exec worker stat -c '%y' /tmp/worker_alive
```

If the api isn't healthy after a deploy, check `docker logs mas-api` first. The deploy workflow asserts healthcheck for 90s and exits 1 on timeout, leaving the previous containers running.

---

## Part E. Updating the service

### E.1 Standard path (CI/CD)

`git push origin main` — that's it. CI builds, deploy.yml SSHes in. Watch Actions, then run Part D.

### E.2 Manual path (when CI is broken or unavailable)

```bash
ssh deploy@mail.example.com        # actual prod: ssh root@49.12.189.77 (or root@postapp.store)
cd /opt/mail-agregator
git pull origin main
# IMAGE_TAG should track the sha you want; if you didn't push to main, build locally:
docker compose build api worker
# Otherwise pull the latest GHCR image (the one CI pushed):
docker compose --profile prod pull api worker
docker compose --profile prod up -d --remove-orphans api worker
docker compose ps
```

### E.3 Rollback

Easiest: re-run the Deploy workflow with `workflow_dispatch` and pass the previous green sha as the `sha` input. The workflow will rewrite `IMAGE_TAG` in `.env` accordingly.

Manually:

```bash
cd /opt/mail-agregator
git checkout <previous-sha>
sed -i "s|^IMAGE_TAG=.*|IMAGE_TAG=<previous-sha>|" .env
docker compose --profile prod pull api worker
docker compose --profile prod up -d api worker
```

Migrations are forward-only (see `deploy/README.md`). If a migration was applied that the previous code can't read, write a forward-fix migration; do **not** `alembic downgrade` in prod.

---

## Part F. Backups

### F.1 PostgreSQL — daily

Add to the host's crontab (`sudo crontab -e -u deploy`):

```cron
0 2 * * * cd /opt/mail-agregator && docker exec mas-postgres pg_dump -U mas -d mail_aggregator -F c | gzip > /opt/backups/pg/$(date +\%F).dump.gz && find /opt/backups/pg -mtime +14 -delete
```

```bash
sudo install -d -o deploy -g deploy /opt/backups/pg
```

Encrypt with `gpg --symmetric` before moving off-host (the dump contains user data and encrypted mail-account password ciphertexts; combined with `MAIL_ENCRYPTION_KEY` they are decryptable).

### F.2 MinIO (attachments) — daily

```cron
0 3 * * * docker run --rm -v mas_minio_data:/data:ro -v /opt/backups/minio:/out alpine tar czf /out/$(date +\%F).tar.gz -C /data . && find /opt/backups/minio -mtime +14 -delete
```

For larger installs, prefer `mc mirror` to a remote S3.

### F.3 .env

Critical. Bypass git entirely — copy `/opt/mail-agregator/.env` into your password manager / 1Password / Vault. Without `MAIL_ENCRYPTION_KEY` the postgres dump is useless.

### F.4 TLS cert — host `/etc/letsencrypt`

Non-critical to back up: if lost, re-run Part B.2 — Let's Encrypt re-issues. Just don't loop because of the rate-limit (50 certs / week / domain). When **migrating** to a new host, however, copy `/etc/letsencrypt` verbatim (`tar` it, preserve symlinks `live/ → archive/`) so you skip re-issuance and keep renewal working — see `docs/07-deployment.md` section 15.4. There is no `mas_certbot_certs` docker volume; certs live on the host.

### F.5 Restore drill

Test once a month against a throwaway VM, per `docs/07-deployment.md` sec. 8. A backup that has never been restored is a wish, not a backup.

---

## Part G. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `nginx: cannot load certificate /etc/letsencrypt/live/...` | first cert never acquired | run Part B.2 |
| `502 Bad Gateway` from nginx | api is down/unhealthy | `docker logs mas-api`; `docker compose ps` |
| Browser shows old TLS cert after renewal | nginx wasn't reloaded after host renewal | `docker compose --profile prod exec nginx nginx -s reload` (ad-hoc); ensure the renewal deploy-hook from Part G.1 is installed |
| `certbot renew` fails with `connection refused` | UFW blocked port 80 | re-check `sudo ufw status` |
| `certbot renew` fails with `too many requests` | LE rate-limited (5 fails/h, 50 issuances/wk) | wait + fix root cause |
| `docker compose pull` returns `unauthorized: authentication required` | packages went private and host has no credential | packages are normally **public** (no login needed); if flipped to private, re-run Part A.9 |
| `Permission denied (publickey)` from GitHub Actions | wrong/missing `DEPLOY_KEY_B64` (must be base64) or wrong `DEPLOY_USER` | verify both secrets, re-test with `ssh -i` from your laptop |
| api is healthy but login returns 500 | `MAIL_ENCRYPTION_KEY` rotated incorrectly (stored ciphertext can't decrypt) | restore previous key into `MAIL_ENCRYPTION_KEY_PREV` and read `docs/06-security.md` sec. 10 |

### G.1 nginx reload on cert renewal (host certbot)

Host `certbot.timer` renews silently in the background; the nginx **container** must then be told to pick up the new cert. Preferred trigger — a certbot **deploy-hook** that fires only on actual renewal:

```bash
sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh >/dev/null <<'EOF'
#!/bin/sh
cd /opt/mail-agregator && docker compose --profile prod exec -T nginx nginx -s reload
EOF
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
```

`nginx -s reload` is graceful — no dropped connections. As a belt-and-braces fallback you may also add a weekly host cron (`sudo crontab -e`, actual prod user `root`):

```cron
# Fallback: reload nginx weekly so a rotated cert goes live even if the deploy-hook was missed.
0 4 * * 1 cd /opt/mail-agregator && docker compose --profile prod exec -T nginx nginx -s reload >/dev/null 2>&1
```

Certs are valid for 90 days; certbot renews at 30 days remaining; the deploy-hook (and weekly fallback) comfortably covers the 60-day rotation window.

---

## Part H. Secret rotation

### H.1 `MAIL_ENCRYPTION_KEY` — yearly (or after any suspected leak)

Follow the formal rotation procedure in `docs/06-security.md` sec. 10. **Do not** simply replace the value — the existing ciphertexts in postgres are encrypted with the old key and become un-decryptable.

### H.2 `ADMIN_PASSWORD`

```bash
cd /opt/mail-agregator
sudo -u deploy nano .env                     # set new ADMIN_PASSWORD
docker compose restart api worker
```

`seed_super_admin` runs at api start and upserts the new hash. The old session in Redis remains valid until its TTL — clear it explicitly if needed:

```bash
docker compose exec redis redis-cli --scan --pattern 'session:*' | xargs -r docker compose exec redis redis-cli DEL
```

### H.3 `POSTGRES_PASSWORD`

Postgres password change is a 2-step:

```bash
# 1. Inside postgres, change the role's password to the new value
docker compose exec postgres psql -U mas -d mail_aggregator \
  -c "ALTER USER mas WITH PASSWORD '<new-strong-password>';"

# 2. Update .env so future container restarts use the new value, then restart api/worker
sudo -u deploy nano .env                     # update POSTGRES_PASSWORD and DATABASE_URL
docker compose restart api worker
```

The postgres container itself does not pick up env changes for an existing role — only step 1 actually changes the password. Don't `docker compose up -d --force-recreate postgres` casually; it would re-init only on a fresh volume.

### H.4 TLS cert

Auto-rotated by the host certbot. Your only operational item is the renewal deploy-hook (weekly `nginx -s reload` fallback) from Part G.1.

### H.5 GHCR PAT

Not applicable while packages are **public** — the host stores no GHCR credential (Part A.9). Only relevant if packages are switched to private, in which case rotate the `read:packages` PAT via Part A.9.

### H.6 SSH deploy key

Rotate yearly:

1. Generate a new key on your laptop (Part A.5).
2. Append the new public key to the deploy user's `authorized_keys` on the server (actual prod: `~root/.ssh/authorized_keys`). **Don't remove the old one yet.**
3. Update the `DEPLOY_KEY_B64` GitHub secret with the **base64** of the new private key (`base64 -w0 <keyfile>`).
4. Trigger a manual deploy and confirm it succeeds.
5. Now remove the old public key from `authorized_keys`.

---

## Quick reference

```bash
# Status
docker compose ps
curl -fsSI https://mail.example.com/healthz

# Logs
docker logs --tail 100 mas-api
docker logs --tail 100 mas-worker
docker logs --tail 100 mas-nginx
journalctl -u certbot --no-pager | tail -100   # host certbot renewal (no mas-certbot container)

# Reload nginx after cert renewal (host certbot; normally the deploy-hook does this)
docker compose --profile prod exec nginx nginx -s reload

# Manual deploy (when CI is unavailable)
git pull origin main
docker compose --profile prod pull api worker
docker compose --profile prod up -d --remove-orphans api worker
```
