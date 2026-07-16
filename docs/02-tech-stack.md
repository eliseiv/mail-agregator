# 02. Технологический стек

Базовые решения зафиксированы в [ADR-0001](./adr/ADR-0001-tech-stack.md). Этот документ — нормативный список версий и обоснование на уровне таблицы. При обновлении версий — править здесь и оставлять changelog в нижней секции.

> **⚠️ ДЕМОНТАЖ ВЫПОЛНЕН (2026-07-15) — часть стека ниже описывает СНЯТЫЕ подсистемы.** По [ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md)/[ADR-0044](./adr/ADR-0044-decommission-runbook.md) агрегатор сведён к mail-connector'у. **MinIO/S3 сняты полностью** (Фаза G, коммиты `8e890a2`/`e0bccc3`): `aioboto3`, сервисы MinIO, bucket `mail-attachments`, S3-env — удалены (строки ниже помечены как исторические). Также сняты Telegram/webhooks/forwarding/tags/groups и Jinja-UI/static (записи `Jinja2`, `slowapi`, sessions-UI, httpx→Telegram Bot API — историчны в части UI/нотификаций; `httpx` остаётся для OAuth Microsoft). Посекционная вычистка этого документа ведётся под **`TD-050`(в)**; здесь помечены только MinIO/S3-строки (задача синхронизации реестров 2026-07-15).

---

## Backend

| Категория | Технология | Версия (минимум) | Зачем |
| --- | --- | --- | --- |
| Язык | Python | **3.12** | Современный async, PEP 695 generics, performance |
| Web framework | FastAPI | **0.115** | Async, OpenAPI, Pydantic v2 native |
| ASGI server (dev) | uvicorn | **0.30** | Стандарт для FastAPI |
| ASGI server (prod) | gunicorn + uvicorn workers | gunicorn **22** | Управление воркерами, graceful reload |
| ORM | SQLAlchemy | **2.0** | Async, type-safe selects |
| DB driver | asyncpg | **0.29** | Самый быстрый async-драйвер Postgres |
| Migrations | Alembic | **1.13** | Стандарт для SQLAlchemy |
| Validation / serialization | Pydantic | **2.7** | Используется FastAPI; v2 быстрее v1 |
| Templating | Jinja2 | **3.1** | SSR без билд-степа |
| Static files | starlette `StaticFiles` | (входит в FastAPI) | Раздача CSS/JS |
| Crypto | cryptography | **42** | AES-GCM (ADR-0005) |
| Password hashing | argon2-cffi | **23.1** | argon2id (ADR-0006) |
| HTTP client | httpx | **0.27** | Тесты, Telegram Bot API, **OAuth token endpoint Microsoft (ADR-0025)** |
| Logging | structlog | **24.1** | Структурные JSON-логи (ADR-0014) |
| Rate limiting | slowapi | **0.1.9** | Поверх Redis (ADR-0009) |
| Sessions backend | redis (python client) | **5.0** | Async; sessions, rate-limit, lockout |
| HTML→text | html2text | **2024.2** | Конвертация HTML-писем в plain text (ADR-0012) |
| Email parsing/build | stdlib `email` + `imap-tools` | imap-tools **1.6** | IMAP fetch + парсинг (ADR-0002). **XOAUTH2 (ADR-0025):** через нижележащий `imaplib.authenticate("XOAUTH2", …)` — `imap-tools` `MailBox.client`; проверить совместимость версии (TD-030). |
| SMTP | aiosmtplib | **3.0** | Async SMTP send (ADR-0002). **XOAUTH2 (ADR-0025):** через `AUTH XOAUTH2 <base64>`; проверить механизм в версии (TD-030). |
| OAuth2 Microsoft | **без отдельной библиотеки (httpx)** | — | **ADR-0025:** authorize/token-flow реализуем вручную на `httpx` (tenant `common`). MSAL не вводим — flow простой (auth-code + refresh), а MSAL тянет лишние зависимости и кэш-абстракции, не нужные при хранении токенов в БД. |
| ~~MinIO/S3 client~~ | ~~aioboto3~~ | ~~**13.2.0**~~ | **СНЯТ (ADR-0044 Фаза G, 2026-07-15):** `aioboto3` удалён из зависимостей вместе с `shared/storage.py`. Историческая запись. |
| Scheduler (worker) | APScheduler | **3.10** | Cron + interval triggers (ADR-0003) |

---

## Хранилища

| Сервис | Версия | Роль |
| --- | --- | --- |
| PostgreSQL | **16** (16.4+ recommended) | Основная БД (метаданные, аудит) |
| Redis | **7.2** | Sessions, rate-limit, временные setup-сессии |
| ~~MinIO~~ | ~~`RELEASE.2024-08-29T01-40-52Z`~~ | **СНЯТ (ADR-0044 Фаза G, 2026-07-15):** сервисы `minio`/`minio-bootstrap`, volume `mas_minio_data`, bucket `mail-attachments` удалены. Коннектор не хранит вложений. Историческая запись. |

---

## Frontend

| Компонент | Технология | Версия |
| --- | --- | --- |
| Templating | Jinja2 (рендерится `api`) | 3.1 |
| CSS | Vanilla CSS, без фреймворка | n/a |
| JS | Vanilla ES2022, без транспиляции, без bundler'а | n/a |
| Иконки | Inline SVG (без иконочного шрифта) | n/a |

Подробности UX в [`08-frontend.md`](./08-frontend.md).

---

## Тесты, lint, type-check

| Инструмент | Версия | Роль |
| --- | --- | --- |
| pytest | **8** | Тест-раннер |
| pytest-asyncio | **0.23** | Async test support |
| coverage.py | **7** | Coverage report |
| ruff | **0.5** | Lint + format (заменяет black/isort/flake8) |
| mypy | **1.10** | Static type-check |
| pre-commit | **3.7** | (опционально) hooks |

Минимальный coverage gate: **75%** на core (`backend/app/services`, `backend/app/repositories`, `worker/`). Тестовая стратегия и обязательный набор инвариантов — в [`05-modules.md`](./05-modules.md) (раздел "20. QA / тесты — обязательный набор для backend/worker"). Если потребуется отдельный документ test-strategy — заведёт QA-агент по согласованию с архитектором.

---

## Инфраструктура / DevOps

| Инструмент | Версия | Роль |
| --- | --- | --- |
| Docker Engine | 24+ | Контейнеризация |
| docker compose | v2 | Локальный и prod-оркестратор |
| GitHub Actions | n/a | CI/CD pipeline |
| Nginx (reverse proxy) | 1.27 alpine | TLS termination, reverse proxy в проде (см. `docs/07-deployment.md` sec. 6) |
| Certbot (Let's Encrypt) | 2.11.0 | Получение и auto-renewal TLS-сертификата (см. `docs/07-deployment.md` sec. 6) |

---

## Запреты и ограничения

- **Celery** — запрещён (overkill, см. ADR-0003).
- **Frontend SPA-фреймворки (React/Vue/Svelte)** — не используются.
- ~~**Локальный диск для вложений** — нельзя; только MinIO.~~ **(неактуально с 2026-07-15, ADR-0044 Фаза G: вложения не хранятся — MinIO снят, коннектор пушит письма в CRM без вложений.)**
- **Pickle для сессий/кэша** — нельзя (RCE-риск); только JSON.
- **Логирование секретов** — запрещено (см. ADR-0014, redact-list).
- **Создание ключей шифрования в коде** — запрещено; только из env.

---

## Changelog

| Дата | Изменение | Автор |
| --- | --- | --- |
| 2026-05-05 | Initial. | architect |
| 2026-05-27 | ADR-0025 (Outlook OAuth2): OAuth token endpoint вызывается через уже имеющийся `httpx` (async); XOAUTH2 для IMAP/SMTP строится через `imap-tools`/`aiosmtplib` thin-helpers (TD-030). MSAL и иные OAuth-библиотеки **не вводятся** — минимизация зависимостей. | architect |
| 2026-05-27 | ADR-0024 (multi-TG 1:N): снят `UNIQUE(telegram_links.user_id)` → один user может иметь несколько Telegram-привязок (1:N); новый env `TG_MAX_LINKS_PER_USER` (default 10) — мягкий лимит против абьюза. Новых библиотек не требует. | architect |
| 2026-06-09 | ADR-0027 (push-only боты по командам): 3 доп. бота `ivan`/`alexandra`/`andrei`, отдельная Redis-очередь `push_notify_queue` + worker-job `push_notify_dispatch`, токен-параметризация `bot.send_notification`. Новые env: `BOT_{IVAN,ALEXANDRA,ANDREI}_TOKEN` + `*_GROUP_ID` + `ADMIN_TELEGRAM_IDS` + `PUSH_NOTIFY_DISPATCH_INTERVAL_SECONDS`/`PUSH_NOTIFY_BATCH_SIZE`. Fire-and-forget — без БД-трекинга/recovery/миграций. Новых библиотек/инфраструктуры не требует (reuse httpx/Redis/APScheduler). | architect |
| 2026-06-24 | ADR-0027 round-44 (+`business2`, 4-й push-бот): добавлен 4-й push-only бот `business2` по образцу остальных. Новые env: `BOT_BUSINESS2_TOKEN`/`_GROUP_ID`/`_WEBHOOK_SECRET` (прод `group_id` задаёт оператор, ≠1/2/3). Роут `push-webhook/{bot_name}` и диспатчер `push_notify_dispatch` — generic; change только `shared/config.py` (поля + перечисление) + `shared/logging.py` (redact). Новых endpoint/job/миграций/библиотек нет. | architect |
