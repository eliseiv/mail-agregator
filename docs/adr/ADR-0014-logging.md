# ADR-0014: Логирование — structlog, JSON в stdout

- **Статус:** accepted
- **Дата:** 2026-05-05

## Context

В docker-окружении стандартный паттерн — логирование в stdout, агрегация снаружи (Docker logs / Loki / etc.). Нужен структурированный JSON для удобства поиска. Должны быть корреляционные поля для трассировки одного HTTP-запроса по всем компонентам и одного sync-цикла.

## Decision

- Библиотека: **structlog** >= 24.
- Формат: JSON (через `structlog.processors.JSONRenderer`).
- Поток: stdout (для контейнеров) + stderr (только для CRITICAL).
- Уровень: `INFO` по умолчанию, `DEBUG` через env `LOG_LEVEL`.
- Стандартные поля каждой записи:
  - `timestamp` (ISO8601 UTC).
  - `level` (info/warning/error/...).
  - `event` (короткое имя события, snake_case).
  - `service` ("api" / "worker").
  - `request_id` (для API; UUID4, генерируется на каждый HTTP-запрос middleware).
  - `cycle_id` (для worker; UUID4 на sync-цикл).
  - `user_id` (если авторизован).
  - `mail_account_id` (если применимо).
- В FastAPI — middleware `RequestIDMiddleware`, добавляет `request_id` в контекст и в response header `X-Request-ID`.
- В worker'е — каждый sync_cycle оборачивается в `with structlog.contextvars.bound_contextvars(cycle_id=...):`.
- Запрещено логировать: сырые пароли, IMAP/SMTP credentials, тела писем, тела вложений, CSRF-токены, session-токены. Эти поля помечены как redact в configurable allowlist.

## Consequences

**Плюсы:**
- Машиночитаемые логи, готовы к ingestion в Loki/ELK/CloudWatch.
- Корреляция через `request_id` / `cycle_id` упрощает debug.

**Минусы:**
- structlog требует немного boilerplate (configure once at startup). Это разовая стоимость.

## Alternatives considered

- **stdlib logging + JSON formatter**: рабочий вариант, но structlog даёт более удобный fluent API и context binding из коробки.
- **loguru**: проще, но менее интегрирован с FastAPI/uvicorn middleware.
