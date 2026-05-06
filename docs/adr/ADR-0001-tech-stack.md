# ADR-0001: Базовый технологический стек

- **Статус:** accepted
- **Дата:** 2026-05-05

## Context

Нужен стек для сервиса-агрегатора почты с типичной нагрузкой:
- ~5 пользователей × ~100 IMAP-аккаунтов = ~500 IMAP-сессий каждые 5 минут;
- веб-UI без сложных интерактивов (списки, формы, просмотр письма);
- хранение метаданных писем (PostgreSQL) и бинарных вложений (object storage);
- одна команда из 1–2 разработчиков, простой деплой через docker-compose;
- Windows-первая dev-среда, Linux-целевой сервер.

Критерии:
1. Минимум движущихся частей.
2. Хорошая поддержка async I/O (IMAP/SMTP пуллинг — I/O-bound).
3. Низкий entry barrier для новых разработчиков.
4. Зрелая экосистема для типичных задач (auth, БД, шаблоны).
5. Совместимость с docker-compose и CI на GitHub Actions.

## Decision

| Слой | Технология | Версия |
| --- | --- | --- |
| Backend framework | **FastAPI** | 0.115+ |
| ASGI server | **uvicorn** (с workers через gunicorn в проде) | uvicorn 0.30+ / gunicorn 22+ |
| Язык | **Python** | 3.12 |
| ORM | **SQLAlchemy** (async) | 2.0+ |
| Migrations | **Alembic** | 1.13+ |
| Validation | **Pydantic** | 2.7+ |
| RDBMS | **PostgreSQL** | 16 |
| Postgres async driver | **asyncpg** | 0.29+ |
| Cache / sessions | **Redis** | 7.2 |
| Object storage | **MinIO** (S3 API), клиент `aioboto3` | server `RELEASE.2024-08-29T01-40-52Z`; aioboto3 `13.2.0` |
| Frontend | Jinja2-шаблоны + vanilla JS + минималистичный CSS (без фреймворка) | Jinja2 3.1+ |
| HTTP client (для тестов SMTP/IMAP — n/a, для будущих интеграций) | `httpx` | 0.27+ |
| Логирование | `structlog` | 24+ |
| Тесты | `pytest`, `pytest-asyncio`, `httpx` test client | pytest 8+ |
| Lint / format | `ruff` | 0.5+ |
| Type-check | `mypy` (strict для core, relaxed для тестов) | 1.10+ |
| CI | GitHub Actions | n/a |
| Контейнеризация | Docker + docker-compose v2 | n/a |

## Consequences

**Плюсы:**
- FastAPI + SQLAlchemy 2.0 async — стандарт де-факто для Python REST + SSR.
- Pydantic v2 — единый слой валидации запросов и сериализации ответов.
- Jinja2 + vanilla JS — никакого билд-степа, никакой ноды в проде. UI — простые HTML-формы и небольшой JS для UX-улучшений.
- MinIO даёт S3-совместимое API локально и в проде, без vendor-lock.
- Redis закрывает сессии и rate-limit одним сервисом.

**Минусы / риски:**
- SQLAlchemy 2.0 async требует аккуратной работы с сессиями; mitigation — единый dependency-инжектор `get_session()` в FastAPI.
- Vanilla JS UI потребует дисциплины, чтобы не превратиться в спагетти; mitigation — список разрешённых компонентов и стилгайд в `08-frontend.md`.

## Alternatives considered

| Альтернатива | Почему отклонена |
| --- | --- |
| Django | Тяжелее для async I/O; admin-панель Django избыточна (нам нужен кастомный UI). |
| Flask + extensions | Хуже async I/O, ручная сборка валидации/OpenAPI. |
| Node.js (NestJS / Fastify) | Команда сильнее в Python; нет нужды в JS на бэке. |
| React/Vue SPA | Усложнение деплоя (отдельный build), CSRF/auth flows сложнее, не оправдано для столь простого UI. |
| MongoDB вместо PostgreSQL | Нужны транзакции (создание пользователя + audit log), реляционная модель естественна. |
| Локальный диск вместо MinIO | Сложнее бэкапить, нет S3-совместимости для будущего перехода в облако. |
| SQLite | Слабая конкурентность для worker + web одновременно. |

## Revisions

- **2026-05-05 (rev. 2):** зафиксированы точные версии MinIO-стека: server image `RELEASE.2024-08-29T01-40-52Z`, клиент `aioboto3==13.2.0`. Версии синхронизированы с `02-tech-stack.md` и `07-deployment.md` sec. 1.
