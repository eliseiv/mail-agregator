# ADR-0008: Стратегия инкрементальной IMAP-синхронизации

- **Статус:** accepted
- **Дата:** 2026-05-05

## Context

Сервис должен раз в 5 минут притягивать новые письма из INBOX каждого аккаунта без избыточного трафика и без дубликатов. IMAP предоставляет несколько механизмов:

- **UID + UIDNEXT** (RFC 3501): у каждого письма стабильный UID; UIDNEXT — гарантированно больший, чем у любого существующего. Стандартный и универсально поддерживаемый.
- **CONDSTORE / MODSEQ** (RFC 4551): инкрементальная отдача изменённых флагов; не нужен для текущего scope.
- **IMAP IDLE** (RFC 2177): server push; полезно, но требует длинных соединений по 500 ящикам — нерационально.

Также при первом подключении нужно решить, сколько истории забирать.

## Decision

### Initial sync (первое подключение аккаунта)

1. При добавлении аккаунта (`POST /api/mail-accounts`) backend выполняет тестовый IMAP+SMTP login и при успехе записывает строку с `last_synced_uidnext IS NULL` и `last_uidvalidity IS NULL`.
2. В первом цикле worker'а для аккаунта без `last_synced_uidnext`:
   - Подключиться к INBOX (READ-ONLY).
   - Запросить UIDs всех писем за последние 30 дней: `UID SEARCH SINCE <date(now-30d)>`.
   - Для каждого UID — fetch (envelope, body, attachments). Сохранить в БД.
   - В конце цикла записать `last_synced_uidnext = max(uid)+1` (либо `UIDNEXT` сервера, если он отдан) и `last_uidvalidity = current UIDVALIDITY`.

### Incremental sync (последующие циклы)

Семантика: `last_synced_uidnext` — это значение `UIDNEXT`, зафиксированное в конце предыдущего успешного цикла (т.е. `max(сохранённый UID) + 1`). Все новые письма имеют `UID >= last_synced_uidnext`.

1. Подключиться к INBOX (READ-ONLY).
2. Проверить `UIDVALIDITY` (см. ниже).
3. Запросить UIDs новых писем: `UID SEARCH UID {last_synced_uidnext}:*`. Это даёт `[]`, если новых нет, либо список `UID >= last_synced_uidnext`. (Защитный фильтр в коде: `[u for u in uids if u >= last_synced_uidnext]` — IMAP-сервер при пустом интервале может вернуть последнее письмо.)
4. Если список пуст — обновить только `last_synced_at = now()`, выход.
5. Иначе для каждого UID (батчами по 50) — fetch (envelope, body, attachments) и сохранение.
   - Body берётся из `BODYSTRUCTURE`-aware fetcher; объекты > 25 MiB по attachment не загружаются (`skipped_too_large=true`); если text/plain или text/html отсутствует — `body_present=false`; объём text > 1 MiB — обрезается, `body_truncated=true` (см. ADR-0012).
6. Сохранение — `INSERT ... ON CONFLICT (mail_account_id, uidvalidity, uid) DO NOTHING` (идемпотентность).
7. По завершении: `last_synced_uidnext = (UIDNEXT сервера) OR (max(uid) + 1)`, `last_uidvalidity = current UIDVALIDITY`, `last_synced_at = now()`, `last_sync_error = NULL`, `consecutive_failures = 0`.

### UIDVALIDITY change

- При каждом подключении проверяется `UIDVALIDITY`. Если он отличается от сохранённого `last_uidvalidity` — это полная переиндексация ящика провайдером (редкий, но валидный случай).
- Действие: внутри текущего же цикла обнулить `last_synced_uidnext` в памяти и пройти ветку initial sync (30-дневное окно). По завершении сохранить `last_synced_uidnext` и обновлённый `last_uidvalidity` в БД одним апдейтом. Дополнительные колонки (`requires_resync`, `uid_invalidated_at`) не используются — состояние полностью определяется парой (`last_synced_uidnext`, `last_uidvalidity`).
- Существующие сообщения в БД с прежним `uidvalidity` сохраняются и продолжают показываться пользователю; они будут естественным образом удалены по retention (30 дней). Новые письма с новой UIDVALIDITY вставляются как обычно — UNIQUE (`mail_account_id`, `uidvalidity`, `uid`) гарантирует отсутствие коллизий.

### Обработка ошибок

- IMAP login fail (auth) — `last_sync_error = "auth_failed: <reason>"`, `is_active = false` (требует повторной валидации пользователем). User увидит error-badge у аккаунта в UI.
- Сетевые ошибки — `last_sync_error = "network: <details>"`, `is_active = true` (попробуем в следующем цикле).
- 3 подряд цикла fail — `is_active = false`, отправляем notification в audit log (для админа).

### Идемпотентность

- Уникальный составной индекс в БД: `messages (mail_account_id, uid, uidvalidity)`. Повторная вставка одного и того же UID — отбрасывается на уровне БД (ON CONFLICT DO NOTHING).

## Consequences

**Плюсы:**
- UIDNEXT-based — простой и надёжный паттерн, поддерживается всеми провайдерами.
- 30-дневное окно при первом подключении — минимизирует первый "холодный" fetch.
- Идемпотентность гарантирована БД-индексом.

**Минусы:**
- Не отслеживаем изменения флагов (read/unread, deleted) — для нашего scope не критично; ТЗ требует только показа новых сообщений и отметки прочитанным локально.
- Если UIDVALIDITY меняется часто — повторный 30-дневный backfill. На практике это редкое событие.

## Alternatives considered

- **Полный fetch каждого ящика**: трафик и нагрузка кратно выше; ТЗ требует именно incremental.
- **IMAP IDLE + persistent connections**: 500 длинных TCP-сессий — высокая нагрузка на коннект-пулы провайдеров и потенциальные баны. Отклонено для текущего scope.
- **MODSEQ/CONDSTORE**: добавит сложность, не даёт значимого выигрыша при текущей частоте 5 минут.

## Revisions

- **2026-05-05 (rev. 2):** убраны упоминания несуществующих колонок `mail_accounts.connected_at`, `mail_accounts.requires_resync`, `messages.uid_invalidated_at` — состояние синхронизации полностью определяется парой (`last_synced_uidnext`, `last_uidvalidity`). Уточнена семантика `last_synced_uidnext` (= `UIDNEXT` после успешного цикла; новые письма имеют `UID >= last_synced_uidnext`). Зафиксирован IMAP-запрос: `UID {last_synced_uidnext}:*` с защитным фильтром по `UID >= last_synced_uidnext`.
