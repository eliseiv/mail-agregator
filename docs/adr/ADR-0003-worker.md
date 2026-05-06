# ADR-0003: Background worker — APScheduler в отдельном контейнере

- **Статус:** accepted
- **Дата:** 2026-05-05

## Context

Нужны фоновые задачи:
1. **sync_cycle** — каждые 5 минут синхронизировать INBOX всех активных mail-аккаунтов.
2. **retention_cleanup** — раз в сутки (03:00 UTC) удалять `messages` и `attachments` старше 30 дней.

> **session_gc не реализуется.** Все сессии (`session:*`, `setup_session:*`, `force_sync:*`, `rl:*`) хранятся в Redis с TTL — Redis сам удаляет их по истечении. Дополнительный GC-job избыточен. Множества `user_sessions:{user_id}` могут содержать "висящие" tokenы тех сессий, которые уже истекли по TTL: это допустимо, потому что (а) проверка валидности сессии всегда идёт через `GET session:{token}` (HIT/MISS), (б) при `revoke_all_for_user` после `SMEMBERS` мы делаем `DEL session:{t}` для каждого — отсутствующие ключи безвредны, (в) сам `user_sessions:{user_id}` имеет TTL = `SESSION_ABSOLUTE_TTL_SECONDS` (7 дней) и протухает целиком.

Кандидаты:

| Решение | Плюсы | Минусы |
| --- | --- | --- |
| **APScheduler** (in-process scheduler, asyncio loop) | Простой; нет внешней очереди; код задач — обычные python-функции; cron+interval triggers | Нет очереди задач (важно — нам она и не нужна); один экземпляр worker (HA не нужен) |
| **ARQ** (Redis-backed task queue) | Распределённость, retry, scheduled jobs | Лишняя сложность: задач у нас 2-3 фиксированных по расписанию, а не event-driven |
| **Celery** | Полный фреймворк | Запрещён ТЗ; overkill |
| **Cron в контейнере + python-скрипт** | Минимум зависимостей | Хуже логирование, нет shared state, нет graceful shutdown |

Дополнительно: ТЗ требует *worker в отдельном контейнере*. Это важно, чтобы перезапуск API не убивал текущий sync, и наоборот.

## Decision

- Использовать **APScheduler 3.10+** (`AsyncIOScheduler`) внутри отдельного контейнера `worker`.
- Стартовый файл `worker/main.py` создаёт scheduler, регистрирует jobs:
  - `sync_cycle` — `IntervalTrigger(minutes=5)`, `coalesce=True`, `max_instances=1`, `misfire_grace_time=60`.
  - `retention_cleanup` — `CronTrigger(hour=3, minute=0)`, `max_instances=1`.
- Worker подключается к тем же PostgreSQL/Redis/MinIO, что и API.
- Для каждого `sync_cycle` worker логирует start/finish + статистику (`accounts_total`, `accounts_ok`, `accounts_failed`, `new_messages`).
- **Отказоустойчивость одного аккаунта не должна валить цикл**: каждая попытка синка обёрнута в `try/except`, ошибка пишется в `mail_accounts.last_sync_error` и в лог; цикл продолжается со следующим аккаунтом.
- Graceful shutdown по SIGTERM: scheduler.shutdown(wait=True) — текущие задачи завершаются.

## Consequences

**Плюсы:**
- Минимум зависимостей: scheduler — это библиотека, не сервис.
- Прозрачно для отладки: jobs — обычные `async def`, не нужно изучать Celery/ARQ DSL.
- Состояние scheduler'а in-memory; перезапуск worker'а — это просто перезапуск процесса. Misfire grace time покрывает короткие downtime.

**Минусы / риски:**
- Один worker = single point of failure. Mitigation: контейнер restart=always; rebuild ≤ 5 минут — допустимо для нашей SLO.
- При горизонтальном масштабе двух worker'ов появятся дубли задач. Mitigation: на текущий scope не нужен; если потребуется — переход на ARQ (новый ADR).

## Alternatives considered

- **ARQ**: рассматривался активно. Отклонён, т.к. event-driven очередь оверкилл — задачи строго по расписанию.
- **APScheduler внутри API-процесса**: отказ — рестарт API убивает синк, нарушение требования "worker в отдельном контейнере".
- **Cron + python**: хуже логирование/наблюдаемость, нет общего event loop.

## Revisions

- **2026-05-05 (rev. 2):** session_gc явно исключён из scope worker'а — полагаемся на Redis TTL. Перечислены ключи и обоснование, почему GC не требуется.
