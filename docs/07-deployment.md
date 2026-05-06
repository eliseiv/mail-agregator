# 07. Deployment

Целевая среда — single-host Linux (например, Ubuntu 22.04 LTS) с Docker Engine 24+ и docker compose v2. Все компоненты упакованы в docker-compose.

---

## 1. Сервисы docker-compose

```mermaid
flowchart LR
    subgraph net[Docker network: mas-net]
        api[api<br/>:8080 internal]
        worker[worker<br/>no ports]
        pg[postgres<br/>:5432 internal]
        rd[redis<br/>:6379 internal]
        mn[minio<br/>:9000 internal<br/>:9001 console]
        cd[caddy<br/>:80, :443]
    end

    cd --> api
    api --> pg & rd & mn
    worker --> pg & rd & mn
```

| Сервис | Image | Restart | Ports (host:container) | Зависит от (depends_on healthy) |
| --- | --- | --- | --- | --- |
| `caddy` | `caddy:2.8-alpine` | always | `80:80, 443:443` | api |
| `api` | local build (`api.Dockerfile`) | always | (только internal) | postgres, redis, minio |
| `worker` | local build (`worker.Dockerfile`) | always | (нет) | postgres, redis, minio |
| `postgres` | `postgres:16-alpine` | always | (только internal) | — |
| `redis` | `redis:7.2-alpine` | always | (только internal) | — |
| `minio` | `minio/minio:RELEASE.2024-08-29T01-40-52Z` (или новее latest stable) | always | `9001:9001` (console, только если нужно; обычно за VPN) | — |

Все internal-порты в общей docker network `mas-net` (default bridge). Никаких host-port mapping для api/postgres/redis/minio:9000.

---

## 2. Volumes

| Volume | Сервис | Содержимое | Backup-критичность |
| --- | --- | --- | --- |
| `mas_pg_data` | postgres | `/var/lib/postgresql/data` | **Critical** — основные данные |
| `mas_minio_data` | minio | `/data` | **Critical** — вложения |
| `mas_redis_data` | redis | `/data` (если включить AOF) | Low — можно потерять (sessions релогинятся) |
| `mas_caddy_data` | caddy | `/data` (TLS certs) | Medium — рестор Let's Encrypt автоматически, но избегаем rate-limit |
| `mas_caddy_config` | caddy | `/config` | Low |

Все volumes — named volumes Docker.

---

## 3. Healthchecks

### `api`
```yaml
healthcheck:
  test: ["CMD", "python", "-c", "import urllib.request,sys; r=urllib.request.urlopen('http://localhost:8080/healthz'); sys.exit(0 if r.status==200 else 1)"]
  interval: 15s
  timeout: 5s
  retries: 3
  start_period: 20s
```

### `worker`
Worker — long-running asyncio process. Healthcheck на основе lock-файла, обновляемого scheduler'ом каждый тик:

```yaml
healthcheck:
  test: ["CMD", "python", "-c", "import time,os,sys; m=os.stat('/tmp/worker_alive').st_mtime; sys.exit(0 if (time.time()-m)<360 else 1)"]
  interval: 60s
  timeout: 5s
  retries: 3
  start_period: 30s
```
Worker пишет touch `/tmp/worker_alive` каждые 30 секунд (отдельный lightweight job в APScheduler).

### `postgres`
```yaml
healthcheck:
  test: ["CMD-SHELL", "pg_isready -U mas -d mail_aggregator"]
  interval: 10s
  timeout: 5s
  retries: 5
```

### `redis`
```yaml
healthcheck:
  test: ["CMD", "redis-cli", "PING"]
  interval: 10s
  timeout: 3s
  retries: 5
```

### `minio`
```yaml
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
  interval: 15s
  timeout: 5s
  retries: 5
```

### `caddy`
По умолчанию (process supervisor); опционально `caddy --pingback`.

---

## 4. Environment variables

Полный список. Devops-агент создаёт `.env.example` со всеми переменными (без значений секретов).

### Общие

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `APP_ENV` | `prod` | yes | `dev` или `prod`. Влияет на CSP, cookie Secure, `ENABLE_DOCS`, SSRF allowlist (см. `06-security.md` sec. 4). |
| `APP_BASE_URL` | `https://mail.example.com` | yes (prod) | Используется для генерации Message-ID и absolute redirects. |
| `LOG_LEVEL` | `INFO` | no | `DEBUG`/`INFO`/`WARNING`/`ERROR`. |
| `ENABLE_DOCS` | `false` | no | Если `true` и APP_ENV != prod — открывает `/docs` Swagger. В prod игнорируется. |

### База данных

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://mas:CHANGE_ME@postgres:5432/mail_aggregator` | yes | DSN. |
| `POSTGRES_USER` | `mas` | yes (для контейнера postgres) | |
| `POSTGRES_PASSWORD` | (random strong) | yes | |
| `POSTGRES_DB` | `mail_aggregator` | yes | |

### Redis

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `REDIS_URL` | `redis://redis:6379/0` | yes | |

### MinIO / S3

Две пары ключей: **root** (для администрирования сервиса MinIO и init-контейнера) и **app** (service account для приложения, привязан политикой только к bucket `mail-attachments`). Подробности — секция 12 ниже.

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `MINIO_ROOT_USER` | (random) | yes | Root-аккаунт для самого сервиса MinIO. Используется ТОЛЬКО контейнерами `minio` и `minio-bootstrap`. |
| `MINIO_ROOT_PASSWORD` | (random strong) | yes | Пароль root-аккаунта MinIO. |
| `MINIO_APP_ACCESS_KEY` | (random) | yes | Service account access key для приложения. Создаётся init-контейнером `minio-bootstrap` с политикой только на bucket `mail-attachments`. |
| `MINIO_APP_SECRET_KEY` | (random strong) | yes | Service account secret. |
| `S3_ENDPOINT_URL` | `http://minio:9000` | yes | Для `api`/`worker`. |
| `S3_ACCESS_KEY` | `${MINIO_APP_ACCESS_KEY}` | yes | Маппится на `MINIO_APP_ACCESS_KEY`. **Никогда** на root-ключ. |
| `S3_SECRET_KEY` | `${MINIO_APP_SECRET_KEY}` | yes | Маппится на `MINIO_APP_SECRET_KEY`. |
| `S3_BUCKET_NAME` | `mail-attachments` | yes | |
| `S3_REGION` | `us-east-1` | no | Для MinIO формальность. |

> **Принцип:** `api` и `worker` никогда не получают root-credentials MinIO. Компрометация app-ключа ограничена правами на единственный bucket (см. политику в секции 12).

### Crypto

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `MAIL_ENCRYPTION_KEY` | (none) | **yes** | base64 32 байта. Генерация: см. `06-security.md` sec. 10. |
| `MAIL_ENCRYPTION_KEY_PREV` | (none) | no | Только во время ротации. |

### Admin seed

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `ADMIN_LOGIN` | `admin` | yes | Username супер-админа. |
| `ADMIN_PASSWORD` | (none) | yes | Пароль супер-админа (используется только при первом seed). |

### Worker / sync

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `MAX_CONCURRENT_IMAP` | `10` | no | Размер semaphore (см. ADR-0013). |
| `WORKER_THREAD_POOL_SIZE` | `14` | no | Размер default ThreadPoolExecutor (= `MAX_CONCURRENT_IMAP + 4`). |
| `SYNC_INTERVAL_MINUTES` | `5` | no | Интервал sync_cycle. Не рекомендуется снижать ниже 3. |
| `RETENTION_DAYS` | `30` | no | TTL писем (см. ADR-0011). |
| `IMAP_TIMEOUT_SECONDS` | `60` | no | Per-account timeout (см. ADR-0013). |
| `INITIAL_SYNC_DAYS` | `30` | no | Окно при первом подключении. |
| `MAX_ATTACHMENT_BYTES` | `26214400` | no | 25 MiB. |

### Sessions / auth

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `SESSION_TTL_SECONDS` | `43200` | no | 12 часов sliding. |
| `SESSION_ABSOLUTE_TTL_SECONDS` | `604800` | no | 7 дней absolute. |
| `SETUP_SESSION_TTL_SECONDS` | `900` | no | 15 минут. |
| `COOKIE_DOMAIN` | (none) | no | Если задан — кладётся в Set-Cookie domain. |

### Caddy

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `CADDY_DOMAIN` | (none) | yes (prod) | FQDN для TLS (например, `mail.example.com`). |

---

## 5. Дочерние решения по контейнеризации

- **Multi-stage build** для `api` и `worker`:
  1. `python:3.12-slim` builder — устанавливает зависимости в venv.
  2. Финальный stage — копирует venv, app code, ENTRYPOINT.
- Запуск под non-root пользователем (UID 1000 или динамический).
- Read-only root filesystem где возможно (`read_only: true` в compose), `tmpfs:/tmp` для worker (lock-файл healthcheck).
- `cap_drop: [ALL]`.
- Для api: `gunicorn -k uvicorn.workers.UvicornWorker -w 2 app.main:app --bind 0.0.0.0:8080 --timeout 60 --graceful-timeout 30`.
- Для worker: `python -m worker.app.main`.
- `ulimit -c 0` (no core dumps — защита `MAIL_ENCRYPTION_KEY` от утечки).

---

## 6. Caddy — пример Caddyfile

```
{$CADDY_DOMAIN} {
    encode zstd gzip

    @static path /static/*
    handle @static {
        reverse_proxy api:8080
        header Cache-Control "public, max-age=86400"
    }

    handle {
        reverse_proxy api:8080 {
            header_up X-Forwarded-Proto {scheme}
            header_up X-Forwarded-For {remote_host}
        }
    }

    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
    }
}
```

Caddy сам берёт TLS у Let's Encrypt; нужен открытый порт 80 (для challenge) и 443.

В dev — заменить блок `{$CADDY_DOMAIN}` на `:8080` (без TLS) и опустить HSTS.

---

## 7. Bootstrapping

### Первый запуск

```bash
git clone <repo>
cd mail-aggregator
cp .env.example .env
# Отредактировать .env: MAIL_ENCRYPTION_KEY, ADMIN_PASSWORD, POSTGRES_PASSWORD,
#   MINIO_ROOT_USER, MINIO_ROOT_PASSWORD, MINIO_APP_ACCESS_KEY, MINIO_APP_SECRET_KEY,
#   CADDY_DOMAIN, APP_BASE_URL
docker compose up -d --build
docker compose logs -f api worker
```

Порядок при первом старте:
1. `minio` поднимается и проходит healthcheck.
2. `minio-bootstrap` (init-контейнер) создаёт bucket `mail-attachments`, политику `mas-app` и service account из `MINIO_APP_*`. Завершается с exit 0.
3. `api` ждёт `minio-bootstrap: service_completed_successfully`, `postgres: service_healthy`, `redis: service_healthy`, затем стартует:
   - Alembic migrations автоматически применяются (entrypoint script).
   - `seed_super_admin` отрабатывает идемпотентно (upsert пароля, см. модуль `auth` в `05-modules.md`).
   - `Storage.ensure_bucket` — defensive проверка `head_bucket`; bucket уже создан init-контейнером, шаг возвращается мгновенно.
4. `worker` стартует с теми же зависимостями.

UI доступен на `https://{CADDY_DOMAIN}/login`.

### Обновление (deploy)

```bash
git pull
docker compose build api worker
docker compose up -d api worker  # без рестарта postgres/redis/minio
```

Migrations применяются автоматически. **Важно**: писать migrations совместимо снизу-вверх (online schema changes; не блокирующие). Если миграция требует downtime — devops согласовывает окно.

### Откат

- При ошибке запуска новой версии: `docker compose tag api:previous && docker compose up -d api`. (Нюансы — ответственность devops; на первой итерации просто `git checkout <tag> && docker compose up -d --build`.)
- Откат migrations: Alembic поддерживает `downgrade`, но в проде применять с осторожностью; preferred — write-forward fix (новая миграция).

---

## 8. Резервное копирование

Cron на хосте (или systemd timer) — devops настраивает:

### Postgres
```bash
docker exec mas-postgres pg_dump -U mas -d mail_aggregator -F c -f /tmp/dump.bin
docker cp mas-postgres:/tmp/dump.bin /backups/pg/$(date +%F).bin
```
Cron: ежедневно в 02:00. Хранение 14 дней (rotate). Шифровать перед перемещением off-site (gpg).

### MinIO
```bash
docker run --rm -v mas_minio_data:/data -v /backups/minio:/out alpine tar czf /out/$(date +%F).tar.gz /data
```
Альтернатива — `mc mirror` на удалённый MinIO/S3.

### Redis
Не бэкапим (опционально AOF). Sessions восстанавливаются логином.

### Восстановление
1. Поднять чистый stack (без api/worker): `docker compose up -d postgres redis minio`.
2. Postgres restore: `docker exec -i mas-postgres pg_restore -U mas -d mail_aggregator -c < dump.bin`.
3. MinIO restore: распаковать tar в volume.
4. Установить `MAIL_ENCRYPTION_KEY` из off-site хранилища (без него всё бесполезно).
5. `docker compose up -d api worker`.

---

## 9. CI/CD pipeline (GitHub Actions)

### Workflow: `.github/workflows/ci.yml`

Триггеры: push, pull_request на `main`.

Стадии (jobs):

#### 1. `lint`
- Установить Python 3.12, ruff, mypy.
- `ruff check .`
- `ruff format --check .`
- `mypy backend worker shared`

#### 2. `test` (matrix? не нужно — одна версия Python)
- Service containers: `postgres:16-alpine`, `redis:7.2-alpine`, `minio/minio:RELEASE.2024-08-29T01-40-52Z`.
- Установить зависимости.
- Применить migrations.
- `pytest -v --cov=backend --cov=worker --cov=shared --cov-report=xml --cov-fail-under=75`.
- Upload coverage report (artifact).

#### 3. `build`
- Только при push в `main` или ручном trigger.
- `docker build -f deploy/api.Dockerfile -t api:${{ github.sha }} .`
- `docker build -f deploy/worker.Dockerfile -t worker:${{ github.sha }} .`
- (опционально) `trivy image` — security scan.

#### 4. `deploy` (опционально, в первой итерации — manual)
- Push images в registry (GHCR / Docker Hub).
- SSH на сервер, `docker compose pull && up -d`.

Конкретные YAML-файлы — ответственность devops.

### Quality gates

| Gate | Условие | Действие при fail |
| --- | --- | --- |
| Ruff | 0 нарушений | block PR |
| Mypy | 0 errors на core; warnings допустимы на тестах | block PR |
| Pytest | все green | block PR |
| Coverage | >= 75% (core) | block PR |
| Docker build | success для api + worker | block merge to main |

Все gates обязательны для merge.

---

## 10. Observability

### Логи

- Структурные JSON в stdout (см. ADR-0014).
- Сбор: `docker compose logs` или внешняя система (Loki / Datadog / etc. — за scope первой итерации).
- `request_id` в response headers и логах.

### Метрики

В первой итерации не реализуем (см. tech-debt **TD-003** в `100-known-tech-debt.md`). Reasoning: scope маленький, можно жить с logs grep + manual count.

Что желательно при появлении метрик:
- counter `mail_aggregator_sync_cycle_total{status="ok|fail"}`.
- histogram `mail_aggregator_sync_account_duration_seconds`.
- gauge `mail_aggregator_active_sessions`.
- counter `mail_aggregator_send_total{status="ok|smtp_fail|append_fail"}`.

### Trace

Не используем (scope не оправдывает). Корреляция через `request_id` / `cycle_id` достаточна.

### Alerts

Базовые (devops настраивает на хосте):
- Disk usage > 80% (для backups + minio).
- Container down > 5 min.
- `pg_dump` cron job failed.

---

## 11. Operational procedures

### 11.1 Смена пароля супер-админа

Супер-админ — единственный, заводится из env (`ADMIN_LOGIN` / `ADMIN_PASSWORD`). UI смены пароля для него отсутствует сознательно (см. `06-security.md` sec. 10). Процедура:

1. Обновить `.env` на сервере:
   ```
   ADMIN_PASSWORD=<новый_сильный_пароль>
   ```
   Файл `.env` имеет режим `chmod 600`, владелец — пользователь docker-runtime.
2. Перезапустить `api` и `worker`:
   ```
   docker compose restart api worker
   ```
3. При старте `api` отрабатывает `seed_super_admin`, который выполняет upsert: для записи с `username = ADMIN_LOGIN` обновляются `password_hash = argon2(ADMIN_PASSWORD)`, `is_admin=true`, `password_reset_required=false`, `lockout_until=NULL`, `failed_login_attempts=0` (инвариант модуля `auth`, см. `05-modules.md`).
4. Проверить: попытаться залогиниться под новым паролем; в логах `api` должно быть `event=admin_seed_password_updated` (или `admin_seed_applied`).

Нюансы:
- Старая активная сессия супер-админа остаётся валидной (Redis TTL не тронут). Если нужно её прибить — `docker compose exec redis redis-cli DEL session:<token>` или просто `FLUSHDB` (это сбросит ВСЕ сессии всех пользователей).
- Если новый пароль не удовлетворяет правилам силы пароля для обычного пользователя — это допустимо, валидация на этапе seed не применяется. Но рекомендуется выбирать пароль не короче 16 символов (без max-ограничения сверху).

### 11.2 Безопасность сервера

- Только SSH (key-based) и 80/443 (Caddy) открыты наружу.
- MinIO console (`:9001`) — закрыт firewall'ом, доступ только через VPN или SSH-tunnel.
- Регулярные обновления базовых образов (`docker compose pull` раз в неделю/месяц).
- `MAIL_ENCRYPTION_KEY` хранится в password manager / sealed env (не в git, не в shared чатах).
- `.env` файл на сервере — `chmod 600`, владелец — пользователь, под которым работает docker.

---

## 12. MinIO bootstrap (non-root service account для приложения)

См. также `06-security.md` sec. 12.

### Идея

MinIO стартует с парой `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` — это credentials для администрирования самого сервиса. Приложение (api/worker) ходит в MinIO **не** под root-ключом, а под отдельным service account `MINIO_APP_ACCESS_KEY` / `MINIO_APP_SECRET_KEY`, у которого есть доступ только к bucket `mail-attachments` (политика `bucket-rw`).

Service account и bucket создаются одноразовым init-контейнером `mas-minio-bootstrap` на базе `minio/mc`. Контейнер падает в "exit 0" после успешной настройки и не запускается повторно (либо безопасно повторяется — все операции идемпотентны).

### docker-compose сервис (фрагмент)

```yaml
services:
  minio:
    image: minio/minio:RELEASE.2024-08-29T01-40-52Z
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
    volumes:
      - mas_minio_data:/data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 15s
      timeout: 5s
      retries: 5
    restart: always

  minio-bootstrap:
    image: minio/mc:RELEASE.2024-08-26T15-33-30Z
    depends_on:
      minio:
        condition: service_healthy
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
      MINIO_APP_ACCESS_KEY: ${MINIO_APP_ACCESS_KEY}
      MINIO_APP_SECRET_KEY: ${MINIO_APP_SECRET_KEY}
      S3_BUCKET_NAME: ${S3_BUCKET_NAME}
    entrypoint: ["/bin/sh", "-c"]
    command:
      - |
        set -eu
        mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
        mc mb --ignore-existing local/"$S3_BUCKET_NAME"
        cat > /tmp/policy.json <<EOF
        {
          "Version": "2012-10-17",
          "Statement": [
            {
              "Effect": "Allow",
              "Action": [
                "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                "s3:ListBucket", "s3:GetBucketLocation"
              ],
              "Resource": [
                "arn:aws:s3:::$S3_BUCKET_NAME",
                "arn:aws:s3:::$S3_BUCKET_NAME/*"
              ]
            }
          ]
        }
        EOF
        mc admin policy create local mas-app /tmp/policy.json || mc admin policy update local mas-app /tmp/policy.json
        # service account идемпотентно: создаём, если нет; иначе обновляем secret
        if mc admin user svcacct info local "$MINIO_APP_ACCESS_KEY" >/dev/null 2>&1; then
          mc admin user svcacct edit local "$MINIO_APP_ACCESS_KEY" --secret-key "$MINIO_APP_SECRET_KEY" --policy /tmp/policy.json
        else
          mc admin user svcacct add local "$MINIO_ROOT_USER" --access-key "$MINIO_APP_ACCESS_KEY" --secret-key "$MINIO_APP_SECRET_KEY" --policy /tmp/policy.json
        fi
        echo "minio-bootstrap done"
    restart: "no"

  api:
    # ... остальное как раньше
    environment:
      S3_ENDPOINT_URL: http://minio:9000
      S3_ACCESS_KEY: ${MINIO_APP_ACCESS_KEY}
      S3_SECRET_KEY: ${MINIO_APP_SECRET_KEY}
      S3_BUCKET_NAME: ${S3_BUCKET_NAME}
    depends_on:
      minio-bootstrap:
        condition: service_completed_successfully
```

### Соответствие env-переменных

- `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` — credentials самого MinIO-сервиса. Используются ТОЛЬКО контейнером `minio` и `minio-bootstrap`.
- `MINIO_APP_ACCESS_KEY`, `MINIO_APP_SECRET_KEY` — credentials service account для приложения. `api` и `worker` используют **только их** (`S3_ACCESS_KEY=$MINIO_APP_ACCESS_KEY`, `S3_SECRET_KEY=$MINIO_APP_SECRET_KEY`).
- root-ключи **не** маппятся в `S3_ACCESS_KEY`/`S3_SECRET_KEY` приложения.

`.env.example` девопс обновляет: добавить отдельные блоки для root и app пар. См. также secs. 4 этого документа — таблица env-переменных уже отражает это разделение.

### Идемпотентность

Все `mc` команды идемпотентны:
- `mc mb --ignore-existing` — bucket создаётся один раз.
- `mc admin policy create ... || update` — политика создаётся или обновляется.
- service account — пере-проверяется через `info`, при наличии — `edit`, иначе `add`.

Безопасно перезапустить compose-проект сколько угодно раз.

---

## 13. Известные ограничения первой итерации

См. также `100-known-tech-debt.md`.

- Single-host deployment. HA не реализована.
- Один worker — single point of failure. Допустимо для текущего scope (TD-006).
- Нет автоматического failover для Postgres (TD-007).
- Нет метрик Prometheus (TD-003).
- Нет orphan-scan для MinIO (TD-004).
- UI отправки не поддерживает аттачи (TD-005).
- Нет CAPTCHA на login (TD-008).
- Нет обратной IMAP-синхронизации флагов read/seen (TD-010).

---
