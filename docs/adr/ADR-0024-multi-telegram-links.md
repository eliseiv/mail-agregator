# ADR-0024 — Несколько Telegram-привязок на один аккаунт системы

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-05-27 |
| Расширяет | [ADR-0022](./ADR-0022-telegram-sso-and-notifications.md) §1 (telegram_links) и §2 (push-нотификации). ADR-0022 остаётся `accepted`; настоящий ADR снимает инвариант «один user — один TG» и пересматривает ключ идемпотентности доставки. |
| Спринт | A (независим от ADR-0025 / Outlook OAuth2) |

## Context

ADR-0022 §1.1 зафиксировал инвариант **«один internal user — максимум один Telegram»** через `UNIQUE(telegram_links.user_id)`. PK таблицы — `telegram_user_id`. SSO-резолв пользователя по `telegram_user_id` однозначен. `sso_service.link_pending` делал pre-check: при попытке привязать второй TG к тому же user писал audit `telegram_link_collision` и **молча не привязывал**. Recipient-SQL (`telegram_notifications.list_recipients_for_message`) джойнит `telegram_links` и доставлял **одному** чату; идемпотентность — `UNIQUE(message_id, user_id)`.

Новое требование пользователя: разрешить **несколько активных TG-привязок** на один `user_id` (личный, рабочий и т.д.). Бот шлёт уведомление **во ВСЕ** привязанные живые чаты. Лимит — мягкий потолок (default 10, env `TG_MAX_LINKS_PER_USER`).

Ключевое наблюдение об однозначности направления связи:
- `telegram_user_id` (PK) → один `user_id`: остаётся однозначным. Один TG-аккаунт по-прежнему принадлежит ровно одному internal user. SSO-резолв не меняется.
- `user_id` → много `telegram_user_id`: становится отношением **1:N**. Снимаем `UNIQUE(user_id)`.

## Decision

### 1. Схема — снять `UNIQUE(user_id)` с `telegram_links`

`telegram_links` остаётся: PK `telegram_user_id`, FK `user_id`, `created_at`, `dead_at`. Меняется только индекс/констрейнт:

- Убрать `UNIQUE` с `user_id` (был объявлен на уровне столбца в модели).
- Сохранить **неуникальный** индекс `telegram_links_user_id_idx` на `(user_id)` (он уже есть как `Index`, нужно убедиться, что не дублируется с unique).
- PK `telegram_user_id` без изменений → атомарный `INSERT … ON CONFLICT (telegram_user_id) DO UPDATE` для перепривязки одного TG к другому user сохраняется.

Лимит `TG_MAX_LINKS_PER_USER` — **мягкий, прикладной** (не DB-констрейнт): проверяется `COUNT(*)` живых линков в `link_pending` перед upsert. Обоснование: жёсткий DB-констрейнт на счётчик строк потребовал бы триггер/exclusion-constraint — избыточно для потолка-защиты «от абьюза» на масштабе ≤5 пользователей. При достижении лимита — audit `telegram_link_limit_reached`, привязка не создаётся, пользователю показывается ошибка.

### 2. Репозиторий `TelegramLinksRepo`

| Метод | Было | Стало |
| --- | --- | --- |
| `get_by_telegram_user_id` | по PK | без изменений |
| `get_active_by_telegram_user_id` | по PK + `dead_at IS NULL` | без изменений (SSO-резолв) |
| `get_by_user_id` → один | `scalar_one_or_none` | **переименовать в `list_by_user_id`** → `list[TelegramLink]` (все линки user'а) |
| — | — | **добавить `list_active_by_user_id(user_id)`** → `list[TelegramLink]` где `dead_at IS NULL` (для UI «мои привязки» + подсчёт лимита) |
| `upsert` | ON CONFLICT (telegram_user_id) | без изменений (механика та же; collision-логика по `user_id` уходит из вызывающего слоя) |
| `delete_by_user_id` | удалял единственный линк | **переименовать в `delete_all_by_user_id(user_id)`** → `int` (кол-во удалённых); вернуть список удалённых `telegram_user_id` для audit |
| — | — | **добавить `delete_one(user_id, telegram_user_id)`** → `bool` — отвязать КОНКРЕТНЫЙ TG. WHERE по обоим полям, чтобы user не мог отвязать чужой TG. |
| `mark_dead(telegram_user_id)` | по PK | без изменений — **подтверждено**: dead помечается per `telegram_user_id`, не трогает остальные линки того же user'а. |
| `mark_alive(telegram_user_id)` | по PK | без изменений |

### 3. SSO / привязка второго TG

- **SSO login** (`/api/telegram/auth`): не меняется. `get_active_by_telegram_user_id(telegram_user_id)` по-прежнему даёт ≤1 строку (PK), резолв `user_id` однозначен. Несколько TG → один user; обратное (TG→user) — однозначно.
- **`link_pending`**: убрать collision-логику. Новая логика:
  1. Если линк с этим `telegram_user_id` уже существует и указывает на **другого** user'а — это перепривязка TG-аккаунта (upsert ON CONFLICT перенесёт его на текущего user'а; пишем audit `telegram_link_rebound`). Это поведение ADR-0022 сохраняется.
  2. Если линк существует и указывает на **текущего** user'а — no-op refresh (audit `telegram_link_created` с `replaced=true`).
  3. Если линка нет — проверить `COUNT(active) < TG_MAX_LINKS_PER_USER`. Если лимит достигнут → audit `telegram_link_limit_reached`, **не привязывать**, поднять `TelegramLinkLimitError` (router → 409 `tg_link_limit`). Иначе upsert + audit `telegram_link_created`.
- **Действие `telegram_link_collision`** в audit — **deprecated** (больше не пишется; оставляем в перечислении действий как исторический для старых записей).

### 4. UX добавления / отвязки (frontend + API)

Привязка второго TG: пользователь уже залогинен в аккаунт системы (пароль/сессия), открывает страницу настроек, видит список привязанных TG и кнопку «Добавить ещё». Технически это **тот же** `tg-auth flow** через WebApp бот: пользователь открывает бот в нужном Telegram-аккаунте (личный/рабочий), WebApp шлёт `POST /api/telegram/auth` с `init_data` этого TG. Поскольку у этого `telegram_user_id` ещё нет линка, обычный flow ведёт через pending-cookie → `/login`. Чтобы привязать к **уже залогиненному** аккаунту без повторного ввода пароля:

- Новый endpoint **`POST /api/telegram/links`** (cookie-authenticated, CSRF-protected): принимает `init_data` свежего TG, HMAC-валидирует, и привязывает `telegram_user_id` к `request.state.session.user_id` (НЕ через pending-flow). Применяет лимит §3. Это «добавление при активной сессии» — отличается от `/api/telegram/auth`, который обслуживает «вход без сессии».
- **`GET /api/telegram/links`** (cookie-auth): список активных привязок текущего user'а — `[{telegram_user_id, created_at, dead_at}]` для UI.
- **`DELETE /api/telegram/links/{telegram_user_id}`** (cookie-auth, CSRF): отвязать конкретный TG (вызывает `delete_one`), audit `telegram_link_revoked` c `reason="user_unlink"`.

Контракты — в `docs/04-api-contracts.md` §4b.

### 5. Logout / reset-password / delete-user — пересмотр семантики revoke

> **round-43 (пересмотр, синхронно с ADR-0022 §1.5).** Прежнее правило §5 «logout сбрасывает ВСЕ линки» **ОТМЕНЕНО**. Эмпирически на проде это вызывало «само-разлогинивание» push: форма «Выйти» сабмитилась **фантомно** (устаревшая вкладка / реактивация Telegram WebApp при заходе с ещё-живой по cookie сессией), удаляя ВСЕ `telegram_links` без действия пользователя → цикл `create→logout→create` против round-38 self-heal. **Logout теперь завершает только веб-сессию и НЕ трогает `telegram_links`.** Отвязка TG — только явной кнопкой «Отвязать» (`DELETE /api/telegram/links/{id}` → `revoke_one`, §4). Полный разбор, edge-кейсы и оценка безопасности — в **ADR-0022 §1.5 «round-43»**. Реализация — удаление вызова `revoke_for_user(reason="logout")` из `backend/app/auth/router.py::logout`; миграций нет.

ADR-0022 §1.5 (исходно): logout/reset-password сбрасывали единственный линк. С multi-TG и round-43:

- **logout** (`POST /logout`): ~~по умолчанию **сбрасывает ВСЕ** линки user'а (`delete_all_by_user_id`)~~ **(round-43 — ОТМЕНЕНО)**. Теперь logout **НЕ удаляет** привязки — завершает только веб-сессию (revoke session + clear cookies + 302 `/login`). Вызов `revoke_for_user(reason="logout")` из endpoint'а **удалён**; audit `telegram_link_revoked` с `reason="logout"` больше **не** пишется. Push не требует активной веб-сессии — привязка самодостаточна. Прекратить push можно явной кнопкой «Отвязать» (§4, `revoke_one`/`user_unlink`) или admin-reset (см. ниже). Обоснование «выйти из веб ≠ перестать получать push» — два разных намерения; разделены. (Q-MTG-1 — **закрыт** этим решением: см. Open questions.)
- **отвязка пользователем (round-43 — единственный пользовательский путь)** (`DELETE /api/telegram/links/{tg_user_id}`): `revoke_one` → `delete_one(user_id, telegram_user_id)` — рвёт **только** указанный TG (WHERE по обоим полям, нельзя отвязать чужой). Audit `telegram_link_revoked` с `reason="user_unlink"`. Требует **активной веб-сессии** (cookie-auth + CSRF) — пользователь сперва логинится, что доказывает контроль над аккаунтом.
- **reset_password** (admin): **сбрасывает ВСЕ** линки (новый владелец / компрометация) — **без изменений** семантики, метод `delete_all_by_user_id`, `reason="password_reset"`. Это сохранённый **путь принудительного отзыва** всех привязок.
- **link_user_missing** (`POST /api/telegram/auth` resolve в удалённого user'а): `revoke_for_user(reason="link_user_missing")` — чистка сирота-привязки — **без изменений**.
- **delete user**: каскад `ON DELETE CASCADE` удаляет все линки — без изменений.

`revoke_for_user` остаётся (внутри зовёт `delete_all_by_user_id`) и используется `reset_password` + `link_user_missing`; **удаляется только его вызов из logout**. Audit пишет одну агрегированную запись `telegram_link_revoked` с массивом `details.telegram_user_ids` (для `reset_password`/`link_user_missing`); `revoke_one` (явная отвязка) пишет запись с одиночным `details.telegram_user_id` + `reason="user_unlink"`.

### 6. `telegram_notifications` — КЛЮЧЕВОЕ изменение ключа идемпотентности

Сейчас идемпотентность — `UNIQUE(message_id, user_id)`. При нескольких чатах на одного user это **баг**: первый чат заберёт `try_reserve`, остальные получат `None` и не получат уведомление.

**Решение:** добавить колонку `telegram_user_id BIGINT NOT NULL` и сменить уникальность на **`UNIQUE(message_id, telegram_user_id)`**.

- `telegram_user_id` — конкретный чат, в который доставлено (FK не ставим на `telegram_links.telegram_user_id`, т.к. линк может быть удалён/перепривязан, а реестр доставок должен переживать это; держим как «слепок chat_id на момент доставки», как `telegram_message_id`). 
- `user_id` остаётся (получатель-владелец; полезен для аудита «что доставлено user X» и для recovery-JOIN). 
- Старый `UNIQUE(message_id, user_id)` (`telegram_notifications_unique`) — **снять**.
- Индексы: добавить `UNIQUE(message_id, telegram_user_id)`; оставить `message_id_idx`; `user_id_idx` оставить.

### 7. Влияние на dispatch / recovery / throttle

- **Recipient-SQL** (`list_recipients_for_message`): JOIN `telegram_links tl ON tl.user_id = u.id AND tl.dead_at IS NULL` теперь даёт **по строке на каждый живой TG** пользователя (раньше ≤1). `SELECT DISTINCT u.id, tl.telegram_user_id, ma.id` — уже возвращает `telegram_user_id`, так что структура `NotifyRecipient(user_id, telegram_user_id, mail_account_id)` **не меняется**, просто строк больше. dispatch уже итерирует по recipients → каждому чату отправит.
- **`try_reserve`**: сменить сигнатуру с `(message_id, user_id)` на `(message_id, user_id, telegram_user_id)`; ON CONFLICT по `(message_id, telegram_user_id)`. INSERT пишет все три колонки.
- **`_dispatch_one_recipient`**: вызывает `try_reserve(message_id, recipient.user_id, recipient.telegram_user_id)`.
- **`mark_dead` при ошибке отправки в ОДИН чат** — помечает `dead_at` только у этого `telegram_user_id`, **не убивает** остальные привязки того же user'а. **Подтверждено** (mark_dead уже по `telegram_user_id`). Recipient-SQL отфильтрует мёртвый линк → остальные чаты продолжат получать.
- **recovery_scan** (`list_missing_for_recovery`): per-recipient `NOT EXISTS` теперь должен быть per-`(message_id, telegram_user_id)`, а не per-`(message_id, user_id)`. JOIN `telegram_links` уже даёт строку на каждый TG; меняем `NOT EXISTS (tn WHERE tn.message_id=m.id AND tn.user_id=u.id)` → `… AND tn.telegram_user_id = tl.telegram_user_id`. Это сохраняет round-33 per-recipient гарантию, но теперь на гранулярности per-chat (round-35).
- **Throttle** (round-32, `rl:tg_send:<chat_id>`): уже per-chat = per `telegram_user_id`. **Без изменений** — ключ `str(recipient.telegram_user_id)`.

### 8. Миграция `20260527_017_multi_telegram_links`

`up`:
1. `ALTER TABLE telegram_links DROP CONSTRAINT IF EXISTS <uq_user_id>` (имя автогенерированное column-level UNIQUE — определить фактическое через `\d telegram_links`; в Alembic — `op.drop_constraint`). Оставить/создать неуникальный `telegram_links_user_id_idx` если он был поглощён unique-индексом.
2. `ALTER TABLE telegram_notifications ADD COLUMN telegram_user_id BIGINT` (добавляется как nullable, чтобы заполнить backfill'ом до перевода в NOT NULL на шаге 5).
3. **Backfill**: `UPDATE telegram_notifications tn SET telegram_user_id = tl.telegram_user_id FROM telegram_links tl WHERE tl.user_id = tn.user_id` — на момент миграции инвариант 1:1 ещё держится (один линк на user), поэтому backfill однозначен. Строки, у которых линк уже удалён (`tl` нет) — `telegram_user_id` останется NULL.
4. Удалить осиротевшие строки без линка ИЛИ оставить с синтетическим значением. **Решение:** строки с NULL после backfill — это исторические доставки удалённым/перепривязанным линкам; они уже доставлены (`sent_at` стоит) и нужны только как след. Поставить `telegram_user_id = 0` (зарезервированный «unknown legacy chat») чтобы выполнить NOT NULL. (Q-MTG-2 — допустимо ли терять точный chat_id истории; на масштабе и сроке retention 30 дней эти строки всё равно вычистятся каскадом.)
5. `ALTER COLUMN telegram_user_id SET NOT NULL`.
6. `ALTER TABLE telegram_notifications DROP CONSTRAINT telegram_notifications_unique` (старый `(message_id,user_id)`).
7. `CREATE UNIQUE INDEX telegram_notifications_msg_chat_uq ON telegram_notifications (message_id, telegram_user_id)`.

`down`: обратные шаги (восстановить `(message_id,user_id)` UNIQUE — потребует дедупа, т.к. multi-chat строки конфликтуют; в `down` допустимо `DELETE` дубликатов оставляя min(id) — задокументировать как lossy downgrade).

## Consequences

**Плюсы:**
- Пользователь получает уведомления во все свои TG (личный + рабочий).
- Гранулярность доставки и recovery — per-chat; mark_dead изолирован per-chat.
- SSO-резолв не усложняется (направление TG→user остаётся 1:1).

**Минусы / риски:**
- Изменение ключа идемпотентности `telegram_notifications` — миграция с backfill; downgrade lossy.
- Объём `telegram_notifications` растёт пропорционально среднему числу линков на user (≤10×). На масштабе ≤5 users остаётся в десятках тысяч строк — приемлемо.
- Recipient-SQL отдаёт больше строк → больше Bot API вызовов; throttle per-chat и глобальный TD-026/TD-027 уже это покрывают.

**Tech debt:**
- **TD-028** — `telegram_user_id=0` для legacy-строк `telegram_notifications` (синтетический chat). Самоустраняется retention-каскадом за 30 дней.

## Alternatives considered

1. **Отдельная таблица `telegram_chats` (1:N) + `telegram_links` как было.** Чище нормализационно, но `telegram_links` уже и есть «привязка TG↔user»; добавление второй таблицы дублирует сущность. Отклонено в пользу снятия UNIQUE.
2. **Идемпотентность остаётся `(message_id, user_id)`, а в одно сообщение склеиваем рассылку во все чаты атомарно.** Не работает с per-chat throttle/retry/mark_dead — один мёртвый чат заблокировал бы reserve для остальных. Отклонено.
3. **Жёсткий DB-лимит на число линков (триггер).** Избыточно для мягкого потолка-«анти-абьюз». Отклонено в пользу прикладной проверки `COUNT(*)`.

## Open questions

- **Q-MTG-1** — ~~logout сбрасывает ВСЕ привязки или только текущую?~~ **ЗАКРЫТ (round-43, 2026-06-11).** Решение пользователя по итогам прод-инцидента «само-разлогинивание push»: **logout НЕ сбрасывает привязки вовсе** — расцеплён с Telegram-привязкой (§5 round-43, ADR-0022 §1.5 «round-43»). Отвязка — только явной кнопкой «Отвязать» (`DELETE /api/telegram/links/{id}`). Прежний дефолт «все» отменён.
- **Q-MTG-2** — допустимо ли при миграции потерять точный `chat_id` у исторических `telegram_notifications` без живого линка (ставим `0`)? Эти строки уже доставлены и вычистятся retention за 30 дней.
