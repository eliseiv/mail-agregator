# 02. Технологический стек

Базовые решения зафиксированы в [ADR-0001](./adr/ADR-0001-tech-stack.md). Этот документ — нормативный список версий и обоснование на уровне таблицы. При обновлении версий — править здесь и оставлять changelog в нижней секции.

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
| HTTP client | httpx | **0.27** | Тесты, будущие интеграции |
| Logging | structlog | **24.1** | Структурные JSON-логи (ADR-0014) |
| Rate limiting | slowapi | **0.1.9** | Поверх Redis (ADR-0009) |
| Sessions backend | redis (python client) | **5.0** | Async; sessions, rate-limit, lockout |
| HTML→text | html2text | **2024.2** | Конвертация HTML-писем в plain text (ADR-0012) |
| Email parsing/build | stdlib `email` + `imap-tools` | imap-tools **1.6** | IMAP fetch + парсинг (ADR-0002) |
| SMTP | aiosmtplib | **3.0** | Async SMTP send (ADR-0002) |
| MinIO/S3 client | aioboto3 | **13.2.0** | Async S3 API |
| Scheduler (worker) | APScheduler | **3.10** | Cron + interval triggers (ADR-0003) |

---

## Хранилища

| Сервис | Версия | Роль |
| --- | --- | --- |
| PostgreSQL | **16** (16.4+ recommended) | Основная БД (метаданные, аудит) |
| Redis | **7.2** | Sessions, rate-limit, временные setup-сессии |
| MinIO | `RELEASE.2024-08-29T01-40-52Z` | Object storage для вложений (точный тег зафиксирован в `07-deployment.md` sec. 1 и в init-контейнере `minio-bootstrap` sec. 12) |

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

Минимальный coverage gate: **75%** на core (`backend/app/services`, `backend/app/repositories`, `worker/`). Тестовая стратегия и обязательный набор инвариантов — в [`05-modules.md`](./05-modules.md) (раздел "17. QA / тесты — обязательный набор для backend/worker"). Если потребуется отдельный документ test-strategy — заведёт QA-агент по согласованию с архитектором.

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
- **Локальный диск для вложений** — нельзя; только MinIO.
- **Pickle для сессий/кэша** — нельзя (RCE-риск); только JSON.
- **Логирование секретов** — запрещено (см. ADR-0014, redact-list).
- **Создание ключей шифрования в коде** — запрещено; только из env.

---

## Changelog

| Дата | Изменение | Автор |
| --- | --- | --- |
| 2026-05-05 | Initial. | architect |
