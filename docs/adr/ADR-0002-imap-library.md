# ADR-0002: Выбор IMAP/SMTP-библиотек

- **Статус:** accepted
- **Дата:** 2026-05-05

## Context

Сервису нужно:
- читать IMAP-папку INBOX каждые 5 минут с ~500 ящиков (incremental fetch по UID);
- получать заголовки + plain-text тело + список вложений;
- сохранять выбранные сообщения (после отправки) в IMAP/Sent;
- отправлять SMTP с поддержкой STARTTLS/SSL и опционально отдельных SMTP-кредов;
- работать с реальными провайдерами (Gmail, Yandex, Mail.ru, Outlook).

Кандидаты в Python-экосистеме:

| Библиотека | Тип | Зрелость | Поведение |
| --- | --- | --- | --- |
| `imaplib` (stdlib) | sync | очень зрелая, но низкоуровневая | требует ручного парсинга RFC 822, многословный код |
| `imap-tools` | sync, обёртка над `imaplib` | активно поддерживается, простая | объекты `MailMessage` с готовыми полями (subject, from, attachments, html/text), удобная работа с UID |
| `aioimaplib` | async | поддерживается умеренно | нативный async, но низкоуровневый (нужно самим парсить FETCH-ответы) |
| `aiosmtplib` | async SMTP | зрелая | удобная отправка с поддержкой STARTTLS, mTLS, креденшелов |

Главный вопрос: sync vs async для IMAP. Соображения:
- Один цикл синка для 500 ящиков идёт в отдельном worker-процессе, не в API.
- Время одного fetch для аккаунта — 1–5 секунд, в основном I/O wait.
- 500 ящиков × ~3 сек = 25 минут последовательно. С concurrency=10 это ~2.5 минуты — укладываемся в 5-минутный интервал.
- `imap-tools` зрелее и проще `aioimaplib`, у нее богаче API и меньше шансов наткнуться на edge-cases с Yandex/Mail.ru.

## Decision

- **IMAP**: `imap-tools` (sync). В worker'е запускается через `asyncio.to_thread` с ограничением `asyncio.Semaphore(MAX_CONCURRENT_IMAP=10)`. См. ADR-0013.
- **SMTP**: `aiosmtplib` (async) — отправка из FastAPI-handler'а в реальном времени. STARTTLS и SSL on-connect поддерживаются нативно.
- В worker'е после успешной отправки — appendsmessage в `Sent` через тот же `imap-tools` (sync) под semaphore.

Версии: `imap-tools >= 1.6`, `aiosmtplib >= 3.0`.

## Consequences

**Плюсы:**
- Удобный высокоуровневый API IMAP -> меньше багов с парсингом MIME.
- Async SMTP ничему не мешает (в отличие от async IMAP — там не получим выигрыша в нашей нагрузке).
- Чёткое разделение: web-процесс (async, отправка) ↔ worker (sync IMAP в thread pool).

**Минусы / риски:**
- Sync IMAP в thread pool — потенциальная проблема при сильно большом числе ящиков (>5000). Mitigation: cap по semaphore + горизонтальное масштабирование worker'ов в будущем.
- `imap-tools` хуже отлажен на нестандартных серверах (Exchange on-prem, экзотические домены) — для текущего scope (gmail/yandex/mail.ru/outlook) это не проблема.

## Alternatives considered

- **`aioimaplib` для IMAP**: реальный выигрыш в производительности минимален, а сложность кода растёт. Отклонено.
- **Полностью sync (smtplib для отправки)**: блокирует event loop в API; лучше использовать async-клиент.
- **`yagmail` / `email-validator` обёртки** — не закрывают IMAP-чтение.
