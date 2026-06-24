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
        ng[nginx<br/>:80, :443]
        cb[certbot<br/>renewal loop]
    end

    ng --> api
    api --> pg & rd & mn
    worker --> pg & rd & mn
    cb -. webroot+certs .- ng
```

| Сервис | Image | Restart | Ports (host:container) | Зависит от (depends_on healthy) |
| --- | --- | --- | --- | --- |
| `nginx` | `nginx:1.27-alpine` | unless-stopped | `80:80, 443:443` | api |
| `certbot` | `certbot/certbot:v2.11.0` | unless-stopped | (нет) | — (shares two volumes with nginx) |
| `api` | local build / GHCR (`deploy/Dockerfile` target `api`) | unless-stopped | (только internal) | postgres, redis, minio, minio-bootstrap, mas-migrations |
| `worker` | local build / GHCR (`deploy/Dockerfile` target `worker`) | unless-stopped | (нет) | postgres, redis, minio, minio-bootstrap, mas-migrations |
| `postgres` | `postgres:16-alpine` | unless-stopped | (только internal) | — |
| `redis` | `redis:7.2-alpine` | unless-stopped | (только internal) | — |
| `minio` | `minio/minio:RELEASE.2024-08-29T01-40-52Z` | unless-stopped | `127.0.0.1:9001:9001` (console, dev; в prod закрыт за firewall/VPN) | — |

Все internal-порты в общей docker network `mas-net` (default bridge). Никаких host-port mapping для api/postgres/redis/minio:9000.

`nginx` и `certbot` запускаются только под `--profile prod` — в dev их нет, api публикуется на `127.0.0.1:8080` через `docker-compose.override.yml`.

---

## 2. Volumes

| Volume | Сервис | Содержимое | Backup-критичность |
| --- | --- | --- | --- |
| `mas_pg_data` | postgres | `/var/lib/postgresql/data` | **Critical** — основные данные |
| `mas_minio_data` | minio | `/data` | **Critical** — вложения |
| `mas_redis_data` | redis | `/data` (если включить AOF) | Low — можно потерять (sessions релогинятся) |
| `mas_certbot_certs` | certbot (RW), nginx (RO) | `/etc/letsencrypt` (account keys + `live/<domain>/{fullchain,privkey,chain}.pem`) | Medium — рестор Let's Encrypt автоматически, но избегаем rate-limit (5 fails/час, 50 certs/нед на домен) |
| `mas_certbot_webroot` | certbot (RW), nginx (RO) | `/var/www/certbot` (HTTP-01 challenge files) | Low — пересоздаётся при каждом renewal |

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

### `nginx`
```yaml
healthcheck:
  test: ["CMD", "wget", "-qO-", "http://127.0.0.1/_health_nginx"]
  interval: 30s
  timeout: 5s
  retries: 3
  start_period: 5s
```
Server-блок отвечает `200 ok\n` на `/_health_nginx` (HTTP, не HTTPS — чтобы не зависеть от наличия cert).

### `certbot`
Healthcheck не настроен — это бесконечный sleep-цикл renewal. Достаточно `restart: unless-stopped`. Логи: `docker logs mas-certbot`.

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
| `SYNC_MAX_CONSECUTIVE_FAILURES` | `3` | no | ADR-0026: порог PERMANENT-ошибок подряд → auto-disable (`ge=1, le=20`). Заменяет хардкод `_DISABLE_AFTER_FAILS`. |
| `SYNC_MASS_FAILURE_RATIO` | `0.5` | no | ADR-0026: доля PERMANENT-падений за цикл, при которой circuit-breaker подавляет массовый disable (`ge=0.0, le=1.0`). |
| `SYNC_MASS_FAILURE_MIN` | `5` | no | ADR-0026: минимум аккаунтов в цикле для активации circuit-breaker (`ge=1, le=10000`). |
| `SYNC_CONNECT_RETRIES` | `3` | no | ADR-0026 (update): повторы открытия IMAP-соединения/login на DNS/connection-ошибках И спорадических transient IMAP-ошибках, backoff 0.5s/1.0s/2.0s (`ge=0, le=10`). Также число retry OAuth-`login failed` (ADR-0028 §3). |
| `SYNC_TRANSIENT_SUPPRESS_MINUTES` | `60` | no | ADR-0026 (update): подавлять запись TRANSIENT `last_sync_error` в UI, если последний успешный sync был в пределах окна (`ge=0, le=10080`; `0` отключает). Распространяется на OAuth-`login failed` (ADR-0028 §6). |
| `SYNC_OAUTH_LOGIN_FAILED_TRANSIENT` | `true` | no | ADR-0028 §7: **обязательный** kill-switch (default-on; `no` = переопределять не требуется, дефолт активирует фикс). При `true` (дефолт) IMAP-`login failed`/`authenticationfailed` у `oauth_outlook` = transient (retry + no-disable). `false` возвращает старое permanent-поведение (откат без редеплоя кода). |

### Sessions / auth

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `SESSION_TTL_SECONDS` | `43200` | no | 12 часов sliding. |
| `SESSION_ABSOLUTE_TTL_SECONDS` | `604800` | no | 7 дней absolute. |
| `SETUP_SESSION_TTL_SECONDS` | `900` | no | 15 минут. |
| `COOKIE_DOMAIN` | (none) | no | Если задан — кладётся в Set-Cookie domain. |

### Reverse proxy + TLS (nginx + certbot)

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `SERVER_DOMAIN` | (none) | yes (prod) | FQDN для TLS (например, `mail.example.com`). nginx envsubst-ит в `default.conf`; certbot использует для запроса cert. |
| `ACME_EMAIL` | `admin@example.com` | yes (prod) | Контакт для Let's Encrypt — приходят уведомления о истечении сертификата + reset-ссылки для аккаунта LE. |

### Telegram bot (ADR-0018 launcher + ADR-0022 SSO/notifications)

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_ENABLED` | `false` | no | Если `false` — webhook-роут регистрируется и валидирует secret, но не вызывает Bot API; диспатчер нотификаций также пропускает доставку. Включается одноразово после deploy + `setWebhook`. |
| `TELEGRAM_BOT_TOKEN` | (none) | yes (если enabled) | Bot-token от BotFather. Маскируется в structlog redact-list рядом с `MAIL_ENCRYPTION_KEY` (см. ADR-0014, `06-security.md` §1.8). **TD-014 cleanup (ADR-0022 migration plan)**: код переименовывается с `BOT_TOKEN` на `TELEGRAM_BOT_TOKEN` (alias на переходный период); devops синхронизирует prod `.env`. |
| `TELEGRAM_WEBHOOK_SECRET` | (none) | yes (если enabled) | 32 hex-символа, генерация: `openssl rand -hex 16`. Используется и в URL-path webhook'а, и в header `X-Telegram-Bot-Api-Secret-Token` (двойная проверка). |
| `TELEGRAM_WEBAPP_URL` | (none) | yes (если enabled) | URL, который бот вкладывает в `web_app.url` inline-кнопки **`/start` launcher** (открывает приложение как Telegram WebApp). **НЕ** используется кнопкой уведомления «Посмотреть сообщение» — она `callback_data "msg:{id}"` (Bug-fix #5, ADR-0022 §2.5), тело письма приходит в чат через webhook callback, без WebApp-URL. (Также residual web-route `/messages/{id}?embed=tg` строит URL из этого хоста при навигации внутри WebApp.) Prod: `https://postapp.store`. Dev: ngrok URL (Telegram требует HTTPS). |
| `TG_AUTH_INIT_DATA_TTL_SEC` | `300` | no | TTL (секунды) для `auth_date` в `init_data` при `POST /api/telegram/auth` (ADR-0022 §1.2). |
| `TG_PENDING_COOKIE_TTL_SEC` | `900` | no | TTL (секунды) cookie `mas_tg_pending` и Redis ключа `tg_pending:{token}` (ADR-0022 §1.2). |
| `TG_NOTIFY_BATCH_SIZE` | `30` | no | Сколько items LPOP'ит `tg_notify_dispatch` за один тик (ADR-0022 §2.4). |
| `TG_NOTIFY_DISPATCH_INTERVAL_SEC` | `5` | no | Интервал APScheduler для `tg_notify_dispatch` (ADR-0022 §2.4). |
| `TG_NOTIFY_RECOVERY_WINDOW_HOURS` | `24` | no | Окно (часы) для `tg_notify_recovery_scan` — не пытаемся доставить уведомления о письмах старше этого срока (ADR-0022 §2.8). |
| `TG_NOTIFY_ALL_MESSAGES` | `true` | no | round-31: `true` — уведомлять по ВСЕМ новым письмам; `false` — только письма с ≥1 тегом (историческое поведение). Откат — смена env + рестарт `worker` (lru-cache `get_settings`), без редеплоя кода (ADR-0022 §2.1/§2.2). |
| `TG_SEND_PER_CHAT_PER_MINUTE` | `20` | no | round-31: per-chat троттлинг доставки уведомлений (Redis `rl:tg_send:<chat_id>`, окно 60 сек). Диапазон 1..60. Защита от flood/429 при `TG_NOTIFY_ALL_MESSAGES=true` (ADR-0022 §2.9). |
| `TG_MAX_LINKS_PER_USER` | `10` | no | **ADR-0024 (Спринт A):** мягкий потолок числа активных TG-привязок на один internal user. Проверяется в `link_pending`/`POST /api/telegram/links` (`COUNT(active) < limit`); при достижении — `409 tg_link_limit`, audit `telegram_link_limit_reached`. Не DB-констрейнт. |
| `BOT_IVAN_TOKEN` | (none) | no | **ADR-0027:** токен push-only бота команды `ivan` (BotFather). **round-42:** нужен **и в worker** (доставка), **и в api** (обработка callback). Маскируется в structlog redact-list. Пустой → бот не настроен (тихо игнорируется). |
| `BOT_IVAN_GROUP_ID` | (none) | no | **ADR-0027:** `mail_accounts.group_id` команды `ivan`. Прод: `1`. Без него бот в `push_team_bots` не попадает. |
| `BOT_IVAN_WEBHOOK_SECRET` | (none) | no | **ADR-0027 round-42 §2/§10:** 32 hex (`openssl rand -hex 16`) — secret push-webhook'а бота `ivan` (header `X-Telegram-Bot-Api-Secret-Token`). Нужен **в api** (валидация callback). Пустой → у бота нет callback-кнопки (`with_button=False`, graceful degradation). Redact (рядом с `TELEGRAM_WEBHOOK_SECRET`). |
| `BOT_ALEXANDRA_TOKEN` | (none) | no | **ADR-0027:** токен push-only бота команды `alexandra`. **round-42:** в **worker + api**. Redact. |
| `BOT_ALEXANDRA_GROUP_ID` | (none) | no | **ADR-0027:** `group_id` команды `alexandra`. Прод: `2`. |
| `BOT_ALEXANDRA_WEBHOOK_SECRET` | (none) | no | **ADR-0027 round-42:** webhook-secret бота `alexandra` (см. `BOT_IVAN_WEBHOOK_SECRET`). Redact. |
| `BOT_ANDREI_TOKEN` | (none) | no | **ADR-0027:** токен push-only бота команды `andrei`. **round-42:** в **worker + api**. Redact. |
| `BOT_ANDREI_GROUP_ID` | (none) | no | **ADR-0027:** `group_id` команды `andrei`. Прод: `3`. |
| `BOT_ANDREI_WEBHOOK_SECRET` | (none) | no | **ADR-0027 round-42:** webhook-secret бота `andrei` (см. `BOT_IVAN_WEBHOOK_SECRET`). Redact. |
| `BOT_BUSINESS2_TOKEN` | (none) | no | **ADR-0027 round-44:** токен push-only бота команды `business2` (BotFather). Нужен **и в worker** (доставка), **и в api** (обработка callback) — как у прочих push-ботов round-42. Маскируется в structlog redact-list. Пустой → бот не настроен (тихо игнорируется). |
| `BOT_BUSINESS2_GROUP_ID` | (none) | no | **ADR-0027 round-44:** `mail_accounts.group_id` команды `business2`. В отличие от `ivan`/`alexandra`/`andrei` (`1`/`2`/`3`) — задаёт **оператор/devops** в prod `.env`; **обязан отличаться** от `1`/`2`/`3` (иначе fail-fast дубля `group_id` на старте, ADR-0027 §2) и совпадать с реальным `groups.id` команды `business2` (ADR-0019). Без него бот в `push_team_bots` не попадает. |
| `BOT_BUSINESS2_WEBHOOK_SECRET` | (none) | no | **ADR-0027 round-44 §2/§10:** 32 hex (`openssl rand -hex 16`) — secret push-webhook'а бота `business2` (header `X-Telegram-Bot-Api-Secret-Token`). Нужен **в api** (валидация callback). Пустой → у бота нет callback-кнопки (`with_button=False`, graceful degradation). Redact (рядом с `TELEGRAM_WEBHOOK_SECRET`). |
| `ADMIN_TELEGRAM_IDS` | (none) | no | **ADR-0027:** CSV Telegram chat id двух администраторов-получателей push-ботов, напр. `11111111,22222222`. Пусто → push-каналы выключены (`push_team_bots_enabled=false`). Не секрет, но в логах оставлять только конкретный `chat_id` доставки. |
| `PUSH_NOTIFY_DISPATCH_INTERVAL_SECONDS` | `5` | no | **ADR-0027:** интервал APScheduler-job `push_notify_dispatch` (drain `push_notify_queue`). |
| `PUSH_NOTIFY_BATCH_SIZE` | `30` | no | **ADR-0027:** размер `LPOP`-батча `push_notify_dispatch` за тик. |
| `OUTLOOK_CLIENT_ID` | (none) | yes (если OAuth enabled) | **ADR-0025 (Спринт B):** Azure App (Application/client) ID. Audience: «Accounts in any organizational directory and personal Microsoft accounts» (multitenant + personal — обязательно для tenant `common`). |
| `OUTLOOK_CLIENT_SECRET` | (none) | yes (если OAuth enabled) | **ADR-0025:** Azure App client secret. Маскируется в structlog redact-list рядом с `MAIL_ENCRYPTION_KEY`/`TELEGRAM_BOT_TOKEN`. |
| `OUTLOOK_REDIRECT_URI` | (none) | yes (если OAuth enabled) | **ADR-0025:** `{APP_BASE_URL}/api/oauth/outlook/callback` — точное совпадение с зарегистрированным в Azure. |
| `OUTLOOK_TENANT` | `common` | no | **ADR-0025:** tenant для authorize/token endpoints `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/...`. Для личных ящиков — `common` (ранее `consumers`; `consumers` давал IMAP XOAUTH2 "User is authenticated but not connected"). `common` пускает и личные, и рабочие аккаунты. |
| `OUTLOOK_OAUTH_STATE_TTL_SECONDS` | `600` | no | **ADR-0025:** TTL Redis-ключа `oauth_state:{state}` (CSRF/anti-fixation state + PKCE verifier). |

OAuth включён (`OUTLOOK_OAUTH_ENABLED`, derived), когда заданы `OUTLOOK_CLIENT_ID` + `OUTLOOK_CLIENT_SECRET`; иначе `/api/oauth/outlook/*` отдают `404` (route скрыт). `OUTLOOK_CLIENT_SECRET` передаётся в **api** (authorize/callback/SMTP) и **worker** (refresh перед IMAP). `TELEGRAM_BOT_TOKEN` хранится в `.env` (`chmod 600`). **Изменение от ADR-0018:** `worker` теперь использует Telegram API для доставки push-нотификаций (ADR-0022 §2) — `TELEGRAM_BOT_TOKEN` передаётся **и** в `api`, **и** в `worker` контейнеры. Маскировка в логах гарантируется redact-list'ом structlog (одинаково для обоих контейнеров). **ADR-0027 (round-44, 4 push-бота):** токены push-only ботов по командам (`BOT_IVAN_TOKEN` / `BOT_ALEXANDRA_TOKEN` / `BOT_ANDREI_TOKEN` / `BOT_BUSINESS2_TOKEN`) + их `*_GROUP_ID` + `*_WEBHOOK_SECRET` + `ADMIN_TELEGRAM_IDS` передаются **и в `worker`** (диспатчер `push_notify_dispatch`), **и в `api`** (callback-webhook `/api/telegram/push-webhook/{name}`, round-42 §10). Все push-боты получают `.env` через `env_file: .env` обоих контейнеров (compose не перечисляет их поимённо в `environment:`) — добавление `business2` в `.env` автоматически прокидывается в api и worker, правка `docker-compose.yml` не требуется. Четыре токена + четыре webhook-secret добавлены в structlog redact-list рядом с `TELEGRAM_BOT_TOKEN`.

### External pull-API (ADR-0029)

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `EXTERNAL_API_KEY` | `""` (пусто) | no | **ADR-0029:** статический ключ для `GET /api/external/messages` (доверенный B2B-партнёр забирает все письма pull'ом). Генерация: `openssl rand -hex 32` (256 бит). **Опционально**: пусто ⇒ фича выключена (`external_api_enabled=false`) ⇒ endpoint отдаёт `401` неперечислимо. Передаётся **только** в `api` (worker не использует). Маскируется в structlog redact-list (рядом с `MAIL_ENCRYPTION_KEY`/`TELEGRAM_BOT_TOKEN`); `X-API-Key`/`Authorization` тоже в redact. Ротация — см. `06-security.md` §10. |
| `EXTERNAL_API_RATE_LIMIT_PER_MINUTE` | `120` | no | **ADR-0029:** лимит запросов в минуту на IP к `GET /api/external/messages` (`LIMIT_EXTERNAL_API`). Consume до проверки ключа (anti-flood). Числовой (`int`, `ge=1`; `0` не допускается). Override на consume-time (паттерн `TG_SEND_PER_CHAT_PER_MINUTE`). |

### CI / Build

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `IMAGE_REGISTRY` | (empty → `mail-aggregator`) | no | Префикс образа. На prod-сервере выставить `ghcr.io/<owner>/<repo>` чтобы compose pull тянул published-образы. |
| `IMAGE_TAG` | `local` | no | Тег образа. CI выставляет `${{ github.sha }}`; deploy.yml перезаписывает `IMAGE_TAG` в `.env` на сервере. |

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

## 6. Reverse proxy: nginx + certbot

TLS терминируется на nginx; cert получаем у Let's Encrypt через certbot. Нужны открытые на firewall'е порты `80` (HTTP-01 challenge + 301 redirect) и `443` (HTTPS).

### Файлы

- `deploy/nginx/nginx.conf` — main config (worker процессы, gzip, log_format, общие SSL defaults).
- `deploy/nginx/templates/default.conf.template` — server-блок для `${SERVER_DOMAIN}`. nginx alpine-образ автоматически рендерит шаблоны из `/etc/nginx/templates/` через envsubst при старте контейнера.

### Поведение server-блока

- Listen `80` — HTTP→HTTPS 301 redirect, **кроме** `/.well-known/acme-challenge/*` (раздаётся из webroot для cert challenge) и `/_health_nginx` (healthcheck).
- Listen `443 ssl http2` — proxy_pass на `http://api:8080`. Cert из `mas_certbot_certs` volume (`/etc/letsencrypt/live/${SERVER_DOMAIN}/`).
- Headers вверх: `Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto: https`, `X-Forwarded-Host`, `X-Request-ID`. Backend полагается на эти headers для cookie Secure + audit IP + Message-ID URL-build.
- HSTS: `Strict-Transport-Security: max-age=63072000; includeSubDomains; preload`.
- gzip on для text/css, application/json, application/javascript, application/xml+rss, atom, image/svg+xml. text/html включается gzip-модулем по умолчанию — добавлять его в `gzip_types` нельзя (warning duplicate MIME type).
- `client_max_body_size 30m` (под MAX_ATTACHMENT_BYTES=25 MiB + MIME overhead).
- `proxy_read_timeout 60s` / `proxy_send_timeout 60s` / `proxy_connect_timeout 5s` — выровнено с gunicorn `--timeout=60` в api Dockerfile.

### Первое получение cert (standalone)

В первый запуск nginx падает (нет cert). Bootstrap:

```bash
# поднимаем всё КРОМЕ nginx
docker compose up -d postgres redis minio minio-bootstrap mas-migrations api worker

# certbot standalone — занимает порт 80 на время challenge
docker compose run --rm -p 80:80 certbot certonly --standalone \
  -d "$SERVER_DOMAIN" --email "$ACME_EMAIL" --agree-tos --no-eff-email

# теперь поднимаем nginx + renewal-loop
docker compose --profile prod up -d nginx certbot
```

### Renewal

`certbot` контейнер — бесконечный цикл `certbot renew --webroot -w /var/www/certbot --quiet` каждые 12 часов. При успешном renewal:

1. Cert файлы в `mas_certbot_certs` обновляются.
2. `--deploy-hook` создаёт маркер `/etc/letsencrypt/.reload-needed` (виден в обоих контейнерах через volume).
3. **Nginx нужно перезагрузить вручную** — certbot не имеет docker socket, и signal-механизмы между контейнерами хрупкие. Способ: внешний host-cron, например еженедельно:
   ```cron
   0 4 * * 1 cd /opt/mail-aggregator && docker compose --profile prod exec -T nginx nginx -s reload
   ```
   `nginx -s reload` graceful — без потерь активных соединений. Cert меняется раз в 60 дней, так что недельный cron безопасно покрывает rotation window.

В dev TLS не терминируется на nginx — его просто нет под `--profile prod`. API публикуется на `127.0.0.1:8080` через `docker-compose.override.yml`.

---

## 7. Bootstrapping

### Первый запуск

```bash
git clone <repo>
cd mail-aggregator
cp .env.example .env
# Отредактировать .env: MAIL_ENCRYPTION_KEY, ADMIN_PASSWORD, POSTGRES_PASSWORD,
#   MINIO_ROOT_USER, MINIO_ROOT_PASSWORD, MINIO_APP_ACCESS_KEY, MINIO_APP_SECRET_KEY,
#   SERVER_DOMAIN, ACME_EMAIL, APP_BASE_URL
docker compose up -d --build
docker compose logs -f api worker
```

Порядок при первом старте:
1. `minio` поднимается и проходит healthcheck.
2. `minio-bootstrap` (init-контейнер) создаёт bucket `mail-attachments`, политику `mas-app` и service account из `MINIO_APP_*`. Завершается с exit 0.
3. `mas-migrations` (one-shot init-контейнер) запускается с `command: ["alembic","upgrade","head"]`, накатывает схему и завершается с exit 0. У `api`/`worker` **нет** entrypoint-скрипта, который сам накатывает Alembic, — миграции применяет **только** этот init-контейнер. `api` и `worker` `depends_on` его `service_completed_successfully`, поэтому ни один из них не стартует против схемы-без-миграций (см. `deploy/README.md` секция «Upgrade»). Образ `api` по-прежнему содержит `alembic` как CLI — но это лишь инструмент, который вызывает init-контейнер, а не автоматический startup-hook.
4. `api` ждёт `minio-bootstrap: service_completed_successfully`, `postgres: service_healthy`, `redis: service_healthy`, `mas-migrations: service_completed_successfully`, затем стартует:
   - `seed_super_admin` отрабатывает идемпотентно (upsert пароля, см. модуль `auth` в `05-modules.md`). Выполняется как startup-hook внутри `api` уже после миграций — гарантировано dependency-цепочкой на `mas-migrations`.
   - `Storage.ensure_bucket` — defensive проверка `head_bucket`; bucket уже создан init-контейнером, шаг возвращается мгновенно.
5. `worker` стартует с теми же зависимостями.

UI доступен на `https://${SERVER_DOMAIN}/login`. Полный operator-runbook по подъёму свежего prod-сервера (DNS, firewall, GH secrets, GHCR auth, бэкапы) — `docs/SERVER-SETUP.md`.

### Обновление (deploy)

Автоматический путь — push в `main`, CI собирает и пушит образы в GHCR, `deploy.yml` забирает их на сервер. См. `docs/SERVER-SETUP.md` Часть E.

Порядок деплоя на сервере (тот же, что выполняет `deploy.yml` через SSH — миграции вызываются **явно** перед стартом нового кода):

```bash
cd /opt/mail-aggregator
git checkout <sha>
# правка IMAGE_TAG / IMAGE_REGISTRY в .env под целевой sha
sed -i "s|^IMAGE_TAG=.*|IMAGE_TAG=<sha>|" .env
# 1. подтянуть новые образы api/worker
docker compose --profile prod pull api worker
# 2. ЯВНО накатить миграции one-shot init-контейнером ДО старта нового кода
docker compose --profile prod run --rm mas-migrations
# 3. перезапустить api/worker уже на новой схеме
docker compose --profile prod up -d --remove-orphans api worker
# 4. nginx — пере-создать (подхватить возможные изменения конфига/шаблонов)
docker compose --profile prod up -d --force-recreate nginx
# 5. healthcheck-гейт: дождаться, пока mas-api станет healthy
docker compose ps
```

**Почему миграции вызываются явно `docker compose run --rm mas-migrations` ПЕРЕД `up -d api worker`:** у `api`/`worker` нет entrypoint-скрипта, который накатывает Alembic; миграции применяет только init-контейнер `mas-migrations`. При обычном `up` Compose дождался бы `mas-migrations: service_completed_successfully`, но явный шаг делает порядок детерминированным и проверяемым в логах деплоя — новый код гарантированно не стартует на старой схеме.

**Инвариант миграций — forward-only / online-совместимые** (no table-locking ALTERs; см. migration policy в `deploy/README.md` секция «Upgrade»). Поэтому короткое окно «схема впереди кода» (миграция уже накатилась, а `up -d api worker` ещё не отработал или упал) **безопасно**: старый запущенный код продолжает работать на новой online-совместимой схеме, а downgrade в проде не выполняется — фикс всегда write-forward новой миграцией. Если миграция требует downtime (несовместимое изменение) — devops согласовывает окно отдельно.

### Откат

- Через workflow_dispatch на `Deploy`: запустить вручную, передать sha из known-good коммита.
- Вручную на сервере:
  ```bash
  cd /opt/mail-aggregator
  git checkout <previous-tag-or-sha>
  sed -i "s|^IMAGE_TAG=.*|IMAGE_TAG=<previous-sha>|" .env
  docker compose --profile prod pull api worker
  docker compose --profile prod up -d api worker
  ```
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
- На PR: `docker buildx build target=api|worker` без push (sanity check Dockerfile).
- На push в `main`: тот же build + push в **GHCR** (`ghcr.io/<owner>/<repo>/api:<sha>` и `:latest`, аналогично для `worker`).
- Аутентификация: `docker/login-action@v3` с `username=${{ github.actor }}` и `password=${{ secrets.GITHUB_TOKEN }}` (GH автоматически выдаёт). Опт-ин `permissions: packages: write` только в этой job (не workflow-wide).
- Trivy security-scan — отдельный workflow (`security.yml`).

#### 4. `deploy` (`.github/workflows/deploy.yml`)
- Триггер: push в `main` после CI green ИЛИ ручной `workflow_dispatch` (можно передать sha для отката на known-good).
- Шаги:
  1. `wait-for-ci` job дожидается зелёного `Build images (api)` и `Build images (worker)` на этой sha (через `lewagon/wait-on-check-action`).
  2. `deploy` job: `appleboy/ssh-action` SSH'ится на `$DEPLOY_HOST` под `$DEPLOY_USER`, на сервере выполняет (в этом порядке): `git checkout <sha>` → перезаписывает `IMAGE_TAG`/`IMAGE_REGISTRY` в `.env` → `docker compose --profile prod pull api worker` → **`docker compose --profile prod run --rm mas-migrations`** (явный шаг миграций перед стартом нового кода) → `docker compose --profile prod up -d --remove-orphans api worker` → `docker compose --profile prod up -d --force-recreate nginx` → healthcheck-гейт: ждёт, пока `mas-api` станет healthy (90s). Явный вызов `mas-migrations` гарантирует, что api/worker не стартуют на старой схеме (у них нет entrypoint-миграций — см. секция 7 «Обновление (deploy)» и `deploy/README.md`).
- Required GH Secrets: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_KEY` (full PEM private key), `DEPLOY_PATH` (обычно `/opt/mail-aggregator`).
- На сервере должен быть выполнен `docker login ghcr.io` под пользователем `$DEPLOY_USER` (одноразово, см. `docs/SERVER-SETUP.md` Часть A шаг 9).
- Concurrency lock `deploy-prod` запрещает параллельные deploy'и разных sha.

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

### 11.2 Post-deploy: раскатка обновлённого builtin-каталога тегов

**Касается деплоя, который меняет builtin-каталог тегов** (`backend/app/tags/builtin.py` — состав тегов или их правил, например добавление тега «Реджект» с `match_mode='all'` или новых App Store Connect-правил).

Builtin-теги материализуются **не** при seed, а функцией `TagsService.ensure_builtin_tags(user_id)` — post-login hook в `auth.AuthService` (см. ADR-0017 §6, `03-data-model.md` секция «Заполнение builtin-тегов»). `ensure_builtin_tags` идемпотентна по принципу «есть хотя бы один builtin → return»: для пользователя, у которого builtin-теги уже существуют, она делает return и **не** переписывает правила существующих тегов. Поэтому сам по себе новый код после деплоя не обновит каталог у пользователей, которые уже логинились раньше, — без дополнительного шага у них останутся старые правила.

Для этого существует **штатный forward-only механизм**, а не ручной data-fix. При изменении состава/правил builtin к деплою прикладывается **forward-only rebuild-миграция**, которая сбрасывает существующие builtin-теги, после чего `ensure_builtin_tags` пересоздаёт новый каталог на следующем логине **каждого** пользователя:

- Референсный пример — `migrations/versions/20260521_016_rebuild_builtin_tags.py`: `DELETE FROM tags WHERE is_builtin = true` (FK `ON DELETE CASCADE` от `tag_rules` и `message_tags` снимает связанные правила и применённые метки — это намеренно, старые правила устарели). После DELETE у каждого пользователя `has_any_builtin=false`, и `ensure_builtin_tags` на его следующем логине пересоздаёт актуальный каталог.
- Эта миграция применяется **автоматически** init-контейнером `mas-migrations` в рамках деплоя — тем самым явным шагом `docker compose --profile prod run --rm mas-migrations` (см. секцию 9, dependency-цепочка `api → mas-migrations: service_completed_successfully`).
- Миграции forward-only: для отката никогда не используется `alembic downgrade` — вместо отката пишется новая forward-fix миграция (migration policy — `deploy/README.md`, «Migration policy: forward-only»).

Процедура после деплоя нового builtin-каталога:
1. Убедиться, что деплой прошёл и rebuild-миграция применена (новый `IMAGE_TAG` запущен, `mas-migrations` завершился `service_completed_successfully`, `mas-api` healthy).
2. **super_admin перелогинивается** (logout → login). Это достаточный шаг, чтобы **немедленно** материализовать новый каталог для свежего пользователя или сразу после rebuild-миграции, не дожидаясь естественного релогина: на следующем login hook `ensure_builtin_tags` отработает на актуальной (новой) версии каталога.

> **Важно:** перелогин материализует каталог только тогда, когда у пользователя `has_any_builtin=false` — то есть для свежего пользователя (первый login) либо для существующего пользователя **после** rebuild-миграции, обнулившей его builtin-теги. Если builtin-теги уже есть и rebuild-миграция не применялась, `ensure_builtin_tags` увидит `has_any_builtin=true`, сделает return и не тронет старые правила. Поэтому раскатка нового builtin-каталога на уже существующих пользователей делается rebuild-миграцией (референс — `016`, применяется автоматически `mas-migrations`), а релогин — для свежих пользователей или для немедленной материализации после миграции. Для исторических писем после rebuild пользователь нажимает «Применить к существующим» на нужном теге (новое авто-тегирование на входящую почту работает сразу).

### 11.3 Безопасность сервера

- Только SSH (key-based) и 80/443 (nginx) открыты наружу.
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
- Telegram бот — только launcher (открывает обычную login-страницу), нет push-уведомлений о новых письмах (TD-013, требует линковки telegram_user_id ↔ user_id; см. ADR-0018).

---

## 14. Telegram bot setup (ADR-0018)

Бот создаётся одноразово через BotFather и подключается к prod-серверу webhook'ом. Никаких background-процессов — webhook обслуживает существующий `api` контейнер.

### 14.1 Создание бота в BotFather

1. В Telegram: `@BotFather` → `/newbot` → выбрать имя и username (например, `mail_aggregator_postapp_bot`).
2. Получить `bot_token` (формат `123456789:ABC-DEF...`). Сохранить в password manager и в `.env` сервера как `TELEGRAM_BOT_TOKEN`.
3. (Опционально) `/setdescription`, `/setabouttext`, `/setuserpic` — косметика.
4. **Важно**: настроить домен WebApp в BotFather: `/mybots → <bot> → Bot Settings → Configure Mini App → enable + URL = https://postapp.store`. Без этого Telegram отказывается открывать WebApp.

### 14.2 Генерация webhook secret

```bash
openssl rand -hex 16
```

Положить в `.env`:
```
TELEGRAM_BOT_TOKEN=<bot_token_от_botfather>
TELEGRAM_WEBHOOK_SECRET=<32_hex_chars>
TELEGRAM_WEBAPP_URL=https://postapp.store
TELEGRAM_BOT_ENABLED=true
```

`chmod 600 .env`. Перезапустить `api`:
```bash
docker compose restart api
```

### 14.3 Регистрация webhook у Telegram

Из любого хоста с доступом в Internet (можно с самого сервера):

```bash
source /opt/mail-aggregator/.env
curl -F "url=https://postapp.store/api/telegram/webhook/${TELEGRAM_WEBHOOK_SECRET}" \
     -F "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
     "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook"
```

Ожидаемый ответ:
```json
{"ok":true,"result":true,"description":"Webhook was set"}
```

Проверка:
```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
```

Поля `url` (без secret_token — Telegram его не показывает) и `pending_update_count: 0` — webhook работает.

#### 14.3-push Регистрация push-webhook'ов (ADR-0027 round-42 §10)

Для каждого настроенного push-бота с непустым `BOT_*_WEBHOOK_SECRET` регистрируется собственный webhook (callback-кнопка «Посмотреть сообщение»). Это **тот же** Bot-API метод `setWebhook`, отличаются токен, URL (`/push-webhook/{name}`) и `secret_token`. Идемпотентно — безопасно гонять при каждом деплое.

Предусловие: `BOT_*_TOKEN` и `BOT_*_WEBHOOK_SECRET` присутствуют в `.env` **api**-контейнера (callback) и **worker** (доставка). Контейнеры читают `.env` через `env_file`, кэшируют settings в процессе (`lru_cache get_settings`), поэтому после правки `.env` нужно **force-recreate обоих** контейнеров — `business2`-токены (как и прочие push-боты) читают и `api`, и `worker`:

```bash
# После добавления/изменения BOT_BUSINESS2_* (и любых push-bot env) в .env —
# пересоздать оба контейнера, чтобы новые переменные подхватились.
docker compose --profile prod up -d --force-recreate api worker
```

Затем зарегистрировать push-webhook'и:

```bash
source /opt/mail-aggregator/.env
for pair in "ivan:${BOT_IVAN_TOKEN}:${BOT_IVAN_WEBHOOK_SECRET}" \
            "alexandra:${BOT_ALEXANDRA_TOKEN}:${BOT_ALEXANDRA_WEBHOOK_SECRET}" \
            "andrei:${BOT_ANDREI_TOKEN}:${BOT_ANDREI_WEBHOOK_SECRET}" \
            "business2:${BOT_BUSINESS2_TOKEN}:${BOT_BUSINESS2_WEBHOOK_SECRET}"; do
  name="${pair%%:*}"; rest="${pair#*:}"; token="${rest%%:*}"; secret="${rest##*:}"
  if [ -n "$token" ] && [ -n "$secret" ]; then
    curl -F "url=https://postapp.store/api/telegram/push-webhook/${name}" \
         -F "secret_token=${secret}" \
         "https://api.telegram.org/bot${token}/setWebhook"
  fi
done
```

Проверка каждого (например ivan):
```bash
curl "https://api.telegram.org/bot${BOT_IVAN_TOKEN}/getWebhookInfo"
```
`url` оканчивается на `/push-webhook/ivan`, `pending_update_count: 0` — push-webhook работает.

Если у бота `BOT_*_WEBHOOK_SECRET` **не** задан — `setWebhook` для него пропускается; push-уведомления этого бота шлются **без** кнопки (фича деградирует gracefully, не ломается).

### 14.4 Smoke-test

1. В Telegram открыть бота → `/start` → должна появиться кнопка `Open Mail Aggregator`.
2. Нажать — открывается WebApp с обычной login-страницей.
3. Логин под существующим пользователем (созданным админом) — попадает в inbox.
4. Логи `api` должны содержать `event=telegram_webhook_received` (level INFO) и НЕ должны содержать значение `TELEGRAM_BOT_TOKEN` ни на одном уровне.
5. **round-42 (push-callback):** в чате push-бота команды прийти уведомление с кнопкой «Посмотреть сообщение»; нажатие админом (chat id ∈ `ADMIN_TELEGRAM_IDS`) → в чат приходит **тело письма**. Логи `api`: `event=push_callback_message_sent`, без значений `BOT_*_TOKEN`/`BOT_*_WEBHOOK_SECRET`. Нажатие НЕ-админом → toast «Нет доступа», тело не показывается.

### 14.5 Ротация secret / token

- **Webhook secret**: сгенерировать новый, обновить `.env`, перезапустить `api`, повторить `setWebhook` (см. 14.3) с новым значением. Старый сразу перестаёт работать.
- **Bot token**: в BotFather `/revoke` → новый token, повторить шаги 14.1–14.3. Старый token немедленно невалиден.
- **round-42 — push-webhook secret / push-bot token**: сгенерировать новый `BOT_*_WEBHOOK_SECRET` (`openssl rand -hex 16`) или ротировать `BOT_*_TOKEN` (BotFather `/revoke`), обновить `.env` (**api + worker**), перезапустить `api` (+ `worker` при смене токена), повторить push-`setWebhook` (см. 14.3-push) для этого бота. Боты независимы — ротация одного не влияет на другие.

### 14.6 Отключение бота

```
TELEGRAM_BOT_ENABLED=false
```

Перезапустить `api`. Webhook продолжит принимать запросы (валидируя secret), но не будет вызывать Bot API. Чтобы Telegram перестал слать апдейты:

```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/deleteWebhook"
```

---
