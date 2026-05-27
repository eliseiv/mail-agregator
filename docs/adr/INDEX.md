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
| [ADR-0018](./ADR-0018-telegram-launcher.md) | Telegram launcher bot + WebApp без линковки аккаунтов | partially superseded by ADR-0022 | 2026-05-07 |
| [ADR-0019](./ADR-0019-groups-and-roles.md) | Роли пользователей и группы (super_admin / group_leader / group_member) с visibility-моделью | accepted | 2026-05-08 |
| [ADR-0020](./ADR-0020-mail-account-nickname.md) | Никнейм (display_name) у mail-аккаунтов | accepted | 2026-05-08 |
| [ADR-0021](./ADR-0021-russian-localization.md) | Полная RU-локализация UI без i18n-фреймворка | accepted | 2026-05-08 |
| [ADR-0022](./ADR-0022-telegram-sso-and-notifications.md) | Telegram persistent SSO (initData HMAC + `telegram_links`) + push-уведомления о письмах с тегами (отменяет «без линковки» из ADR-0018, закрывает TD-013) | accepted | 2026-05-13 |
| [ADR-0023](./ADR-0023-outbound-webhooks.md) | Outbound webhooks для команд (один webhook на `group_id`, AES-GCM secret, dispatcher по паттерну ADR-0022, фильтр «не флудим историей») | accepted | 2026-05-20 |
| [ADR-0024](./ADR-0024-multi-telegram-links.md) | Несколько Telegram-привязок на один аккаунт (снятие `UNIQUE(telegram_links.user_id)`, ключ идемпотентности `telegram_notifications` → `(message_id, telegram_user_id)`, доставка во все живые чаты; расширяет ADR-0022) | accepted | 2026-05-27 |
| [ADR-0025](./ADR-0025-outlook-oauth2.md) | OAuth2 (XOAUTH2) для личных Outlook (consent через сайт+OctoBrowser, IMAP/SMTP XOAUTH2 напрямую, refresh-токен AES-GCM, расширение `mail_accounts`) | accepted | 2026-05-27 |
