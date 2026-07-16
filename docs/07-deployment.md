# 07. Deployment

> **⚠️ ДЕМОНТАЖ ВЫПОЛНЕН (2026-07-15) — этот документ описывает ДО-демонтажный агрегатор.** По [ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md)/[ADR-0044](./adr/ADR-0044-decommission-runbook.md) агрегатор сведён к mail-connector'у. **MinIO/S3 СНЯТЫ полностью** (Фаза G, `8e890a2`/`e0bccc3`): сервисы `minio`/`minio-bootstrap`, volume `mas_minio_data`, bucket `mail-attachments`, S3-env, `deploy/minio-bootstrap.sh` — **удалены**. Все секции ниже про MinIO/S3 (сервис `minio`, `mas_minio_data`, «MinIO bootstrap» sec. 12, S3-env-переменные, MinIO-бэкап/restore) — **ИСТОРИЧЕСКИЕ, не отражают текущий compose/прод**. Также сняты Telegram/webhooks/forwarding/tags/groups и весь Jinja-UI/static (HTML-URL → 404; живы `/healthz`/`/readyz`/`/api/external/*`). Актуальный прод: 4 таблицы (`alembic_version`/`mail_accounts`/`messages`/`users`), ревизия `20260715_028`, без MinIO. Посекционная вычистка этого документа ведётся под **`TD-050`(в)** — до её завершения читать секции про снятые подсистемы как исторические.

Целевая среда — single-host Linux (Ubuntu 22.04 LTS или новее) с Docker Engine 24+ и docker compose v2. Все компоненты упакованы в docker-compose.

**Актуальный prod (с 2026-07-01):** выделенный сервер Hetzner `49.12.189.77`, Ubuntu 26.04, домен `postapp.store` (A-запись у reg.ru). Деплой по SSH под `root`, checkout в `/opt/mail-agregator`. До 2026-07-01 прод жил на общем сервере `132.243.113.117`; процедура переезда — секция 15 «Migration to a new server».

> **TLS-модель (важно — читать перед правкой §6).** Сертификаты Let's Encrypt обслуживаются **на хосте**: пакетный `certbot` пишет в host-каталог `/etc/letsencrypt`, а обновление гоняет системный `certbot.timer` (webroot `/var/www/certbot`). В docker-compose **нет** сервиса `certbot` и **нет** volume `mas_certbot_certs`/`mas_certbot_webroot` — контейнер `nginx` (единственный prod-only сервис под `--profile prod`) читает `/etc/letsencrypt` и `/var/www/certbot` через **bind-mount с хоста** (см. `docker-compose.yml`, сервис `nginx`). Это осознанное решение, зафиксировано в ADR-0032.

---

## 1. Сервисы docker-compose

```mermaid
flowchart LR
    subgraph host[Host]
        cb[certbot pkg + certbot.timer<br/>host-level renewal]
        le[(/etc/letsencrypt<br/>/var/www/certbot)]
        subgraph net[Docker network: mas-net]
            api[api<br/>:8080 internal]
            worker[worker<br/>no ports]
            pg[postgres<br/>:5432 internal]
            rd[redis<br/>:6379 internal]
            mn[minio<br/>:9000 internal<br/>:9001 console]
            ng[nginx<br/>:80, :443]
        end
    end

    ng --> api
    api --> pg & rd & mn
    worker --> pg & rd & mn
    cb --> le
    le -. bind-mount RO .- ng
```

| Сервис | Image | Restart | Ports (host:container) | Зависит от (depends_on healthy) |
| --- | --- | --- | --- | --- |
| `nginx` | `nginx:1.27-alpine` | unless-stopped | `80:80, 443:443` | api |
| `api` | local build / GHCR (`deploy/Dockerfile` target `api`) | unless-stopped | (только internal) | postgres, redis, minio, minio-bootstrap, mas-migrations |
| `worker` | local build / GHCR (`deploy/Dockerfile` target `worker`) | unless-stopped | (нет) | postgres, redis, minio, minio-bootstrap, mas-migrations |
| `postgres` | `postgres:16-alpine` | unless-stopped | (только internal) | — |
| `redis` | `redis:7.2-alpine` | unless-stopped | (только internal) | — |
| `minio` | `minio/minio:RELEASE.2024-08-29T01-40-52Z` | unless-stopped | `127.0.0.1:9001:9001` (console, dev; в prod закрыт за firewall/VPN) | — |

Все internal-порты в общей docker network `mas-net` (default bridge). Никаких host-port mapping для api/postgres/redis/minio:9000.

**Нет** контейнера `certbot` — TLS-renewal выполняет host-level `certbot.timer` (см. секцию 6). `nginx` — **единственный** сервис под `--profile prod`; в dev его нет, api публикуется на `127.0.0.1:8080` через `docker-compose.override.yml`. TLS-материал (`/etc/letsencrypt`) и ACME-webroot (`/var/www/certbot`) прокидываются в `nginx` **bind-mount'ом с хоста** (RO), а не через named volumes.

---

## 2. Volumes

| Volume | Сервис | Содержимое | Backup-критичность |
| --- | --- | --- | --- |
| `mas_pg_data` | postgres | `/var/lib/postgresql/data` | **Critical** — основные данные |
| `mas_minio_data` | minio | `/data` | **Critical** — вложения |
| `mas_redis_data` | redis | `/data` (если включить AOF) | Low — можно потерять (sessions релогинятся) |

Все три — named volumes Docker (см. `docker-compose.yml`, секция `volumes:`).

**Host bind-mounts (TLS, не docker volumes).** Сертификаты и ACME-challenge живут на **хосте** и монтируются в `nginx` read-only — named volume для них **нет**:

| Host-путь | Монтируется в | Владелец на хосте | Backup-критичность |
| --- | --- | --- | --- |
| `/etc/letsencrypt` | `nginx:/etc/letsencrypt:ro` | host `certbot` (RW), nginx (RO) | Medium — account keys + `live/<domain>/{fullchain,privkey,chain}.pem`. Рестор Let's Encrypt автоматический, но избегаем rate-limit (5 fails/час, 50 certs/нед на домен). При переезде каталог копируется tar'ом (секция 15), чтобы не переполучать cert. |
| `/var/www/certbot` | `nginx:/var/www/certbot:ro` | host `certbot` (RW), nginx (RO) | Low — HTTP-01 challenge files, пересоздаётся при каждом renewal. |

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
Контейнера `certbot` **нет** — renewal выполняется host-level `certbot.timer` (systemd), поэтому docker-healthcheck неприменим. Статус: `systemctl status certbot.timer` / `systemctl list-timers certbot.timer`; логи renewal: `journalctl -u certbot` (или `/var/log/letsencrypt/letsencrypt.log`). См. секцию 6.

---

## 4. Environment variables

Полный список полей `Settings` (`shared/config.py`) — **этот раздел, а не `.env.example`, является нормативной сводкой env**.

> **Соглашение о составе `.env.example` (зафиксировано 2026-07-16).** Шаблон `.env.example` — не зеркало `Settings`, а **чек-лист провижининга оператора**: в него входят ключи, которые оператор обязан или типично хочет задать (секреты, специфичные для инсталляции URL/домены, часто крутимые пороги). **Внутренние тюнеры с рабочим дефолтом и валидированным диапазоном в шаблон НЕ выносятся** — их место в таблицах этого раздела. Расхождение состава шаблона и `Settings` **само по себе НЕ является дефектом** и не заводится как долг: `Settings` читает окружение с `extra="ignore"`, отсутствующий в `.env` ключ берёт дефолт. Дефект — это (а) ключ, **обязательный** на старте (сегодня ровно один: `MAIL_ENCRYPTION_KEY`, `_enforce_required`) или нужный для связи с CRM, но отсутствующий в шаблоне; либо (б) ключ в шаблоне/CI, которого **уже нет** в `Settings` (рассинхрон env↔код — вот это заводится, см. `TD-060`). Прежняя формулировка требовала от `.env.example` «все переменные» и порождала ложные находки при сплошном свипе (напр. `MAX_BODY_BYTES`, 2026-07-16). **Сплошной свип 2026-07-16 (пересчитан после снятия `MAX_ATTACHMENT_BYTES`, `TD-060` группа C — было 45):** в `Settings` **44 поля**; в шаблоне отсутствуют 8 — `SERVICE_NAME` (внутреннее, воркер переопределяет в коде) и 7 тюнеров с дефолтами (`MAX_BODY_BYTES`, `SYNC_MAX_CONSECUTIVE_FAILURES`, `SYNC_MASS_FAILURE_RATIO`, `SYNC_MASS_FAILURE_MIN`, `SYNC_CONNECT_RETRIES`, `SYNC_TRANSIENT_SUPPRESS_MINUTES`, `SYNC_OAUTH_LOGIN_FAILED_TRANSIENT`); все 8 — штатное поведение по соглашению выше, правок `.env.example` **не требуется**.

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

### ~~Admin seed~~ — СНЯТО

> **ИСТОРИЧЕСКОЕ. Полей `ADMIN_LOGIN`/`ADMIN_PASSWORD` в `Settings` больше нет** (`TD-060` (6d), рабочее дерево 2026-07-16). Интерактивный супер-админ ушёл вместе с cookie-UI ([ADR-0044](./adr/ADR-0044-decommission-runbook.md) A3/§5): `seed_super_admin` снят, единственный сидируемый аккаунт — технический `crm-service` с `password_hash=NULL` (`backend/app/auth/service.py:33-70`), интерактивного входа в коннекторе не существует. `ADMIN_PASSWORD` **больше не требуется на старте**: `_enforce_required` (`shared/config.py:271-277`) требует единственный ключ — `MAIL_ENCRYPTION_KEY`. Ключи удалены из `.env.example`/CI; легаси-значение в прод-`.env` безвредно (`extra="ignore"`). Провижинить и ротировать нечего — см. `docs/SERVER-SETUP.md` §A.8 / §H.2.

### Worker / sync

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `MAX_CONCURRENT_IMAP` | `10` | no | Размер semaphore (см. ADR-0013). |
| `WORKER_THREAD_POOL_SIZE` | `14` | no | Размер default ThreadPoolExecutor (= `MAX_CONCURRENT_IMAP + 4`). |
| `SYNC_INTERVAL_MINUTES` | `5` | no | Интервал sync_cycle. Не рекомендуется снижать ниже 3. |
| `RETENTION_DAYS` | `30` | no | TTL писем (см. ADR-0011). |
| `IMAP_TIMEOUT_SECONDS` | `60` | no | Per-account timeout воркера при синхронизации (см. ADR-0013). **Не** относится к connection-test — там `MAILBOX_TEST_DEADLINE_SECONDS`. |
| `MAILBOX_TEST_DEADLINE_SECONDS` | `45` | no | **ADR-0047:** hard-deadline на **весь** connection-test ящика (host-assert + IMAP-логин + SMTP-логин), общий для `POST /mailboxes/test`, `POST /mailboxes` и `PATCH /mailboxes/{id}` при смене кредов. Это **единственная** верхняя граница **probe**. Исчерпание → доменная `422` (`imap_login_failed`/`smtp_login_failed`, `details.detail="timeout"`), НЕ `504` и НЕ зависание. Границы `ge=10, le=45` (`shared/config.py:169` — норма **применена в коде**): `le` **машинно** охраняет инвариант `deadline + teardown (≤ 5 с, ADR-0047 §2.3) + внепробная часть запроса (≤ 5 с) < proxy_read_timeout (60s)`; значение > 45 **не проходит** валидацию на старте. (Отменённые `le=50`/`le=55` были over-claim: `50 + 5 + 5 = 60` = ровно `proxy_read_timeout` → `504`.) **При изменении `proxy_read_timeout` пересчитать `le`** (формула — §6 / ADR-0047 §5). |
| `INITIAL_SYNC_DAYS` | `30` | no | Окно при первом подключении. |
| ~~`MAX_ATTACHMENT_BYTES`~~ | — | — | **СНЯТО — поля в `Settings` больше нет** (`TD-060`, группа B/C, рабочее дерево 2026-07-16, НЕ задеплоено). Вложения демонтированы вместе с MinIO ([ADR-0044](./adr/ADR-0044-decommission-runbook.md) Фаза G): поле оставалось in-memory size-cap'ом мёртвого fetch-пути воркера, который снят целиком (`worker/app/imap_fetcher.py:227-230` — «Attachments are NOT extracted (ADR-0043 §4)»; grep `MAX_ATTACHMENT_BYTES|max_att_bytes` по `shared/`/`backend/`/`worker/` = **0**). Ключ удалён из `.env.example`; легаси-значение в прод-`.env` безвредно (`extra="ignore"`). **Не путать с `client_max_body_size`** — тот больше на этот ключ не опирается, см. §6. |
| `MAX_BODY_BYTES` | `1048576` | no | 1 MiB — верхняя граница сохраняемого тела письма (читается воркером: `worker/app/sync_cycle.py:593`). Границы `ge=1024, le=10485760`. Внутренний тюнер: в `.env.example` намеренно отсутствует (см. соглашение в начале раздела). |
| `SYNC_MAX_CONSECUTIVE_FAILURES` | `3` | no | ADR-0026: порог PERMANENT-ошибок подряд → auto-disable (`ge=1, le=20`). Заменяет хардкод `_DISABLE_AFTER_FAILS`. |
| `SYNC_MASS_FAILURE_RATIO` | `0.5` | no | ADR-0026: доля PERMANENT-падений за цикл, при которой circuit-breaker подавляет массовый disable (`ge=0.0, le=1.0`). |
| `SYNC_MASS_FAILURE_MIN` | `5` | no | ADR-0026: минимум аккаунтов в цикле для активации circuit-breaker (`ge=1, le=10000`). |
| `SYNC_CONNECT_RETRIES` | `3` | no | ADR-0026 (update): повторы открытия IMAP-соединения/login на DNS/connection-ошибках И спорадических transient IMAP-ошибках, backoff 0.5s/1.0s/2.0s (`ge=0, le=10`). Также число retry OAuth-`login failed` (ADR-0028 §3). |
| `SYNC_TRANSIENT_SUPPRESS_MINUTES` | `60` | no | ADR-0026 (update): подавлять запись TRANSIENT `last_sync_error` в UI, если последний успешный sync был в пределах окна (`ge=0, le=10080`; `0` отключает). Распространяется на OAuth-`login failed` (ADR-0028 §6). |
| `SYNC_OAUTH_LOGIN_FAILED_TRANSIENT` | `true` | no | ADR-0028 §7: **обязательный** kill-switch (default-on; `no` = переопределять не требуется, дефолт активирует фикс). При `true` (дефолт) IMAP-`login failed`/`authenticationfailed` у `oauth_outlook` = transient (retry + no-disable). `false` возвращает старое permanent-поведение (откат без редеплоя кода). |

### ~~Sessions / auth~~ — СНЯТО

> **ИСТОРИЧЕСКОЕ. Пользовательских сессий и cookie-полей в `Settings` больше нет.** `SESSION_TTL_SECONDS` / `SESSION_ABSOLUTE_TTL_SECONDS` / `SETUP_SESSION_TTL_SECONDS` / `COOKIE_DOMAIN` (а также `LOGIN_FAILURE_THRESHOLD` / `LOGIN_LOCKOUT_MINUTES` / `SAFE_REDIRECT_AFTER_LOGIN` / `LOGIN_PATH`) сняты вместе с cookie-UI ([ADR-0044](./adr/ADR-0044-decommission-runbook.md) A3; гигиена — `TD-060` (1)/(6c), рабочее дерево 2026-07-16), ключи удалены из `.env.example`/CI. Аутентификация коннектора — **только машинная** (`EXTERNAL_API_KEY` через `X-API-Key`/`Bearer`); сессий, CSRF и form-fallback не существует. Единственный живой `SESSION_*`-ключ — `SESSION_GUARD_STRICT` ниже, и он **не про пользовательские сессии**.

### Session guards: рантайм-детектор потерянных статус-событий (ADR-0046 §2.1.1 / `TD-054`)

Речь о **DB-сессии** (SQLAlchemy `AsyncSession`), а **не** о пользовательской сессии — совпадение префикса `SESSION_` случайно (тем более что пользовательские сессии сняты, см. блок выше).

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `SESSION_GUARD_STRICT` | (не задана) ⇒ strict **только** под pytest | no | **ADR-0046 §2.1.1 / `TD-054`:** режим детектора незакрытых post-COMMIT side-effect'ов (забытый `flush_crm_status_events()`). Читается **напрямую из окружения**, минуя pydantic-`Settings` (`shared/session_guards.py:47`, `strict_mode()` `:95-100`) → валидации типа нет. **Не задана** (прод) ⇒ strict включается, только если в окружении есть `PYTEST_CURRENT_TEST` (т.е. под pytest); на проде такой переменной нет ⇒ **strict выключен**. **Задана** ⇒ truthy-набор `1` / `true` / `yes` / `on` (регистронезависимо, с trim, `_TRUTHY` `:49`) ⇒ strict **ON**; **любое** другое значение, включая пустую строку, ⇒ strict **OFF** — в том числе под pytest (осознанный аварийный выключатель). |

**Что делает детектор.** На закрытии **любой** DB-сессии (`shared/db.py:110` — FastAPI-зависимость `get_session`; `:127` — `make_session` воркера/CLI: других способов получить сессию в кодовой базе нет) проверяются guard'ы, зарегистрированные компонентами с отложенными post-COMMIT эффектами (сегодня один — `MailAccountService`, `backend/app/accounts/service.py:149-157`, probe = непустой `_pending_status_account_ids`).

| Режим | Поведение при непустой очереди |
| --- | --- |
| strict **OFF** (прод, дефолт) | structlog-`warning` `crm_status_pending_dropped` с `owner=MailAccountService` и `mail_account_ids=[…]` (`session_guards.py:119`; имя события — константа `CRM_STATUS_PENDING_DROPPED_EVENT`, `accounts/service.py:61`). Запрос/цикл **НЕ** падает: уже закоммиченный `PATCH` возвращает свой `200`. |
| strict **ON** (pytest / явный truthy) | тот же `warning`, затем жёсткий `AssertionError` (`session_guards.py:122-126`) — забытый flush падает на CI, а не молча теряется. |

Детектор **никогда не отправляет** событие сам (авто-flush отклонён в ADR-0046 §Alternatives: teardown не знает, был ли COMMIT). Он только делает молчаливую потерю шумной.

**Прод:** переменную задавать **не нужно и не рекомендуется** — `SESSION_GUARD_STRICT=true` на проде превратил бы забытый flush в `500` на **уже закоммиченном** запросе (данные записаны, клиент получил ошибку). Аварийное применение: временно выставить в `true` на staging/dev, чтобы поймать вызывающего без flush. Приходит в `api` и `worker` через `env_file: .env` (в `environment:` compose поимённо не перечисляется — правка `docker-compose.yml` не требуется).

### Reverse proxy + TLS (nginx-контейнер + host certbot)

`SERVER_DOMAIN` — единственная env-переменная, которую читает контейнер `nginx`. Host-level `certbot` (пакет + `certbot.timer`) конфигурируется на хосте (домен и e-mail задаются при первом выпуске cert, секция 6), а **не** через `.env` docker-стека.

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `SERVER_DOMAIN` | (none) | yes (prod) | FQDN для TLS (прод: `postapp.store`). nginx envsubst-ит в `default.conf`. Должен совпадать с доменом host-cert в `/etc/letsencrypt/live/<domain>/`. |
| `ACME_EMAIL` | `admin@example.com` | no (host-level) | Контакт Let's Encrypt (уведомления об истечении). Используется **host-командой** `certbot certonly` при первом выпуске (секция 6), контейнеры его не читают. Может присутствовать в `.env` как справочная запись. |

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
| `MAILBOX_DOWN_ALERT_ENABLED` | `true` | no | **ADR-0033:** kill-switch Telegram-алерта об авто-отключении почты. `false` → worker не enqueue'ит и не регистрирует job `mailbox_alert_dispatch`. Доставка — **основным** ботом (`TELEGRAM_BOT_TOKEN`), новых секретов нет. |
| `MAILBOX_ALERT_DISPATCH_INTERVAL_SECONDS` | `5` | no | **ADR-0033:** интервал APScheduler для `mailbox_alert_dispatch` (§14.3). |
| `MAILBOX_ALERT_BATCH_SIZE` | `30` | no | **ADR-0033:** `LPOP count` из `mailbox_alert_queue` за тик. |
| `FORWARDING_ENABLED` | `true` | no | **ADR-0034:** kill-switch переадресации писем команды лидеру. `false` → worker не enqueue'ит `forward_ids` и не регистрирует job `forward_dispatch`. Отправка — SMTP-кредами самих ящиков, новых секретов нет. |
| `FORWARD_DISPATCH_INTERVAL_SECONDS` | `5` | no | **ADR-0034:** интервал APScheduler для `forward_dispatch` (§14.4). |
| `FORWARD_BATCH_SIZE` | `30` | no | **ADR-0034:** `LPOP count` из `forward_dispatch_queue` за тик. |
| `FORWARD_MAX_TOTAL_BYTES` | `26214400` | no | **ADR-0034:** суммарный лимит вложений в одном форварде (~25 МБ); превышающие пропускаются с пометкой в теле. |
| `FORWARD_PER_ACCOUNT_PER_MINUTE` | `30` | no | **ADR-0034:** per-account throttle пересылок (Redis token-bucket, fail-open + лог при превышении). |
| `FORWARD_SMTP_HOST` | `""` | no | **ADR-0034 §5.1:** host служебного SMTP-релея для отправки форвардов. Пусто → релей выключен (отправка кредами самого ящика, прежнее поведение). Задан вместе с `FORWARD_SMTP_FROM` + `FORWARD_SMTP_USERNAME` → `forward_relay_enabled=true`, форварды уходят через релей. Передаётся **только** в `worker`. |
| `FORWARD_SMTP_PORT` | `587` | no | **ADR-0034 §5.1:** порт релея (`1..65535`). 587 = submission/STARTTLS. |
| `FORWARD_SMTP_USERNAME` | `""` | no | **ADR-0034 §5.1:** логин SMTP-релея. Часть условия `forward_relay_enabled`. |
| `FORWARD_SMTP_PASSWORD` | `""` | no | **ADR-0034 §5.1:** пароль SMTP-релея (**секрет**). `chmod 600 .env`; маскируется в structlog redact-list рядом с mail-паролями. Единственный новый секрет канала переадресации. |
| `FORWARD_SMTP_FROM` | `""` | no | **ADR-0034 §5.1:** адрес в `From` форварда в ветке релея (единый служебный отправитель). Часть условия `forward_relay_enabled`. Оригинальный отправитель уходит в `Reply-To`. |
| `FORWARD_SMTP_STARTTLS` | `true` | no | **ADR-0034 §5.1:** STARTTLS при подключении к релею (submission/587). |
| `FORWARD_SMTP_SSL` | `false` | no | **ADR-0034 §5.1:** implicit-TLS (SMTPS/465) при подключении к релею. Взаимоисключающе с STARTTLS (как у ящиков). |
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
| `OUTLOOK_REDIRECT_URI` | (none) | yes (если OAuth enabled) | **ADR-0045 §4** (амендмент к ADR-0025): `{APP_BASE_URL}/api/external/mailboxes/oauth/callback` — точное совпадение с зарегистрированным в Azure. **Session-callback `/api/oauth/outlook/callback` из ADR-0025 СНЯТ** вместе с cookie-UI (ADR-0044 A3); его заменил headless external-callback (`backend/app/external/router.py:550`). Значение обновлено на cut-over — код только читает env (`shared/config.py`). |
| `OUTLOOK_TENANT` | `common` | no | **ADR-0025:** tenant для authorize/token endpoints `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/...`. Для личных ящиков — `common` (ранее `consumers`; `consumers` давал IMAP XOAUTH2 "User is authenticated but not connected"). `common` пускает и личные, и рабочие аккаунты. |
| `OUTLOOK_OAUTH_STATE_TTL_SECONDS` | `600` | no | **ADR-0025:** TTL Redis-ключа `oauth_state:{state}` (CSRF/anti-fixation state + PKCE verifier). |

OAuth включён (`outlook_oauth_enabled`, derived), когда заданы `OUTLOOK_CLIENT_ID` + `OUTLOOK_CLIENT_SECRET`; иначе `/api/external/mailboxes/oauth/*` отдают `404` (route скрыт). `OUTLOOK_CLIENT_SECRET` передаётся в **api** (authorize/callback/SMTP) и **worker** (refresh перед IMAP).

> **Всё дальше в этом абзаце — ИСТОРИЧЕСКОЕ (Telegram снят демонтажём, ADR-0044 A3).** Полей `TELEGRAM_*`/`BOT_*`/`ADMIN_TELEGRAM_IDS` в `Settings` нет, роутов `/api/telegram/*` нет, диспатчера `push_notify_dispatch` нет; уведомления — целиком на стороне CRM. Оставлено как история конфигурации до демонтажа; вычистка — `TD-050`(в).

`TELEGRAM_BOT_TOKEN` хранился в `.env` (`chmod 600`). **Изменение от ADR-0018:** `worker` теперь использует Telegram API для доставки push-нотификаций (ADR-0022 §2) — `TELEGRAM_BOT_TOKEN` передаётся **и** в `api`, **и** в `worker` контейнеры. Маскировка в логах гарантируется redact-list'ом structlog (одинаково для обоих контейнеров). **ADR-0027 (round-44, 4 push-бота):** токены push-only ботов по командам (`BOT_IVAN_TOKEN` / `BOT_ALEXANDRA_TOKEN` / `BOT_ANDREI_TOKEN` / `BOT_BUSINESS2_TOKEN`) + их `*_GROUP_ID` + `*_WEBHOOK_SECRET` + `ADMIN_TELEGRAM_IDS` передаются **и в `worker`** (диспатчер `push_notify_dispatch`), **и в `api`** (callback-webhook `/api/telegram/push-webhook/{name}`, round-42 §10). Все push-боты получают `.env` через `env_file: .env` обоих контейнеров (compose не перечисляет их поимённо в `environment:`) — добавление `business2` в `.env` автоматически прокидывается в api и worker, правка `docker-compose.yml` не требуется. Четыре токена + четыре webhook-secret добавлены в structlog redact-list рядом с `TELEGRAM_BOT_TOKEN`.

### External pull-API (ADR-0029)

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `EXTERNAL_API_KEY` | `""` (пусто) | no | **ADR-0029:** статический ключ для `GET /api/external/messages` (доверенный B2B-партнёр забирает все письма pull'ом). Генерация: `openssl rand -hex 32` (256 бит). **Опционально**: пусто ⇒ фича выключена (`external_api_enabled=false`) ⇒ endpoint отдаёт `401` неперечислимо. Передаётся **только** в `api` (worker не использует). Маскируется в structlog redact-list (рядом с `MAIL_ENCRYPTION_KEY`/`TELEGRAM_BOT_TOKEN`); `X-API-Key`/`Authorization` тоже в redact. Ротация — см. `06-security.md` §10. |
| `EXTERNAL_API_RATE_LIMIT_PER_MINUTE` | `120` | no | **ADR-0029:** лимит запросов в минуту на IP к `GET /api/external/messages` (`LIMIT_EXTERNAL_API`). Consume до проверки ключа (anti-flood). Числовой (`int`, `ge=1`; `0` не допускается). Override на consume-time (паттерн `TG_SEND_PER_CHAT_PER_MINUTE`). |

### External reply-API (ADR-0035)

Отдельный write-гейт для `POST /api/external/messages/{id}/reply` (ответ на существующее письмо тем же `EXTERNAL_API_KEY`). Read-контракт ADR-0029 (ключ + лимит выше) остаётся обязательным условием — reply не работает без непустого `EXTERNAL_API_KEY`; `EXTERNAL_REPLY_ENABLED` — **дополнительный** opt-in поверх него.

| Переменная | Default | Required | Описание |
| --- | --- | --- | --- |
| `EXTERNAL_REPLY_ENABLED` | `false` | no | **ADR-0035 §1:** отдельный гейт записи для `POST /api/external/messages/{id}/reply`. `false` (default) ⇒ reply отдаёт `403 forbidden` даже при валидном ключе — существующие read-only деплои ADR-0029 не получают write-способность молча при апгрейде кода. Запись — явный opt-in оператора. **На prod `postapp.store` reply активирован: `EXTERNAL_REPLY_ENABLED=true`** (в серверном `.env`, см. ниже). Передаётся **только** в `api` (worker не использует). Bool (`true`/`false`). |
| `EXTERNAL_REPLY_RATE_LIMIT_PER_MINUTE` | `30` | no | **ADR-0035 §4:** отдельный, более жёсткий лимит запросов в минуту на IP к reply-endpoint (`LIMIT_EXTERNAL_REPLY`), независимый от read-лимита (каждый вызов = один реальный SMTP-send). Consume **первым**, до проверки ключа. Числовой (`int`, `ge=1`; `0` не допускается). Override на consume-time (паттерн `EXTERNAL_API_RATE_LIMIT_PER_MINUTE`). Default `30` < read `120`. |

Обе переменные приходят в `api` через `env_file: .env` (compose **не** перечисляет их поимённо в `environment:` — тот же механизм, что `EXTERNAL_API_KEY` и push-боты). Правка `docker-compose.yml` не требуется: достаточно строк в серверном `.env`. **Активация на `postapp.store`:** в `/opt/mail-agregator/.env` задать `EXTERNAL_REPLY_ENABLED=true` (и, при необходимости тюнинга, `EXTERNAL_REPLY_RATE_LIMIT_PER_MINUTE`), затем `docker compose --profile prod up -d --force-recreate api` (deploy.yml трогает только `IMAGE_TAG`/`IMAGE_REGISTRY`, остальные строки `.env` не переписывает — значение сохраняется между деплоями). `EXTERNAL_REPLY_ENABLED`/`EXTERNAL_REPLY_RATE_LIMIT_PER_MINUTE` — не секреты (флаг + число), в redact-list не добавляются; секрет остаётся только `EXTERNAL_API_KEY`.

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

## 6. Reverse proxy: nginx (контейнер) + certbot (host-level)

TLS терминируется на контейнере `nginx`. Сертификаты Let's Encrypt выпускает и обновляет **host-level** `certbot` (системный пакет + `certbot.timer`), пишущий в host-каталог `/etc/letsencrypt`; nginx читает его **bind-mount'ом с хоста** (RO). В docker-compose нет ни сервиса `certbot`, ни named-volume для сертификатов. Нужны открытые на firewall'е порты `80` (HTTP-01 challenge + 301 redirect) и `443` (HTTPS).

### Файлы

- `deploy/nginx/nginx.conf` — main config (worker процессы, gzip, log_format, общие SSL defaults).
- `deploy/nginx/templates/default.conf.template` — server-блок для `${SERVER_DOMAIN}`. nginx alpine-образ автоматически рендерит шаблоны из `/etc/nginx/templates/` через envsubst при старте контейнера.
- Host: `/etc/letsencrypt/live/${SERVER_DOMAIN}/{fullchain,privkey}.pem` — cert, bind-mount `-> /etc/letsencrypt:ro` в nginx.
- Host: `/var/www/certbot` — webroot для HTTP-01, bind-mount `-> /var/www/certbot:ro` в nginx.

### Поведение server-блока

- Listen `80` — HTTP→HTTPS 301 redirect, **кроме** `/.well-known/acme-challenge/*` (раздаётся из host-webroot `/var/www/certbot` для cert challenge) и `/_health_nginx` (healthcheck).
- Listen `443 ssl http2` — proxy_pass на `http://api:8080`. Cert берётся из bind-mount host-каталога `/etc/letsencrypt/live/${SERVER_DOMAIN}/`.
- Headers вверх: `Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto: https`, `X-Forwarded-Host`, `X-Request-ID`. Backend полагается на эти headers для cookie Secure + audit IP + Message-ID URL-build.
- HSTS: `Strict-Transport-Security: max-age=63072000; includeSubDomains; preload`.
- gzip on для text/css, application/json, application/javascript, application/xml+rss, atom, image/svg+xml. text/html включается gzip-модулем по умолчанию — добавлять его в `gzip_types` нельзя (warning duplicate MIME type).
- **`client_max_body_size 8m`** — норма зафиксирована 2026-07-16 (прежнее `30m` обосновывалось снятым `MAX_ATTACHMENT_BYTES`, см. врезку ниже). **Код приведён к норме** (2026-07-16): `deploy/nginx/nginx.conf:80` держит `client_max_body_size 8m`, обоснование в комментарии `:56-79` переписано по `ExternalSendRequest.body_text` без ссылки на снятое поле; `nginx -t` зелёный. **Не задеплоено** — на проде старый образ с `30m` до коммита+деплоя остатка `TD-060`.

> **Обоснование `client_max_body_size 8m` (2026-07-16) — по фактическим схемам живых эндпоинтов, а не по снятому полю.**
> За этим nginx живут ровно два роутера — `external_router` и `health_router` (`backend/app/main.py:99-100`); **вложений коннектор не принимает вовсе** (grep `attachment` по `backend/app/external/schemas.py` и `backend/app/send/mime.py` = **0**). Самое крупное принимаемое тело — `ExternalSendRequest` (`POST /api/external/mailboxes/{account_id}/send`, `backend/app/external/router.py:395`): `body_text: str = Field(..., max_length=1_048_576)` (`backend/app/external/schemas.py:231`), т.е. **1 MiB символов**. Pydantic считает `max_length` в **символах**, а не байтах, поэтому потолок запроса в байтах:
> - UTF-8 worst case — 4 байта/код-поинт → 4 MiB;
> - JSON-escape (`ensure_ascii`) — **6 байт/код-поинт для BMP** (`\uXXXX`, напр. кириллица `"я"`) → **6 MiB**; **12 байт/код-поинт для astral** (U+10000+, эмодзи: `json.dumps` даёт **surrogate-пару** `"😀"` — два `\uXXXX` на ОДИН код-поинт) → **12 MiB**. Существенно, что Pydantic `max_length` считает **код-поинты** (`len()`), а `len('😀') == 1`: тело из 1 048 576 эмодзи `max_length` **проходит** и даёт 12 MiB > `8m` → `413`. Лимит от этого не «маловат» — см. вывод ниже;
> - остальные поля (пересчитано 2026-07-16 — прежняя оценка «≈ < 100 KiB» была **занижена вчетверо**, см. ниже), множитель 6 B/код-поинт (BMP; для astral — 12, но у `refs`/`subject`/`in_reply_to` это ASCII-заголовки RFC 5322):
>   - `refs` — `max_length=_MAX_REFS_LEN` = **65 536** символов (`schemas.py:233`, константа `:167`) ⇒ **384 KiB**. Доминирующее слагаемое; именно оно опровергало прежнюю оценку;
>   - `subject` ≤ 998 (`:230`) и `in_reply_to` ≤ `_MAX_IN_REPLY_TO_LEN` = 998 (`:232`, константа `:166`) ⇒ ≈ 6 KiB каждый;
>   - `to`/`cc` — **схема НЕ ограничивает байты**: `Field(..., max_length=100)` (`:228-229`) ограничивает **длину списка** (≤ 100 элементов), а не длину элемента, и `_EMAIL_RE = ^[^@\s]+@[^@\s]+\.[^@\s]+$` (`backend/app/send/schemas.py:16`, применяется `_validate_addresses:19-28`) принимает адрес **любой длины**. Реалистичный потолок — 100 × 254 (RFC 5321 §4.5.3.1.3) × 6 ≈ **150 KiB**, но это свойство реальных адресов, **не гарантия кода**. Формально схема примет 100 «адресов» по мегабайту — такую форму запроса отсекает именно `client_max_body_size`, и в этом одна из его функций. Per-item bound как hardening — **`TD-061`** (открыт, не блокер).
>
> Сумма нормируемых схемой полей: 384 + 6 + 6 ≈ **396 KiB**, плюс реалистичные `to`/`cc` ≈ 150 KiB ⇒ **≈ 546 KiB < 1 MiB**, т.е. слагаемое «+ 1 MiB» формулы ниже покрывает их с запасом.
>
> **Итог — РЕАЛИСТИЧНЫЙ worst case (1 MiB текста письма любым живым языком, BMP): 6 MiB + ≈ 0.5 MiB ≈ 6.4 MiB < 8 MiB.** Формальный потолок схемы **выше `8m`** по двум независимым причинам (обе — один класс: схема допускает то, чего реальное письмо не содержит): (1) сплошной astral-текст — 12 MiB; (2) `to`/`cc` без per-item bound (`TD-061`). Ни одна не делает `8m` неверным — см. вывод.
>
> `8m` покрывает реалистичный worst case (6 MiB + поля) с запасом и при этом сжимает поверхность DoS вчетверо против `30m`. **Регрессии нет: `8m` строго больше любого РЕАЛИСТИЧНОГО валидного тела** (текст письма ≤ 1 MiB код-поинтов на живом языке + реальные адреса ≤ 254 октета) — такое тело отвергнуто быть не может.
>
> Оговорка «реалистичного» **существенна и не является ослаблением вывода**. Схема формально примет и тело сверх `8m` — сплошной astral-текст (12 MiB) или `to`/`cc` без per-item bound (`TD-061`); оба получат `413` на прокси. Это не дефект лимита, а его **прямое назначение**: письмо из миллиона эмодзи подряд или сотни мегабайтных «адресов» — не валидная почта, а мусор/DoS, и отсекать такое до приложения — ровно то, ради чего `client_max_body_size` существует. Класс тот же, что честно зафиксирован для `to`/`cc`: **граница схемы (код-поинты) и граница прокси (байты) меряют разные величины**, и там, где схема допускает больше байт, чем осмысленно, разницу добирает прокси. Поднимать `8m` под astral-worst-case (24 MiB) означало бы вернуть ту самую поверхность DoS, ради снятия которой лимит и опускали. Прежнее `30m` под 25 MiB вложений более ничего не защищает: путь, ради которого оно ставилось, физически отсутствует.
> **При изменении `max_length` у `ExternalSendRequest.body_text` (или добавлении бинарного поля/upload'а) пересчитать `client_max_body_size` и обновить обе точки — эту строку и директиву `deploy/nginx/nginx.conf:80` (+ её обоснование в комментарии `:56-79`).** Формула — **`6 × max_length + 1 MiB`, и это ЯВНО BMP-оценка** (реалистичный worst case, по которому и выбран действующий `8m`), **а НЕ формальный потолок схемы**: множитель `6` верен только для BMP, у astral-код-поинтов он **`12`** (surrogate-пара). Полный потолок = `12 × max_length + 1 MiB`. Применять формулу вслепую нельзя — сначала решить, какой из двух случаев нормируется:
> - оставляем BMP-оценку (как сейчас) ⇒ осознанно принимаем, что сплошной astral-текст получит `413`, и это by design (мусор/DoS отсекает прокси);
> - хотим пропускать и astral ⇒ считать по `12 ×` и **удвоить** лимит, предварительно взвесив возврат поверхности DoS.
>
> Слагаемое «+ 1 MiB» — не запас «на глаз», а bound остальных полей (≈ 546 KiB, расчёт выше): **при подъёме `_MAX_REFS_LEN` (`backend/app/external/schemas.py:167`) выше ≈ 145 000 символов оно перестаёт покрывать `refs`, и формулу нужно пересчитывать целиком, а не только по `body_text`.**
- `proxy_read_timeout 60s` / `proxy_send_timeout 60s` / `proxy_connect_timeout 5s` — выровнено с gunicorn `--timeout=60` в api Dockerfile.

> **Инвариант цепочки бюджетов (ADR-0047 §2.1 + ADR-0032 follow-up) — `proxy_read_timeout` менять только вместе с ним.**
> Каждый долгий HTTP-путь агрегатора обязан вернуть **доменную ошибку JSON раньше**, чем nginx превратит ожидание в `504` **HTML** (иначе причина отказа теряется у клиента — прод-баг проверки ящика):
> ```
> connection-test:  MAILBOX_TEST_DEADLINE_SECONDS 45  +  teardown ≤ 5  +  внепробная ≤ 5  = 55  <  60
> send (reply):     SMTP 20 + IMAP APPEND 30 + 5 = 55                                            <  60
>                                                    proxy_read_timeout = gunicorn --timeout
> ```
> У connection-test верхнюю границу **probe** задаёт **только** hard-deadline (`MAILBOX_TEST_DEADLINE_SECONDS`), но граница **ответа** = дедлайн + **два** нормированных слагаемых: **teardown после отмены** (`asyncio.wait_for` дожидается `finally` отменённой пробы — вежливый `QUIT` под `_SMTP_QUIT_TIMEOUT = 5`, `accounts/testers.py:65`; `ADR-0047` §2.3) и **внепробная часть запроса** (auth / SELECT / decrypt / INSERT / сериализация DTO — ≤ 5 с, `ADR-0047` §5). Сумма сокет-таймаутов `_IMAP_TIMEOUT + _SMTP_TIMEOUT` (20+20) в инвариант **не входит**: это эвристика fail-fast, а не bound (`ADR-0047` §2.2). У send — наоборот: там сокет-таймаут `aiosmtplib` покрывает всю последовательность, поэтому сумма констант и есть граница.
> Наружу цепочка продолжается на стороне CRM (`ADR-053` §1.1/§1.2/§1.2.1/§6): `60 < MAIL_API_MAILSERVER_TIMEOUT_SEC (75, read-фаза одного вызова) < MAIL_API_MAILSERVER_DEADLINE_SEC (85, overall ОДНОГО вызова)`, а наружное звено CRM нормировано **по бюджету ЗАПРОСА, а не вызова** (`ADR-053` §1.2.1): `Σ overall всех вызовов запроса + внепробная работа CRM (≤5) < proxy_read_timeout nginx CRM (120)`; худший путь CRM (`create` + компенсирующее удаление сироты) = `85 + 15 + 5 = 105 < 120` (`ADR-053` §6). Прежнее звено «nginx CRM **90**» **отменено**: оно нормировалось против одного вызова и не выдерживало пути с двумя (85 + компенсация → 115 > 90). Цепочка строго возрастает, поэтому любой ответ агрегатора доходит до CRM. Числа CRM-стороны сверять по `ADR-053`, агрегатор их не задаёт.
> Бюджет connection-test страхуется машинно: `MAILBOX_TEST_DEADLINE_SECONDS` объявлен как `Field(default=45, ge=10, le=45)` (`shared/config.py:169` — норма амендмента **в коде**, TD-058 закрыт) → при худшем допустимом значении `45 + 5 + 5 = 55 < 60` (формула `le ≤ 60 − teardown 5 − внепробная 5 − резерв 5`, `ADR-0047` §5). Прежнее `le=50` **отменено**: оно пропускало значение `50`, дающее ровно `50 + 5 + 5 = 60` = `proxy_read_timeout` → `504` HTML; сегодня такое значение падает на валидации при старте. Бюджет send — константы модуля `send/service.py:50-51`. **Снижение `proxy_read_timeout` ниже 60s ломает оба** и требует ADR; **подъём** `MAILBOX_TEST_DEADLINE_SECONDS` выше 45 требует одновременного подъёма `proxy_read_timeout` + gunicorn `--timeout` и пересчёта `le` по формуле.

### Установка certbot на хост (одноразово)

```bash
sudo apt-get update && sudo apt-get install -y certbot
sudo install -d -m 755 /var/www/certbot
```

### Первое получение cert (standalone)

В первый запуск nginx падает (нет cert). Cert выпускаем host-командой, пока порт 80 свободен (nginx ещё не поднят):

```bash
# поднимаем всё КРОМЕ nginx (nginx — единственный сервис под --profile prod)
docker compose up -d postgres redis minio minio-bootstrap mas-migrations api worker

# host-certbot standalone — сам занимает порт 80 на время challenge
sudo certbot certonly --standalone \
  -d "$SERVER_DOMAIN" --email "$ACME_EMAIL" --agree-tos --no-eff-email --non-interactive

# теперь поднимаем nginx (он bind-mount'ит host /etc/letsencrypt и /var/www/certbot)
docker compose --profile prod up -d nginx
```

После первого выпуска рекомендуется переключить renewal на **webroot** (nginx уже держит порт 80), чтобы обновление не требовало остановки nginx — см. ниже.

### Renewal (host `certbot.timer`)

Renewal выполняет системный `certbot.timer` (ставится автоматически пакетом `certbot`; проверить — `systemctl list-timers certbot.timer`). Он гоняет `certbot renew` дважды в сутки; renew срабатывает фактически только когда до истечения < 30 дней.

Метод обновления — **webroot** (nginx постоянно слушает `:80` и раздаёт `/.well-known/acme-challenge/` из `/var/www/certbot`), поэтому останавливать nginx не нужно. Renewal-конфиг домена (`/etc/letsencrypt/renewal/<domain>.conf`) должен указывать `authenticator = webroot` и `webroot_path = /var/www/certbot`.

После успешного renewal контейнер nginx нужно перезагрузить, чтобы подхватить новый cert. Хостовый `deploy-hook` делает graceful reload:

```bash
# /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh  (chmod +x)
#!/bin/sh
cd /opt/mail-agregator && docker compose --profile prod exec -T nginx nginx -s reload
```

`nginx -s reload` graceful — без потерь активных соединений. Хук выполняется только при фактическом обновлении cert. Как страховочный вариант допустим еженедельный host-cron с тем же `nginx -s reload` (недельное окно безопасно покрывает 60-дневный rotation window).

В dev TLS не терминируется — контейнера nginx нет под `--profile prod`. API публикуется на `127.0.0.1:8080` через `docker-compose.override.yml`.

---

## 7. Bootstrapping

### Первый запуск

```bash
git clone <repo>
cd mail-agregator
cp .env.example .env
# Отредактировать .env: MAIL_ENCRYPTION_KEY (единственный обязательный на старте),
#   POSTGRES_PASSWORD, EXTERNAL_API_KEY, CRM_INGEST_URL + CRM_PUSH_SECRET,
#   SERVER_DOMAIN, ACME_EMAIL, APP_BASE_URL
# ADMIN_PASSWORD / MINIO_* / S3_* — СНЯТЫ (ADR-0044 A3 + Фаза G), не задавать.
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

UI доступен на `https://${SERVER_DOMAIN}/login`. Полный operator-runbook по подъёму свежего prod-сервера (DNS, firewall, GH secrets, host-certbot, бэкапы) — `docs/SERVER-SETUP.md`. Перенос существующего прода на новый сервер — секция 15 ниже.

### Обновление (deploy)

Автоматический путь — push в `main`, CI собирает и пушит образы в GHCR, `deploy.yml` забирает их на сервер. См. `docs/SERVER-SETUP.md` Часть E.

Порядок деплоя на сервере (тот же, что выполняет `deploy.yml` через SSH — миграции вызываются **явно** перед стартом нового кода):

```bash
cd /opt/mail-agregator
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
  cd /opt/mail-agregator
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
- Required GH Secrets (фактические имена/значения, см. `.github/workflows/deploy.yml`):
  - `DEPLOY_HOST` — прод: `49.12.189.77` (**единственный** Secret, изменённый при переезде 2026-07-01; ранее `132.243.113.117`).
  - `DEPLOY_USER` — прод: `root`.
  - `DEPLOY_KEY_B64` — **base64** приватного SSH-ключа (не `DEPLOY_KEY`!). Workflow декодирует его `base64 -d` в `~/.ssh/deploy_key`. base64-кодирование выбрано, потому что однострочное значение без переносов не искажается UI GitHub Secrets. Публичный ключ добавлен в `~root/.ssh/authorized_keys` на сервере; приватный на стороне оператора — `~/.ssh/postapp-deploy`.
  - `DEPLOY_PATH` — прод: `/opt/mail-agregator` (имя каталога = имя репозитория, одна `g`).
- **GHCR-образы публичные** — `docker login ghcr.io` на сервере **не требуется** (файла `~/.docker/config.json` нет; `docker compose pull` тянет `ghcr.io/<owner>/<repo>/{api,worker}` анонимно). Если образы когда-либо станут private — вернуть шаг `docker login` (PAT `read:packages`).
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

### 11.1 ~~Смена пароля супер-админа~~ — СНЯТО

> **ИСТОРИЧЕСКОЕ — процедуру НЕ выполнять, менять нечего.** Интерактивный супер-админ ушёл вместе с cookie-UI ([ADR-0044](./adr/ADR-0044-decommission-runbook.md) A3/§5): `seed_super_admin` снят, полей `ADMIN_LOGIN`/`ADMIN_PASSWORD` в `Settings` нет (`TD-060` (6d)), сессий/Redis-`session:*` не существует. Единственный сидируемый аккаунт — технический `crm-service` с `password_hash=NULL` (`backend/app/auth/service.py:33-70`); пароля у него нет и логин его не принимает. Аутентификация коннектора — только машинная (`EXTERNAL_API_KEY`); её ротация — `06-security.md` §10 / `SERVER-SETUP.md` §H.2. Текст ниже сохранён как история конфигурации до демонтажа.

Супер-админ — единственный, заводился из env (`ADMIN_LOGIN` / `ADMIN_PASSWORD`). UI смены пароля для него отсутствовал сознательно (см. `06-security.md` sec. 10). Процедура **(недействительна)**:

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

Builtin-теги (после [ADR-0040](./adr/ADR-0040-global-tags.md)) — **глобальные** (`user_id IS NULL`, единый каталог) и материализуются функцией `seed_builtin_tags(session)` в **lifespan** приложения (`backend/app/main.py::create_app`, по образцу `seed_super_admin`; см. `03-data-model.md` секция «Заполнение builtin-тегов»). `seed_builtin_tags` идемпотентна (`INSERT … ON CONFLICT (name) WHERE user_id IS NULL DO NOTHING`): если глобальный builtin с таким именем уже есть, она его **не** переписывает. Поэтому сам по себе новый код после деплоя не обновит правила уже существующих builtin-тегов — без дополнительного шага в каталоге останутся старые правила. (Прежний per-login механизм `TagsService.ensure_builtin_tags` + релогин отменён ADR-0040; UI-логина в агрегаторе не будет — ADR-0041.)

Для этого существует **штатный forward-only механизм**, а не ручной data-fix. При изменении состава/правил builtin к деплою прикладывается **forward-only rebuild-миграция**, которая сбрасывает существующие builtin-теги, после чего `seed_builtin_tags` на ближайшем старте приложения пересоздаёт актуальный глобальный каталог:

- Референсный пример — `migrations/versions/20260521_016_rebuild_builtin_tags.py`: `DELETE FROM tags WHERE is_builtin = true` (FK `ON DELETE CASCADE` от `tag_rules` и `message_tags` снимает связанные правила и применённые метки — это намеренно, старые правила устарели). После DELETE глобальных builtin в каталоге нет, и `seed_builtin_tags` на старте нового образа пересоздаёт актуальный каталог.
- Эта миграция применяется **автоматически** init-контейнером `mas-migrations` в рамках деплоя — тем самым явным шагом `docker compose --profile prod run --rm mas-migrations` (см. секцию 9, dependency-цепочка `api → mas-migrations: service_completed_successfully`).
- Миграции forward-only: для отката никогда не используется `alembic downgrade` — вместо отката пишется новая forward-fix миграция (migration policy — `deploy/README.md`, «Migration policy: forward-only»).

Процедура после деплоя нового builtin-каталога:
1. Убедиться, что деплой прошёл и rebuild-миграция применена (новый `IMAGE_TAG` запущен, `mas-migrations` завершился `service_completed_successfully`, `mas-api` healthy).
2. **Перезапуск `mas-api`** (штатно происходит при деплое нового `IMAGE_TAG`) материализует новый каталог: lifespan `seed_builtin_tags` отрабатывает на актуальной (новой) версии `builtin.py`. Отдельного релогина не требуется — каталог глобальный, не per-user.

> **Важно:** `seed_builtin_tags` пересоздаёт builtin только когда глобального builtin с таким именем нет — то есть после rebuild-миграции, обнулившей `tags WHERE is_builtin=true`. Если builtin-теги уже есть и rebuild-миграция не применялась, `ON CONFLICT DO NOTHING` не тронет старые правила. Поэтому раскатка нового builtin-каталога делается rebuild-миграцией (референс — `016`, применяется автоматически `mas-migrations`) + перезапуском `mas-api` (штатный при деплое нового `IMAGE_TAG`). Для исторических писем после rebuild — `POST /api/external/tags/{id}/apply-to-existing` на нужном теге (новое авто-тегирование на входящую почту работает сразу).

### 11.3 Безопасность сервера

- Только SSH (key-based) и 80/443 (nginx) открыты наружу.
- MinIO console (`:9001`) — закрыт firewall'ом, доступ только через VPN или SSH-tunnel.
- Регулярные обновления базовых образов (`docker compose pull` раз в неделю/месяц).
- `MAIL_ENCRYPTION_KEY` хранится в password manager / sealed env (не в git, не в shared чатах).
- `.env` файл на сервере — `chmod 600`, владелец — пользователь, под которым работает docker.

---

## 12. MinIO bootstrap (non-root service account для приложения)

> **⚠️ ИСТОРИЧЕСКИЙ РАЗДЕЛ — MinIO bootstrap целиком (Фаза G, `8e890a2`/`e0bccc3`): сервисы `minio`/`minio-bootstrap`, volume `mas_minio_data`, bucket `mail-attachments`, `deploy/minio-bootstrap.sh` СНЯТО демонтажём ([ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md), выполнено на проде 2026-07-15).** Описанное ниже в коде/проде агрегатора НЕ существует; текст сохранён как record исходного решения. НЕ реализовывать и не принимать за действующий контракт.

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

> **⚠️ ИСТОРИЧЕСКИЙ РАЗДЕЛ — настройка ботов агрегатора (основной + 4 push-бота) СНЯТО демонтажём ([ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md), выполнено на проде 2026-07-15).** Описанное ниже в коде/проде агрегатора НЕ существует; текст сохранён как record исходного решения. НЕ реализовывать и не принимать за действующий контракт. **Актуально:** боты обслуживаются CRM (вебхуки переключены на CRM — [ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) §5); у агрегатора Telegram-env/роутера нет.

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
source /opt/mail-agregator/.env
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
source /opt/mail-agregator/.env
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

## 15. Migration to a new server

Runbook переноса всего прода на новый выделенный хост. Референс — фактический переезд 2026-07-01 (`132.243.113.117` → `49.12.189.77`, Hetzner, Ubuntu 26.04, домен `postapp.store`, ADR-0032). Все данные переносятся 1:1; `MAIL_ENCRYPTION_KEY` сохраняется тем же (иначе зашифрованные пароли ящиков и OAuth refresh-токены станут нерасшифровываемыми).

Переиспользует команды бэкапа из секции 8 (`pg_dump` / MinIO `tar`).

### 15.1 Провижининг нового хоста

```bash
# на НОВОМ сервере (root)
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
apt-get update && apt-get install -y certbot
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable
install -d /opt && git clone https://github.com/<owner>/<repo>.git /opt/mail-agregator
install -d -m 755 /var/www/certbot
```

Публичный SSH-ключ деплоя (тот же, что в `DEPLOY_KEY_B64`) — в `~root/.ssh/authorized_keys` нового хоста.

### 15.2 Останов записи на СТАРОМ сервере (anti split-brain)

До снятия дампа остановить всё, что пишет в БД/IMAP/шлёт уведомления, иначе новые письма после `pg_dump` потеряются:

```bash
# на СТАРОМ сервере
cd /opt/mail-agregator            # (старый путь мог быть /opt/mail-aggregator)
docker compose --profile prod stop worker api
```

`postgres`/`minio` оставляем поднятыми — они нужны для дампа. С этого момента начинается короткий downtime.

### 15.3 Снятие дампов на старом сервере

```bash
# PostgreSQL (custom format — как в секции 8)
docker exec mas-postgres pg_dump -U mas -d mail_aggregator -F c -f /tmp/dump.bin
docker cp mas-postgres:/tmp/dump.bin ./pg-dump.bin

# MinIO volume (tar, как в секции 8)
docker run --rm -v mas_minio_data:/data:ro -v "$PWD":/out alpine \
  tar czf /out/minio-data.tar.gz -C /data .
```

### 15.4 Перенос артефактов на новый сервер

Переносятся: `pg-dump.bin`, `minio-data.tar.gz`, `.env` (вербатим, включая тот же `MAIL_ENCRYPTION_KEY`), host-каталог TLS `/etc/letsencrypt`.

```bash
# со старого хоста (или через промежуточную машину)
scp pg-dump.bin minio-data.tar.gz root@49.12.189.77:/opt/mail-agregator/
scp /opt/mail-agregator/.env       root@49.12.189.77:/opt/mail-agregator/.env
# TLS: сохранить симлинки (live/ ссылается на archive/) — tar с хоста
tar czf letsencrypt.tar.gz -C /etc letsencrypt
scp letsencrypt.tar.gz root@49.12.189.77:/tmp/
```

На новом хосте:

```bash
cd /opt/mail-agregator
chmod 600 .env
tar xzf /tmp/letsencrypt.tar.gz -C /etc        # восстанавливает /etc/letsencrypt (cert postapp.store)
```

> Перенос `/etc/letsencrypt` целиком нужен, чтобы **не** перевыпускать cert (экономит Let's Encrypt rate-limit) и чтобы renewal продолжил работать. `certbot.timer` на новом хосте подхватит существующий renewal-конфиг после установки пакета `certbot` (шаг 15.1).

### 15.5 Восстановление данных на новом сервере

```bash
cd /opt/mail-agregator
# поднять только data-tier (без api/worker/nginx)
docker compose up -d postgres minio minio-bootstrap
docker compose run --rm mas-migrations         # создаёт схему в свежем volume

# PostgreSQL restore (как в секции 8)
docker cp pg-dump.bin mas-postgres:/tmp/dump.bin
docker exec -i mas-postgres pg_restore -U mas -d mail_aggregator -c /tmp/dump.bin

# MinIO restore: распаковать tar обратно в volume
docker run --rm -v mas_minio_data:/data -v "$PWD":/in alpine \
  sh -c "cd /data && tar xzf /in/minio-data.tar.gz"
```

Затем поднять стек и подтянуть образы:

```bash
docker compose --profile prod pull api worker      # GHCR публичный — docker login не нужен
docker compose --profile prod up -d --force-recreate api worker nginx
docker compose ps
```

### 15.6 Cutover: переключение трафика и деплоя

Порядок важен, чтобы не осталось split-brain (два живых прода):

1. Убедиться, что новый стек healthy: `docker compose ps`, `curl -ksI https://49.12.189.77/healthz --resolve postapp.store:443:49.12.189.77`.
2. Обновить **только** GitHub Secret `DEPLOY_HOST` → `49.12.189.77` (`DEPLOY_USER`/`DEPLOY_KEY_B64`/`DEPLOY_PATH` не меняются — user/key/path идентичны).
3. Перенацелить A-запись `postapp.store` (reg.ru) на `49.12.189.77`. TTL DNS определяет длительность окна, когда часть трафика ещё идёт на старый IP.
4. Дождаться пропагации DNS: `dig +short postapp.store` → `49.12.189.77`.
5. Полностью остановить старый прод, чтобы исключить приём почты/уведомлений двумя серверами:
   ```bash
   # на СТАРОМ сервере, после подтверждения работы нового
   docker compose --profile prod down
   ```
6. Прогнать post-deploy smoke-test (Part D в `SERVER-SETUP.md`) и, для push-ботов, повторить `setWebhook` не нужно — webhook'и указывают на домен `postapp.store`, а не на IP, поэтому смена IP их не ломает.

### 15.7 Проверка после переезда

- `curl -sSI https://postapp.store/healthz | head -1` → `HTTP/2 200`.
- `curl -sSI https://postapp.store/healthz | grep -i strict-transport` → HSTS присутствует (TLS-cert валиден, перенесён из `/etc/letsencrypt`).
- Логин супер-админа → inbox рендерится, письма и вложения на месте (значит pg + MinIO + `MAIL_ENCRYPTION_KEY` перенесены корректно).
- `docker logs mas-worker --tail 50` → sync-циклы идут без `login failed`/decrypt-ошибок (подтверждает совпадение `MAIL_ENCRYPTION_KEY`).
- Тестовый push через любого настроенного Telegram-бота доходит (webhook по домену не пострадал).
- Ручной прогон deploy'а (`workflow_dispatch`) успешно SSH'ится на новый `DEPLOY_HOST`.

---
