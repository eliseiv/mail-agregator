# ADR-0034 — Переадресация входящих писем команды на почту лидера

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-07-03 |
| Связь с другими ADR | **Форкает паттерн** [ADR-0023](./ADR-0023-outbound-webhooks.md) (per-group конфиг «одна запись на команду» + Redis-очередь + APScheduler-диспатчер + DB-дедуп через UNIQUE-claim). **Видимость/ACL получателя-конфигуратора** — по модели членств [ADR-0030](./ADR-0030-multi-group-membership.md) (`user_groups`) и visibility-scope [ADR-0019](./ADR-0019-groups-and-roles.md) §7. **Dispatch-паттерн fire-and-forget** — как [ADR-0033](./ADR-0033-mailbox-down-telegram-alert.md) (очередь + диспатчер, без recovery-scan). **Переиспользует** SMTP-ядро отправки (`send/service.py`, ADR-0002/ADR-0025 XOAUTH2), шифрование кредов (ADR-0005), storage вложений (ADR-0007), no-JS fallback (ADR-0015), CSRF (ADR-0010). UI встраивается в существующую страницу «Интеграции» (ADR-0023 §2.7). |

---

## Context

Продукт-запрос: **лидер команды хочет получать копии всех входящих писем своей команды на собственный e-mail**. Сценарий:

1. На странице **«Интеграции»** (`/my/integrations`, уже существует — ADR-0023) лидер добавляет **один** e-mail для переадресации своей команды (CRUD, ровно одна запись на команду).
2. Сервис при получении **нового** письма **любым** ящиком команды пересылает его целиком на этот адрес.

Фичи в коде нет (`forward` в кодовой базе — только `X-Forwarded-For` в middleware). Ближайший полный образец per-group интеграции — **webhooks** ([ADR-0023](./ADR-0023-outbound-webhooks.md)); паттерн фоновой доставки «Redis-очередь + APScheduler-диспетчер + DB-дедуп» доказан на webhook / tg_notify / mailbox_alert. Этот ADR форкает готовые паттерны, добавляя **новый домен** (свои таблицы, своя очередь, свой диспатчер) поверх неизменного `worker.sync_cycle`.

### Ключевые продуктовые ограничения (приняты пользователем как дефолт)

- **Отправитель форварда** = ящик команды, получивший письмо (его SMTP-креды). `From` = адрес этого ящика, `To` = адрес лидера. В теле — блок «пересланное сообщение» с оригинальными From/Date/To/Subject. Служебного (system) SMTP у сервиса **нет** — отправлять больше нечем.
- **Содержимое** = целиком: `body_text` + `body_html` + вложения из MinIO. Вложения с `skipped_too_large=true` и превышение суммарного лимита (~25 МБ) — **пропускать** с пометкой в теле.
- **Область** = только **НОВЫЕ** входящие после включения; только ящики команды (`mail_accounts.group_id` = команда лидера); персональные ящики (`group_id IS NULL`) **не** пересылаются.

---

## Decision

### 1. Модель данных — две новые таблицы (миграция `20260703_021`)

#### 1.1. `group_forwarding` — конфигурация (1 row на команду; форк `webhooks`)

```sql
CREATE TABLE group_forwarding (
    id          BIGSERIAL PRIMARY KEY,
    group_id    BIGINT NOT NULL UNIQUE
                REFERENCES groups(id) ON DELETE CASCADE,
    forward_to  TEXT NOT NULL,                       -- e-mail лидера, 3..254
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- UNIQUE(group_id) служит индексом lookup при dispatch.
-- Триггер BEFORE UPDATE ON group_forwarding → NEW.updated_at = now() (общий паттерн).
```

- **Одна запись на команду** (`UNIQUE(group_id)`). Нет secret'а (в отличие от `webhooks`) — переадресация не требует auth к внешнему receiver'у; отправка идёт через SMTP самого ящика.
- `forward_to` хранится **plaintext** — это e-mail-адрес назначения, не секрет.
- **Каскад:** `DELETE FROM groups` → каскадно удаляет `group_forwarding` (и `message_forwards` этой группы, см. ниже).
- `created_at` — anchor фильтра «не флудим историей» (§3.4): пересылаются только письма с `internal_date >= created_at`.
- **Объём:** ≤ 5 команд × 1 = ≤ 5 строк.

#### 1.2. `message_forwards` — реестр/claim доставок (дедуп; форк `webhook_deliveries`)

```sql
CREATE TABLE message_forwards (
    id          BIGSERIAL PRIMARY KEY,
    message_id  BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    group_id    BIGINT NOT NULL REFERENCES groups(id)   ON DELETE CASCADE,
    forward_to  TEXT NOT NULL,                       -- снимок адреса на момент отправки (audit)
    sent_at     TIMESTAMPTZ NULL,                    -- NULL = claim' nut, отправка не завершена
    error       TEXT NULL,                           -- усечён до 500 байт; без хостовых деталей
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT message_forwards_unique UNIQUE (message_id, group_id)
);
CREATE INDEX message_forwards_message_id_idx ON message_forwards(message_id);
CREATE INDEX message_forwards_group_id_idx   ON message_forwards(group_id);
```

- **Дедуп-семантика (exactly-once claim ДО отправки).** Диспатчер перед сборкой/отправкой форварда делает
  `INSERT INTO message_forwards (message_id, group_id, forward_to) VALUES (:mid,:gid,:to) ON CONFLICT (message_id, group_id) DO NOTHING RETURNING id`.
  Пустой `RETURNING` → письмо для этой команды уже обработано → **skip**. Это гарантирует ровно одну пересылку даже при дубле в очереди / повторном enqueue / рестарте worker (симметрично `webhook_deliveries.try_reserve`).
- **Исход попытки** пишется в ту же строку: успех → `sent_at = now()`; ошибка SMTP → `error = <усечённый текст>` (строка **остаётся**, `sent_at` = NULL). Строка с проставленным `error` **не** ретраится (нет recovery-scan — см. §3.6, Alternatives 5). Это осознанный at-most-once после claim: операционный сигнал важнее гарантии, письмо всегда доступно в UI.
- **Каскады:** `DELETE FROM messages` (retention 30 д, ADR-0011) → каскадно чистит `message_forwards`. `DELETE FROM groups` → каскадно чистит (FK `group_id`).
- **Объём:** ≤ 5 команд × ≤ 100 ящиков × ~поток писем/день × 30 д retention. С CASCADE от `messages` — авточистка.

> **Почему таблица, а не boolean-флаг на `messages`** (Alternatives 3): переадресация — per-команда (`(message_id, group_id)`), а один ящик может при переносе (ADR-0031) сменить команду; флаг `messages.forwarded` не различал бы команды и потребовал бы миграции колонки. Отдельная таблица повторяет доказанную дедуп-модель `webhook_deliveries` и даёт audit-след (`forward_to`, `error`, `sent_at`).

### 2. API — CRUD конфигурации

Endpoint-prefix `/api/forwarding/me`. Доступ: `group_leader` (своя команда по `scope.group_id`) **ИЛИ** `super_admin` (обязателен `?group_id=<id>`). `group_member` → `403 forbidden`. ACL — **копия** `WebhooksService._resolve_target_group_id` (`webhooks/service.py`): member → 403; super_admin без `?group_id` → 400; super_admin с `?group_id` → эта группа; leader с `?group_id` → 400 (запрещён). Все state-changing endpoints — под CSRF (ADR-0010).

Поскольку запись **одна на команду**, используется идемпотентный **`PUT` (upsert)** вместо `POST`(create, 409)+`PATCH` — проще и без 409-конфликта (Alternatives 6).

| Метод / путь | Назначение | Успех | Ошибки |
| --- | --- | --- | --- |
| `GET /api/forwarding/me` | Прочитать конфиг команды | `200 {id, group_id, forward_to, is_active, created_at, updated_at}` | `404 not_found` (не настроен); `403 forbidden` |
| `PUT /api/forwarding/me` | Создать/обновить (upsert) | `200` (обновлён) / `201` (создан) — тело как в GET | `400 validation_error` (невалидный e-mail / отсутствует `forward_to`); `403 forbidden` |
| `DELETE /api/forwarding/me` | Удалить конфиг | `204` | `404 not_found`; `403 forbidden` |

- **Запрос PUT** (JSON): `{forward_to: str, is_active?: bool}` (`is_active` default `true` при создании; при обновлении — оставляет прежнее, если не передан).
- **Валидация `forward_to`** — тот же **ручной паттерн**, что в `backend/app/accounts/schemas.py` (наличие ровно одного `@`, домен с точкой, без `..`, `min_length=3, max_length=254`). `EmailStr`/pydantic-email в проекте **не** используется — единообразие важнее.
- **Audit** (`AuditWriter`): `forwarding_updated` (PUT create/update, `details={group_id, forward_to, is_active}`), `forwarding_deleted` (DELETE, `details={group_id}`). Добавляются в `ALLOWED_ACTIONS`.
- **Rate-limit** per-group (`rate_limit.py`, как webhooks): `PUT` 30/час per `group_id`, `DELETE` 10/час per `group_id`.

#### 2.1. Form-encoded fallback (no-JS, ADR-0015)

Секция «Переадресация» на `/my/integrations` работает без JS. `PUT`/`DELETE` недоступны в HTML-форме → используется `_method`-override middleware:

| Целевой метод | Form-fallback |
| --- | --- |
| `PUT /api/forwarding/me` | `POST /api/forwarding/me` + form-поле `_method=PUT` |
| `DELETE /api/forwarding/me` | `POST /api/forwarding/me/delete` + form-поле `_method=DELETE` |

Оба пути — **exact** (без `\d+`-параметра) → добавляются в `_OVERRIDE_EXACT_PATHS` в `backend/app/middlewares.py` (было 5 → станет **7**). Регекс-список `_OVERRIDE_REGEX_PATHS` (16) **не меняется**. Тест `tests/unit/test_method_override.py::TestRegexCount::test_exact_paths_present` (`assert len(_OVERRIDE_EXACT_PATHS) == 5`) обновляется на **7** — это отмечено для backend-агента. Redirect-цели form-success ведут на `/my/integrations` с flash.

### 3. Пайплайн пересылки (worker, форк webhook-dispatch)

#### 3.1. Producer — `ForwardDispatchService.enqueue_message_ids(ids)`

`backend/app/forwarding/dispatch_service.py`. Делает один batched `redis.lpush(FORWARD_DISPATCH_QUEUE_KEY, *payloads)`, `_QueuePayload{v:1, message_id, source}` (форк `webhooks/dispatch_service.py`). Опционально pre-filter (отбросить ids, у чьей группы нет активного `group_forwarding`) — финальная проверка всё равно в консюмере.

#### 3.2. Хук в `worker.sync_cycle`

После COMMIT транзакции новых писем (рядом с tg/webhook enqueue, `worker/app/sync_cycle.py`), в **отдельном** `try/except` (Redis-сбой не роняет sync и не влияет на TG/webhook):

- Аккумулятор `forward_ids: list[int]` собирается в цикле `save_message` — **новые вставленные** `message_id` (не `notify_ids`/tagged: переадресация не зависит от тегов, шлём **все** новые входящие). Фильтр на enqueue-side: **только ящики с `group_id IS NOT NULL`** (персональные не пересылаются).
- **Loop-guard (часть 1) на enqueue-side.** Письмо **не** добавляется в `forward_ids`, если его исходные IMAP-заголовки уже несут `X-Forwarded-By: mail-aggregator` (доступны в live-объекте `imap_tools` в `save_message` — **без** новой колонки в БД). Это разрывает возможную петлю (форвард, попавший в другой наш ящик, не пересылается повторно).
- В конце `sync_one_account` — один `ForwardDispatchService.enqueue_message_ids(forward_ids)`.

> Так как enqueue происходит **только** для писем, вставляемых текущим циклом (нет backfill/recovery для forward), исторические письма, уже лежавшие в БД до включения переадресации, **никогда** не попадают в очередь — «только новые после включения» выполняется естественно; дополнительный temporal-guard §3.4 закрывает edge с initial-backfill нового ящика.

#### 3.3. Consumer — job `forward_dispatch` (`worker/app/forward_dispatch.py`)

Форк `worker/app/webhook_dispatch.py`. По интервалу `FORWARD_DISPATCH_INTERVAL_SECONDS` (default 5), `max_instances=1, coalesce=True`, под фиче-флагом `FORWARDING_ENABLED`:

```text
1. items = redis.lpop(FORWARD_DISPATCH_QUEUE_KEY, count=FORWARD_BATCH_SIZE)   # default 30
2. if not items: return
3. for raw in items: await dispatch_one(message_id)  # каждый в своём try/except — сбой не роняет цикл
4. log forward_dispatch_finish {sent, skipped_dedup, skipped_no_config, skipped_loop, skipped_history, errors}
```

#### 3.4. `dispatch_one(message_id)` — алгоритм

```text
1. Загрузить Message + MailAccount (по Message.mail_account_id) + вложения (MessagesRepo.list_attachments_bulk).
   if not message / not account: log + skip.
2. if account.group_id IS NULL: skip (персональный ящик — defensive, enqueue уже фильтрует).
3. gf = GroupForwardingRepo.get_by_group_id(account.group_id)
   if gf is None or not gf.is_active: skip (нет конфига / выключен).
4. Temporal-guard: if message.internal_date < gf.created_at: skip (не флудим историей / initial-backfill).
5. Loop-guard (часть 2): if gf.forward_to == account.email: skip (форвард самому себе → петля).
6. Claim: fid = MessageForwardsRepo.claim(message_id, account.group_id, gf.forward_to)
          # INSERT ... ON CONFLICT (message_id, group_id) DO NOTHING RETURNING id
   if fid is None: skip (уже переслано — идемпотентность).
7. Per-account forward rate-limit (try_consume, fail-open + лог при превышении) — защита от burst.
8. msg = build_forward_mime(account, message, attachments, gf.forward_to)   # §4
9. try: smtp_send_message(account, msg, recipients=[gf.forward_to])          # §5, без Sent-append
      MessageForwardsRepo.mark_sent(fid)                                     # sent_at = now()
   except SMTP/OAuth/timeout error as e:
      MessageForwardsRepo.mark_error(fid, truncate(str(e), 500))            # НЕ ретраить, лог, не ронять цикл
```

**Инварианты:** идемпотентность через `message_forwards` UNIQUE; loop-guard в двух точках (§3.2 enqueue + §3.4 шаги 4/5); `FORWARD_MAX_TOTAL_BYTES` ограничивает размер письма (§4); sync_cycle никогда не падает из-за ошибок SMTP/Redis на этом пути.

#### 3.5. Регистрация job — `worker/app/main.py`

`IntervalTrigger(seconds=FORWARD_DISPATCH_INTERVAL_SECONDS)`, `id="forward_dispatch"`, `coalesce=True`, `max_instances=1`, обёрнут в `_safe_*`-wrapper (unhandled-исключение логируется, не валит scheduler). Регистрируется **только** при `FORWARDING_ENABLED=true` (как push/mailbox_alert по флагу).

#### 3.6. Без recovery-scan (осознанно)

В отличие от webhooks (ADR-0023 §3.5), у переадресации **нет** `recovery_scan` и **нет** ретраев. Модель — fire-and-forget-после-claim (как ADR-0033/ADR-0027): при падении worker между claim и отправкой, либо при ошибке SMTP, форвард по этому письму теряется без повтора; письмо остаётся в системе и видно в UI. Компромисс зафиксирован как **TD-043**.

### 4. Сборка пересылаемого письма — `build_forward_mime(...)`

Текущий `backend/app/send/mime.py::build_mime` строит **только** `text/plain` без HTML/вложений. Добавляется `build_forward_mime(account, message, attachments, forward_to) -> EmailMessage` (в `send/mime.py` или `forwarding/mime.py`) — `multipart/mixed`:

- **Заголовки:** `Subject: Fwd: <original subject>` (при пустом — `Fwd: (без темы)`); `From: account.email`; `To: forward_to`; новый `Message-ID` (`generate_message_id`, как в send); **`X-Forwarded-By: mail-aggregator`** (loop-guard-штамп).
- **Тело:** `add_alternative(text)` + `add_alternative(html, subtype="html")` (html-часть — только если `body_html` непуст). Обе части **предваряются** блоком «пересланное сообщение», собранным из полей `Message`:
  ```
  ---------- Пересланное сообщение ----------
  От: <from_name или from_addr>
  Дата: <internal_date>
  Кому: <to_addrs>
  Тема: <subject или «(без темы)»>
  ```
  (в html-части — эквивалент с `<br>`; все значения `html.escape()`).
- **Вложения:** для каждого `att` **кроме** `skipped_too_large=true` — стрим `Storage.get_object_stream(att.s3_key)` → `msg.add_attachment(data, maintype, subtype, filename)`. Контролируется суммарный `FORWARD_MAX_TOTAL_BYTES` (~25 МБ): вложение, выводящее сумму за лимит, **пропускается**. Пропущенные (oversized и over-limit) перечисляются в теле блоком «⚠️ Вложения не пересланы (слишком большие): <имена>».

### 5. Отправка — переиспользование SMTP-ядра

Из `backend/app/send/service.py` выделяется общий хелпер `smtp_send_message(account, msg, recipients)` (backend-агент), инкапсулирующий обе ветки аутентификации:
- **password:** `decrypt_mail_password(account.smtp_encrypted_password | encrypted_password, account.id)` + SMTP LOGIN;
- **oauth_outlook:** `OutlookTokenService.get_valid_access_token(account)` + XOAUTH2 (`_smtp_send_oauth`);
плюс `assert_public_host(account.smtp_host)` (SSRF), `_ssl_context`, `_SMTP_TIMEOUT=20` (fail-fast, ADR-0032 follow-up). Используется и в `send`, и в `forward`.

- **Sent-append НЕ делается** для форвардов — не засоряем «Отправленные» ящика (в отличие от обычной отправки).
- **SSRF-поверхность форварда узкая:** соединение идёт **только** к SMTP-хосту самого ящика (уже валидирован `assert_public_host` при создании ящика и повторно в `smtp_send_message`); `forward_to` — это адрес в конверте, к нему прямого соединения нет. Поэтому отдельной URL-SSRF-проверки (как для webhook URL) не требуется.

### 6. Config (`shared/config.py` / env)

| Env | Default | Назначение |
| --- | --- | --- |
| `FORWARDING_ENABLED` | `true` | Kill-switch. `false` → worker не enqueue'ит и не регистрирует job `forward_dispatch`. |
| `FORWARD_DISPATCH_INTERVAL_SECONDS` | `5` | Интервал APScheduler для `forward_dispatch`. |
| `FORWARD_BATCH_SIZE` | `30` | `LPOP count` из `forward_dispatch_queue` за тик. |
| `FORWARD_MAX_TOTAL_BYTES` | `26214400` (25 МБ) | Суммарный лимит вложений в одном форварде; превышающие — пропускаются с пометкой. |
| `FORWARD_PER_ACCOUNT_PER_MINUTE` | `30` | Per-account throttle пересылок (Redis token-bucket, fail-open + лог). |

Отправка идёт **кредами самих ящиков** — новых секретов нет.

---

## Consequences

### Положительные
- **Продуктовая ценность:** лидер видит весь входящий поток команды в своём привычном почтовом клиенте без входа в сервис.
- **Максимальное переиспользование:** форк webhooks (per-group CRUD/ACL, очередь+диспатчер, дедуп-claim) + send-ядро (SMTP/креды/OAuth) + storage (вложения). Новый код — тонкий слой поверх доказанных паттернов; одна миграция, две таблицы, один job.
- **Отправитель = сам ящик** → письмо проходит SPF/DKIM домена ящика (меньше шанс попасть в спам у лидера), не требует служебного SMTP (которого нет).
- **Exactly-once после claim** гарантирован БД (`UNIQUE(message_id, group_id)`), не Redis.
- **Изоляция:** отдельная очередь/диспатчер/таблицы; сбой пересылки не влияет на sync, TG-нотификации и webhooks, и наоборот.
- **Двойной loop-guard** (enqueue-side header-inspection + consumer self-check) + отсутствие пересылки персональных ящиков делают петлю практически невозможной.

### Отрицательные / компромиссы
- **Fire-and-forget после claim (нет retry/recovery)** — при ошибке SMTP или падении worker форвард по письму теряется без повтора (строка `message_forwards` с `error` не ретраится). Письмо доступно в UI. **TD-043**.
- **Полное содержимое + вложения → трафик и объём писем.** Форвард дублирует весь входящий поток команды через SMTP ящика; крупные вложения (> `FORWARD_MAX_TOTAL_BYTES`) пропускаются с пометкой — лидер видит их в самом сервисе.
- **Отправитель — получивший ящик** → в почтовом ящике лидера форварды приходят «от» разных ящиков команды (`From` = ящик), а не от единого адреса. Это осознанный выбор (служебного SMTP нет); оригинальный отправитель виден в блоке «пересланное сообщение».
- **Temporal-guard по `gf.created_at`** может переслать «свежую» историю при добавлении нового ящика в команду с уже включённой переадресацией (backfill-письмо с `internal_date >= created_at`). Редкий edge; ограничен 30-дневным backfill-окном (ADR-0008).
- **at-most-once на письмо** — если письмо не переслалось с первой попытки, лидер его в почте не увидит (только в UI сервиса). Приемлемо для нотификационного канала.

---

## Alternatives considered

1. **Server-side IMAP-redirect / правило пересылки у провайдера** вместо нашего SMTP-resend (отвергнуто).
   - Contra: требует прав/настройки forwarding-rule в каждом провайдере (Gmail/Yandex/Outlook — разные API, часто недоступны по IMAP/паролю; OAuth-scope не покрывает); нет единого контроля «одна запись на команду»; нельзя пересобрать письмо/добавить блок «пересланное». Наш resend полностью в нашем контроле и однороден по провайдерам.
   - **Решение:** наш SMTP-resend кредами ящика.

2. **Отправитель = служебный SMTP** (единый `noreply@`) вместо получившего ящика (отвергнуто).
   - Contra: **служебного SMTP у сервиса нет** (инфраструктурно). Единый `From` дал бы стабильный отправитель, но потребовал бы завести и сопровождать отдельный домен/ящик + пройти SPF/DKIM, чего в scope нет.
   - **Решение:** отправитель = получивший ящик (его креды). Оригинальный `From` — в теле.

3. **Дедуп через boolean-флаг `messages.forwarded`** вместо таблицы `message_forwards` (отвергнуто).
   - Contra: не различает команды (ящик может сменить команду — ADR-0031), не даёт audit `forward_to`/`error`/`sent_at`, требует миграции колонки + backfill. Флаг — одно состояние на письмо, а нужно per-`(message,group)`.
   - **Решение:** таблица с `UNIQUE(message_id, group_id)` — форк `webhook_deliveries`.

4. **Пересылать только письма с тегами** (webhook-семантика) (отвергнуто).
   - Contra: продукт-требование — «все входящие команды», не «важные». Лидер хочет полную копию потока.
   - **Решение:** enqueue **всех** новых входящих ящиков команды (без тег-предиката).

5. **Recovery-scan + retry** недоставленных форвардов (как webhook ADR-0023 §3.5) (отвергнуто для MVP).
   - Contra: усложняет (нужен scan-SQL с `NOT EXISTS(message_forwards)`, окно, LIMIT). Для нотификационной пересылки fire-and-forget достаточно (письмо не теряется — оно в UI). Симметрично ADR-0033/ADR-0027.
   - **Решение:** без recovery; компромисс — TD-043. При жалобах на пропуски — лёгкий recovery отдельным ADR.

6. **`POST`(create, 409)+`PATCH`(update)** как у webhooks (отвергнуто).
   - Contra: запись одна на команду — `PUT`-upsert проще (нет 409-ветки, идемпотентный «задать адрес команды»). Webhook отдаёт one-shot secret при create → там `POST` оправдан; здесь секрета нет.
   - **Решение:** `PUT /api/forwarding/me` (upsert) + `GET`/`DELETE`.

7. **Loop-guard через хранимую колонку-флаг** (`messages.is_forwarded_copy`, выставляемую в sync при детекте заголовка) вместо inline-инспекции заголовков на enqueue (отвергнуто).
   - Contra: требует миграции колонки + запись в горячем пути sync каждого письма. Заголовки `X-Forwarded-By` доступны в live-объекте `imap_tools` прямо в `save_message` — проверка бесплатна и без схемы.
   - **Решение:** enqueue-side header-inspection (§3.2), без новой колонки.

---

## Open questions

Нет блокеров. Модель (§1), API/ACL (§2), пайплайн и loop-guard (§3), MIME (§4), SMTP-переиспользование (§5), config (§6) зафиксированы. Компромисс отсутствия retry/recovery — TD-043.

## Cross-references
- ADR-0023 — образец per-group конфига + очередь/диспатчер/дедуп (форк-источник).
- ADR-0030 — членства `user_groups` (предикат ACL super_admin/leader).
- ADR-0033 / ADR-0027 — fire-and-forget dispatch без recovery (образец §3.6).
- ADR-0005 / ADR-0025 — шифрование кредов, XOAUTH2 (переиспользуются в `smtp_send_message`).
- ADR-0015 — no-JS `_method` fallback (§2.1).
- `03-data-model.md` — таблицы `group_forwarding`, `message_forwards`; миграция `20260703_021`.
- `04-api-contracts.md` §4e — endpoints `/api/forwarding/me`.
- `05-modules.md` §19a — модуль `forwarding`; §14.4 — worker `forward_dispatch`; §11 — расширение `send/mime`/`smtp_send_message`.
- `06-security.md` §1.14 — угрозы канала переадресации.
- `08-frontend.md` — секция «Переадресация» на `/my/integrations`.
- `100-known-tech-debt.md` — TD-043.
