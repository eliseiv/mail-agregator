# ADR-0013: Конкурентность IMAP-сессий

- **Статус:** accepted
- **Дата:** 2026-05-05

## Context

`imap-tools` — синхронная библиотека (ADR-0002). В worker'е (asyncio loop) нужно конкурентно обрабатывать ~500 ящиков за 5 минут, не блокируя event loop и не открывая 500 одновременных IMAP-соединений.

## Decision

- Worker создаёт **`asyncio.Semaphore(MAX_CONCURRENT_IMAP)`**, конфигурируется через env (`MAX_CONCURRENT_IMAP`, default = **10**).
- Для каждого аккаунта создаётся `asyncio.Task`:
  ```text
  async with semaphore:
      result = await asyncio.to_thread(sync_account_blocking, account)
      await save_to_db(result)
  ```
- `asyncio.to_thread` использует встроенный default executor — `ThreadPoolExecutor`. Размер пула = `MAX_CONCURRENT_IMAP + 4` (запас для других blocking calls). Конфигурируется через env `WORKER_THREAD_POOL_SIZE`.
- `gather(*tasks, return_exceptions=True)` — собирает результаты; ошибки одного аккаунта не валят остальные (см. ADR-0008).
- Per-account timeout: 60 секунд на весь sync аккаунта (`asyncio.wait_for`). При timeout — `last_sync_error = "timeout_60s"`.
- IMAP-соединение не переиспользуется между циклами (открывается и закрывается каждый раз). Это упрощает state и совместимо с провайдерами, которые любят закрывать idle-соединения.

### Запас по производительности

- 500 ящиков × 3 сек среднее / 10 параллельных = **2.5 минуты** -> укладываемся в 5-минутный интервал с 2x запасом.
- Если в будущем `accounts > 1000` — поднять `MAX_CONCURRENT_IMAP` до 20–30 (постепенно, проверяя баны провайдеров).

## Consequences

**Плюсы:**
- Простая модель: семафор + thread pool, никаких сложных пулов соединений.
- Лимит сверху защищает от bursts и от банов провайдеров (Gmail чувствителен к большому числу одновременных IMAP-сессий с одного IP).

**Минусы:**
- Каждый цикл открывает 500 новых TCP-соединений; нагрузка на network/conntrack. Допустимо для нашего scope.
- ThreadPoolExecutor — ограничен GIL, но IMAP — I/O-bound, GIL отпускается на сетевых вызовах.

## Alternatives considered

- **Async IMAP (`aioimaplib`)**: см. ADR-0002 — отклонено в пользу зрелости `imap-tools`.
- **Несколько worker-процессов с разделением аккаунтов**: преждевременно; сначала проверим scope с одним worker.
- **Persistent IMAP connections (IDLE)**: 500 длинных соединений — riskier для банов; см. ADR-0008.
