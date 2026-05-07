# ADR Index

Реестр архитектурных решений (Architecture Decision Records). Формат файла: `ADR-NNNN-<slug>.md`. Каждый ADR содержит секции **Context / Decision / Consequences / Alternatives considered**. Решения иммутабельны: если решение меняется — заводится новый ADR со ссылкой на устаревший, статус старого меняется на `superseded by ADR-XXXX`.

| ID | Название | Статус | Дата |
| --- | --- | --- | --- |
| [ADR-0001](./ADR-0001-tech-stack.md) | Базовый технологический стек (FastAPI + PostgreSQL + MinIO + Jinja2) | accepted | 2026-05-05 |
| [ADR-0002](./ADR-0002-imap-library.md) | Выбор IMAP/SMTP-библиотек (imap-tools sync в worker, aiosmtplib async) | accepted | 2026-05-05 |
| [ADR-0003](./ADR-0003-worker.md) | Background worker — APScheduler в отдельном контейнере | accepted | 2026-05-05 |
| [ADR-0004](./ADR-0004-sessions.md) | Сессии — server-side через Redis, opaque cookie | accepted | 2026-05-05 |
| [ADR-0005](./ADR-0005-encryption.md) | Шифрование почтовых паролей — AES-256-GCM с per-record IV | accepted | 2026-05-05 |
| [ADR-0006](./ADR-0006-password-hashing.md) | Хеширование паролей пользователей — argon2id | accepted | 2026-05-05 |
| [ADR-0007](./ADR-0007-storage-scheme.md) | Схема хранения вложений в MinIO | accepted | 2026-05-05 |
| [ADR-0008](./ADR-0008-sync-strategy.md) | Стратегия инкрементальной IMAP-синхронизации (UIDNEXT-based, 30-day initial backfill) | accepted | 2026-05-05 |
| [ADR-0009](./ADR-0009-rate-limiting.md) | Rate-limit на login и password-set | accepted | 2026-05-05 |
| [ADR-0010](./ADR-0010-csrf-protection.md) | CSRF-защита для всех cookie-аутентифицированных POST | accepted | 2026-05-05 |
| [ADR-0011](./ADR-0011-retention.md) | Ретенция писем и вложений — 30 дней, daily cleanup | accepted | 2026-05-05 |
| [ADR-0012](./ADR-0012-message-body-storage.md) | Хранение тел писем — plain text only в PostgreSQL (TEXT) | accepted | 2026-05-05 |
| [ADR-0013](./ADR-0013-concurrency-model.md) | Конкурентность IMAP-сессий — asyncio.Semaphore=10 + thread pool для sync-библиотеки | accepted | 2026-05-05 |
| [ADR-0014](./ADR-0014-logging.md) | Логирование — structlog, JSON в stdout, request_id correlation | accepted | 2026-05-05 |
| [ADR-0015](./ADR-0015-no-js-fallback.md) | No-JS fallback — `_method` override + form-encoded acceptance + content negotiation | accepted | 2026-05-05 |
| [ADR-0016](./ADR-0016-two-step-login.md) | Two-step login (username then password, ADR-style flow as Google) | accepted | 2026-05-06 |
| [ADR-0017](./ADR-0017-tags.md) | Теги для писем — rule-based авто-классификация и пользовательские правила | accepted | 2026-05-07 |
