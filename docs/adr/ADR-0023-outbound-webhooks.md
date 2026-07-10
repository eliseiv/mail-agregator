# ADR-0023 — Outbound webhooks для команд (push-нотификации в произвольные внешние HTTP-сервисы по тегам)

| | |
| --- | --- |
| Статус | **superseded by [ADR-0043](./ADR-0043-strip-to-connector-push-to-crm.md)** (2026-07-10) — outbound webhooks снимаются (в проде 0); ранее accepted |
| Дата | 2026-05-20 |
| Заменяет / отменён | — (не отменяет ADR-0022; это **отдельный** канал доставки, параллельный TG-нотификациям) |

## Context

После закрытия TD-013 в [ADR-0022](./ADR-0022-telegram-sso-and-notifications.md) единственный outbound-канал в системе — push-уведомления в Telegram бота. Канал привязан к **внутренним** пользователям (через `telegram_links`) и к **нашему** боту: внешние интеграции (CRM, Slack, аналитика, Make.com, n8n, любой self-hosted endpoint клиента) невозможны.

Пользователь явно запросил:

1. **Лидер команды настраивает ОДИН webhook на свою команду.** Super-admin может создать webhook для любой команды; «глобальных» webhook'ов нет.
2. **Триггер — только письма с тегами**, симметрично TG-уведомлениям. На письма без тегов webhook не срабатывает.
3. **Auth — статический secret в заголовке `X-Webhook-Secret`** (явный выбор пользователя; HMAC-signature избыточен для текущего масштаба и сложнее в интеграции на стороне получателя).
4. **Один webhook на команду** (`UNIQUE(group_id)`), без поддержки нескольких подписчиков на одну команду в MVP.
5. **Симметричная фильтрация «не флудим историей»** — webhook **не** получает письма, которые пришли в БД **до** его создания (`m.internal_date >= webhook.created_at`); это уже доказанный паттерн (round-13 для TG-нотификаций).

С момента ADR-0022 архитектура push-доставки — стабильна и переиспользуема: in-memory accumulator в `sync_one_account` → `LPUSH ... queue` после COMMIT → APScheduler-job каждые 5 сек `LPOP` батчами → доставка с обработкой 429/5xx/transient → таблица-реестр для idempotency с `UNIQUE(target_id, message_id)` + recovery_scan каждый час с 24-часовым окном. Этот паттерн **прямо переиспользуется** для outbound webhooks; добавляется только новый домен (свои таблицы, свой Redis-list, свой dispatcher, свой recovery) поверх существующего `worker.sync_cycle` без изменений его контракта.

Существующая инфраструктура (которая прямо используется):
- `worker.sync_cycle.save_message` уже собирает `notified_message_ids: list[int]` и в конце `sync_one_account` LPUSH'ит их в `tg_notify_queue` (см. `05-modules.md` §14). Для webhook'ов добавляется параллельный LPUSH в **отдельный** `webhook_dispatch_queue` — те же `message_id`, та же изоляция try/except.
- `MailPasswordCipher` (AES-256-GCM, версия + IV + ciphertext+tag) с AAD-биндингом по `mail_account_id` (ADR-0005, `shared/crypto.py`). Тот же примитив переиспользуется для `webhook.secret_encrypted` с AAD-биндингом по `webhook_id`.
- `MailAccountsRepo.list_canonical_account_ids` (round-18) для дедупликации email-учёток между командами — webhook должен использовать тот же helper, чтобы дубли email в разных командах не приводили к double-dispatch одной командой.
- `slowapi` rate-limit infrastructure (ADR-0009) для `/api/webhooks/me/test`.
- `AuditWriter.log` + closed-enum `ALLOWED_ACTIONS` (`backend/app/audit/service.py`) для записи lifecycle webhook'ов.
- SSRF protection-pattern из [§4 `06-security.md`](../06-security.md#4-ssrf-защита-для-imapsmtp-testconnect) для IMAP/SMTP — тот же паттерн применяется к URL'у webhook'а (запрет приватных CIDR в DNS-резолве).

---

## Decision

### 1. Модель данных — две новые таблицы

#### 1.1. `webhooks` — конфигурация (1 row на команду)

```sql
CREATE TABLE webhooks (
    id                   BIGSERIAL PRIMARY KEY,
    group_id             BIGINT NOT NULL UNIQUE
                         REFERENCES groups(id) ON DELETE CASCADE,
    url                  TEXT NOT NULL,                       -- https only, 1..2048
    secret_encrypted     BYTEA NOT NULL,                      -- AES-256-GCM, AAD = b"webhook_secret|" + webhook.id
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    consecutive_failures INT NOT NULL DEFAULT 0,
    dead_at              TIMESTAMPTZ NULL,
    last_fired_at        TIMESTAMPTZ NULL,
    last_error           TEXT NULL,                           -- усечённый до 500 байт, без secrets
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT webhooks_url_https_check CHECK (url LIKE 'https://%'),
    CONSTRAINT webhooks_url_length_check CHECK (char_length(url) BETWEEN 9 AND 2048)
);

CREATE INDEX webhooks_active_idx ON webhooks(is_active) WHERE is_active = TRUE;
-- UNIQUE(group_id) уже служит индексом для lookup по группе при dispatch.
```

**Каскады:**
- `DELETE FROM groups WHERE id=:gid` → каскадно удалит `webhooks` (FK ON DELETE CASCADE) → каскадно удалит `webhook_deliveries` (см. ниже). При штатном `DELETE /api/admin/groups/{id}` (ADR-0019, см. `04-api-contracts.md`) backend сначала требует пустоту группы, но FK на webhooks намеренно `CASCADE` — webhook привязан к группе, не к участникам.

**Объём:** ≤ 5 команд × 1 webhook = ≤ 5 строк.

**Trigger:** `BEFORE UPDATE ON webhooks` — `NEW.updated_at = now()` (общий паттерн с `users_settings`, `tags`, `mail_accounts`, `users`).

#### 1.2. `webhook_deliveries` — реестр доставленных событий (idempotency)

```sql
CREATE TABLE webhook_deliveries (
    id               BIGSERIAL PRIMARY KEY,
    webhook_id       BIGINT NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
    message_id       BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    sent_at          TIMESTAMPTZ NULL,                        -- NULL = row claim'ed, POST ещё не завершён
    response_code    INT NULL,                                -- HTTP status от target endpoint
    response_excerpt TEXT NULL,                               -- первые 500 байт response body, без secrets
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT webhook_deliveries_unique UNIQUE (webhook_id, message_id)
);

CREATE INDEX webhook_deliveries_webhook_id_idx ON webhook_deliveries(webhook_id);
CREATE INDEX webhook_deliveries_message_id_idx ON webhook_deliveries(message_id);
-- UNIQUE(webhook_id, message_id) обслуживает try-claim INSERT ... ON CONFLICT DO NOTHING.
```

**Каскады:**
- `DELETE FROM messages WHERE id=:mid` (retention cleanup, ADR-0011) → каскадно удалит `webhook_deliveries`. Симметрично `telegram_notifications` (ADR-0022 §2.3).
- `DELETE FROM webhooks WHERE id=:wid` → каскадно удалит `webhook_deliveries`. Симметрично.

**Объём:** оценка ≤ 5 команд × ≤ 100 ящиков (общий пул) × ~5 писем-с-тегами/день × 30 дней retention = **~75 000 строк max** на пике. С CASCADE от `messages` (retention 30d) → автоматическая очистка.

### 2. API — endpoints для CRUD конфигурации

Все endpoints — `super_admin` ИЛИ `group_leader`. `group_member` → 403.

`super_admin` работает с тем же endpoint-prefix'ом `/api/webhooks/me` через query-параметр `group_id` (см. ниже). Отдельный `/api/admin/webhooks/...` не вводится — оставляем поверхность API минимальной.

#### 2.1. `GET /api/webhooks/me`

| | |
| --- | --- |
| Доступ | `group_leader` (по `scope.group_id`) ИЛИ `super_admin` (с обязательным `?group_id=<id>`) |
| Query | `group_id?: int` — **обязателен** для `super_admin`, **запрещён** для `group_leader` (если передан — `400 validation_error` `field=group_id`). Семантика: «webhook какой именно группы». |
| 200 (найден) | `{id, group_id, url, is_active, last_fired_at, last_error, dead_at, consecutive_failures, created_at, updated_at}` — **БЕЗ `secret`**. |
| 404 | `not_found` если у группы webhook не настроен. |
| 403 | `forbidden` — group_member или group_leader не в той группе, что указано через `group_id` (super_admin override не его кейс). |

#### 2.2. `POST /api/webhooks/me`

| | |
| --- | --- |
| Доступ | `group_leader` (на свою группу) ИЛИ `super_admin` (с `?group_id=<id>`) |
| Запрос | JSON `{url: str}` |
| Валидация | `url` — `https://...`, max 2048; DNS-резолв ВСЕХ A/AAAA target host **запрещает** приватные CIDR (см. §4 SSRF ниже). При попадании → `400 webhook_url_private_ip`. `https://localhost` / `127.0.0.1` / `[::1]` отвергаются на этапе lexical-parse без DNS. |
| Поведение | (1) Backend генерирует `secret_plaintext = secrets.token_urlsafe(32)` (44 символа base64url); (2) INSERT row через `nextval('webhooks_id_seq')` → шифруем `secret_plaintext` с AAD=`b"webhook_secret\|" + str(webhook_id).encode()` (тот же паттерн, что у `MailPasswordCipher`, см. `06-security.md` §2 и §«AAD для INSERT» в `05-modules.md` модуль 5); (3) INSERT с явным `id`. |
| Rate-limit | 10/час per group (защита от спама создания). |
| 201 | `{id, group_id, url, secret: "<plaintext>", is_active: true, last_fired_at: null, last_error: null, dead_at: null, consecutive_failures: 0, created_at, updated_at}` — **`secret` показан ОДИН РАЗ**, лидер обязан скопировать; никакой recovery нет. |
| 409 | `conflict` `field=group_id` — у группы уже есть webhook (UNIQUE `group_id`). Лидер делает `PATCH` для смены URL или `DELETE` + `POST`. |
| 400 | `validation_error` (некорректный URL) / `webhook_url_private_ip` (SSRF). |
| 403 | `forbidden`. |
| Audit | `webhook_created` (`actor_user_id` = вызвавший, `target_user_id` = лидер группы (или сам super_admin при self-action), `details = {group_id, webhook_id, url}`). |

#### 2.3. `PATCH /api/webhooks/me`

| | |
| --- | --- |
| Доступ | `group_leader` ИЛИ `super_admin` (с `?group_id=<id>`) |
| Запрос | JSON: любое подмножество `{url?: str, is_active?: bool}` |
| Поведение | (a) `url` — та же валидация, что в `POST`; смена URL **не** ротирует secret. (b) `is_active=true` после `dead_at` → backend в одной транзакции делает `UPDATE webhooks SET is_active=true, dead_at=NULL, consecutive_failures=0, last_error=NULL WHERE id=:wid` (re-enable). (c) `is_active=false` → диспатчер пропускает доставку (фильтр `WHERE is_active=true`); существующие row в `webhook_deliveries` не удаляются. |
| 200 | объект как в `GET` (без `secret`). |
| 400 | `validation_error` / `webhook_url_private_ip`. |
| 404 | `not_found`. |
| 403 | `forbidden`. |
| Audit | `webhook_updated` (`details = {webhook_id, changed_fields: ["url"|"is_active"], previous_dead_at: ts\|null}`). |

#### 2.4. `DELETE /api/webhooks/me`

| | |
| --- | --- |
| Доступ | `group_leader` ИЛИ `super_admin` (с `?group_id=<id>`) |
| Поведение | `DELETE FROM webhooks WHERE id=:wid` → CASCADE удалит `webhook_deliveries`. После delete LPUSH в очередь больше не происходит (sync_cycle resolve'ит recipients перед каждым dispatch). Возможные in-flight POST'ы в worker'е завершатся естественным путём (диспатчер ловит ошибки), но новые row в `webhook_deliveries` уже не создадутся. |
| 204 | success. |
| 404 | `not_found`. |
| 403 | `forbidden`. |
| Audit | `webhook_deleted` (`details = {webhook_id, group_id, url}`). |

#### 2.5. `POST /api/webhooks/me/rotate-secret`

| | |
| --- | --- |
| Доступ | `group_leader` ИЛИ `super_admin` (с `?group_id=<id>`) |
| Запрос | пустое тело (CSRF обязателен). |
| Поведение | (1) Backend генерирует новый `secret_plaintext`; (2) шифрует с AAD=`webhook_id` (тем же id, что и был); (3) UPDATE `secret_encrypted=<new_blob>, updated_at=now()`. Старый secret **немедленно** перестаёт быть валидным — все следующие POST'ы используют новый. **Внимание:** receiver-приложение должно поддерживать ротацию (deploy → обновить secret) без даунтайма со своей стороны; если receiver не успеет — будет получать 200 OK, но HMAC'a и не было — он не сможет валидировать. (Возможен паттерн «двойного secret» с TTL на старый — отложен в [§Open questions Q-WH-1](#open-questions).) |
| Rate-limit | 5/час per webhook (защита от accidental DoS на самого себя). |
| 200 | `{id, group_id, secret: "<new-plaintext>", url, is_active, ...}` — secret снова показан **один раз**. |
| 404 | `not_found`. |
| 403 | `forbidden`. |
| Audit | `webhook_secret_rotated` (`details = {webhook_id}`). |

#### 2.6. `POST /api/webhooks/me/test`

| | |
| --- | --- |
| Доступ | `group_leader` ИЛИ `super_admin` (с `?group_id=<id>`) |
| Запрос | пустое тело (CSRF обязателен). |
| Поведение | Backend синхронно (внутри request-handler) делает один POST на webhook URL с фиксированным `event="test"` payload. **НЕ** пишет row в `webhook_deliveries`. **НЕ** трогает `consecutive_failures` / `dead_at` / `last_error` — это диагностическая операция. Возвращает receiver response code и (truncated) body клиенту. |
| Rate-limit | 10/час per webhook (env `WEBHOOK_TEST_LIMIT=10`). |
| 200 | `{response_code: int, response_excerpt: str, duration_ms: int}` — даже при receiver 5xx/timeout возвращаем 200 с информацией (это диагностика, не ошибка нашей системы). |
| 502 | `upstream_error` — DNS-резолв fail, timeout > 10s, network unreachable. `details: {reason}`. |
| 404 | `not_found`. |
| 403 | `forbidden`. |

Payload `event="test"`:
```json
{
  "event": "test",
  "timestamp": "2026-05-20T12:00:00.000Z",
  "webhook_id": 7,
  "team": {"id": 5, "name": "Команда A"}
}
```

#### 2.7. HTML route — `GET /my/integrations`

| | |
| --- | --- |
| Доступ | `group_leader` (своя группа) ИЛИ `super_admin` (видит селектор группы с дропдауном; на старте — простая форма «выберите group_id», на следующей итерации UI можно усовершенствовать). |
| Render | Jinja2 template `templates/my/integrations.html` — форма URL input + статус (last_fired_at / last_error / consecutive_failures / dead-indicator) + кнопки `[Rotate secret]`, `[Test webhook]`, `[Delete]`. |
| Form-fallback | Все state-changing операции в этом разделе используют form-encoded fallback (см. ADR-0015, `04-api-contracts.md` секция «Form-encoded fallback»). Sibling-роуты `.../delete` для DELETE через `POST + _method=DELETE`. Modal с показом secret один раз — реализуется server-side через flash-сообщение со специальной категорией `secret_reveal` (one-shot, очищается после первого GET). |

#### 2.8. Заголовки исходящего POST (от нас → target)

| Заголовок | Значение |
| --- | --- |
| `Content-Type` | `application/json; charset=utf-8` |
| `X-Webhook-Secret` | `<plaintext-secret>` (расшифрованный из `secret_encrypted`) |
| `User-Agent` | `mas-webhook/1.0` |
| `X-Webhook-Event` | `message_tagged` или `test` (дублирует поле `event` payload — удобно для receiver-маршрутизации) |
| `X-Webhook-Delivery-Id` | `<webhook_deliveries.id>` (для `message_tagged`; в `test` — `00000000` placeholder) |

**Timeout:** total `WEBHOOK_HTTP_TIMEOUT_SECONDS=10` (connect + read + write); httpx параметр `timeout=httpx.Timeout(10.0)`.

#### 2.9. Payload `event="message_tagged"`

```json
{
  "event": "message_tagged",
  "timestamp": "2026-05-20T12:00:00.000Z",
  "webhook_id": 7,
  "team": {"id": 5, "name": "Команда A"},
  "message": {
    "id": 12345,
    "internal_date": "2026-05-20T11:55:00Z",
    "from_addr": "sender@example.com",
    "from_name": "Sender Name",
    "subject": "Тема письма",
    "body_text": "Plain-text content, truncated to first 16384 chars",
    "body_truncated": false,
    "mail_account": {
       "id": 7,
       "email": "support@example.com",
       "display_name": "Support"
    },
    "tags": [
      {"id": 7, "name": "Urgent", "color": "#dc2626"}
    ]
  }
}
```

**Решение по содержимому payload:**

1. **Один POST на команду на сообщение**, не «по тегу пользователя». В отличие от TG-нотификаций (per-user, потому что теги per-user), здесь webhook привязан к команде → recipients = одна команда → 1 POST. В `tags` массив включаются **теги всех участников команды + владельца ящика** на этом письме, но **НЕ** персональные теги super_admin (round-28: super_admin-теги изолированы от webhook-канала — см. §3.2 «Изоляция от персональных тегов super_admin» и ADR-0017 §5.1). Это соответствует семантике «команда получает уведомление о письме, на которое сработал хоть один её тег». См. §3.2 ниже про recipient SQL.
2. **Body truncate**: `body_text[:16384]` (16 KiB), с флагом `body_truncated`. Полное body может быть до 1 MiB (ADR-0012), но receiver-системы (Make.com, n8n, Zapier и т.д.) обычно ограничены 8–32 KiB на webhook payload — 16 KiB выбран как разумный компромисс.
3. **Attachments — НЕ включаются в payload.** Receiver хочет знать «пришло письмо с тегом» — для скачивания файла receiver может обратиться к нашей API через сессию пользователя из своей системы. В payload включается только `body_text`. Если в будущем потребуется список attachments — это **отдельный ADR** (минор-bump payload schema, при этом field `attachments` будет добавлен опционально, без breaking change).
4. **Не включаем `to_addrs` / `cc_addrs`**: payload должен помочь receiver'у роутить — он знает свой mail_account (email/display_name) и отправителя; адресаты-получатели (`to`) совпадают с этим mail_account.

**Schema versioning:** `event="message_tagged"` фиксирован в текущем виде; будущие добавления non-required полей — без breaking change. Удаления/переименования полей **запрещены без нового ADR** (`ADR-0023.1`).

### 3. Dispatch pipeline — параллельный TG, но изолированный

#### 3.1. Триггер в `worker.sync_cycle`

После round-13 + ADR-0022 §2.1 в `sync_one_account` уже есть аккумулятор `notified_message_ids: list[int]` (см. `worker/app/sync_cycle.py:213` и `:288`). После `mailbox.logout()` (см. `:314`) делается:

```python
if notified_message_ids:
    try:
        async with make_session() as s, s.begin():
            pushed = await TelegramNotifyService(s).enqueue_message_ids(notified_message_ids)
        log.info("tg_notify_enqueue", count=len(notified_message_ids), pushed=pushed)
    except Exception as e:
        log.warning("tg_notify_enqueue_failed", error=str(e))
```

ADR-0023 **симметрично добавляет** ниже того же блока:

```python
    try:
        async with make_session() as s, s.begin():
            pushed = await WebhookDispatchService(s).enqueue_message_ids(notified_message_ids)
        log.info("webhook_enqueue", count=len(notified_message_ids), pushed=pushed)
    except Exception as e:
        log.warning("webhook_enqueue_failed", error=str(e))
```

**Инварианты переиспользования:**
- Тот же список `notified_message_ids` — TG и webhook доставляются за один и тот же триггер (apply_tags вернул `applied_count>0`).
- Failure LPUSH webhook'а НЕ валит sync_cycle И НЕ валит TG-доставку (изоляция через try/except). Симметрично.
- `WebhookDispatchService.enqueue_message_ids(ids)` принимает `list[int]` (без дублей; sync_cycle гарантирует уникальность в рамках одного account-цикла) и делает **один** batched `LPUSH webhook_dispatch_queue val1 val2 ...`. Перед LPUSH service фильтрует ids — если у команд, владеющих этими mail_accounts, нет настроенного webhook'а (или `is_active=false` / `dead_at IS NOT NULL`), id отбрасываются. Это снижает мусор в очереди.

#### 3.2. Recipient resolution (SQL для одного `message_id`)

```sql
-- На каждый message_id из очереди диспатчер вызывает:
SELECT
    w.id AS webhook_id,
    w.group_id,
    w.url,
    w.secret_encrypted
FROM webhooks w
JOIN mail_accounts ma ON ma.id = (
    SELECT m_inner.mail_account_id FROM messages m_inner WHERE m_inner.id = :mid
)
JOIN messages m ON m.id = :mid
WHERE w.group_id = ma.group_id                      -- webhook принадлежит группе, владеющей ящиком
  AND w.is_active = TRUE
  AND w.dead_at IS NULL
  AND m.internal_date >= w.created_at               -- «не флудим историей» (симметрично round-13 для TG)
  AND EXISTS (                                       -- у кого-то из КОМАНДЫ есть свой тег на этом письме
      SELECT 1
      FROM message_tags mt
      JOIN tags t ON t.id = mt.tag_id
      JOIN users u ON u.id = t.user_id
      WHERE mt.message_id = m.id
        AND (
            u.group_id = ma.group_id                 -- участник команды, владеющей ящиком
            OR u.id = ma.user_id                     -- владелец ящика (на случай владельца вне группы)
        )
        -- ВНИМАНИЕ: ветки `u.role='super_admin'` здесь НЕТ намеренно (round-28).
        -- См. блок «Изоляция от персональных тегов super_admin» ниже.
  )
LIMIT 1;
```

> **Изоляция от персональных тегов super_admin (round-28).**
> round-28 (ADR-0017 §5.1) навешивает персональные теги super_admin на письма
> **всех** команд, чтобы super_admin получал **TG**-уведомления. Webhook-канал
> команды эти теги учитывать **не должен**: иначе письмо, помеченное **только**
> super_admin-тегом (и ни одним тегом членов команды), ложно триггерило бы
> webhook чужой команды, а `name`/`color` персонального тега super_admin утекли бы
> в её внешний JSON-payload. Поэтому EXISTS-предикат смотрит только на
> принадлежность тега самой команде (`u.group_id = ma.group_id`) или владельцу
> ящика (`u.id = ma.user_id`), **без** `u.role='super_admin'`. То же правило
> применяется к `list_tags_for_team` (ниже). Пользователь запросил только
> TG-уведомление super_admin — не webhook.

**Почему один webhook на сообщение** (а не fan-out по user-тегам):
- Webhook привязан к команде (`UNIQUE(group_id)`), не к user'у. Команда хочет одно событие «пришло письмо с тегом» — receiver уже сам решит, что с этим делать.
- В payload `tags[]` мы включаем **агрегацию тегов команды на этом письме** через дополнительный SELECT (`list_tags_for_team`) — теги участников группы и владельца ящика, **без** персональных тегов super_admin (та же изоляция, что в EXISTS выше):

```sql
SELECT DISTINCT t.id, t.name, t.color
FROM message_tags mt
JOIN tags t ON t.id = mt.tag_id
JOIN users u ON u.id = t.user_id
JOIN mail_accounts ma ON ma.id = (
    SELECT m_inner.mail_account_id FROM messages m_inner WHERE m_inner.id = :mid
)
WHERE mt.message_id = :mid
  AND (u.group_id = :group_id OR u.id = ma.user_id)   -- команда + владелец ящика; НЕ super_admin
ORDER BY t.name;
```

> `:group_id` здесь равен `ma.group_id` (передаётся диспатчером из recipient'а
> §3.4 шаг 4). Ветка `u.id = ma.user_id` симметрична EXISTS-предикату — покрывает
> владельца ящика вне группы. Персональные теги super_admin (group_id иной/NULL,
> не владелец чужого ящика) в выборку не попадают.

#### 3.3. APScheduler job `webhook_dispatch`

```python
# worker/app/webhook_dispatch.py
async def webhook_dispatch() -> None:
    """Каждые WEBHOOK_DISPATCH_INTERVAL_SECONDS=5 сек drain webhook_dispatch_queue.
    APScheduler: max_instances=1, coalesce=True.
    """
    items = await redis.lpop("webhook_dispatch_queue", count=WEBHOOK_BATCH_SIZE)  # default 30
    if not items:
        return
    for raw in items:
        message_id = json.loads(raw)["message_id"]
        await dispatch_one_payload(message_id)
```

#### 3.4. `dispatch_one_payload(message_id)` — алгоритм

```text
1. ctx = SELECT m.id, m.subject, m.from_addr, m.from_name, m.body_text, m.body_truncated,
                m.internal_date,
                ma.id AS mail_account_id, ma.email, ma.display_name AS mail_account_display_name,
                ma.group_id,
                g.name AS group_name
         FROM messages m
         JOIN mail_accounts ma ON ma.id = m.mail_account_id
         JOIN groups g ON g.id = ma.group_id
         WHERE m.id = :mid
   if not ctx: log warn "webhook_dispatch_message_missing" and return

2. recipient = await WebhooksRepo.find_active_for_message(ctx.mail_account_id, ctx.group_id, message_id)
   # SQL из §3.2 (плюс фильтр m.internal_date >= w.created_at и EXISTS(tags))
   if recipient is None: return                              # нет webhook'а / inactive / dead / нет тегов / history-filter

3. delivery_id = await WebhookDeliveriesRepo.try_reserve(recipient.webhook_id, message_id)
   # INSERT INTO webhook_deliveries (webhook_id, message_id) VALUES (:wid, :mid)
   # ON CONFLICT (webhook_id, message_id) DO NOTHING RETURNING id
   if delivery_id is None: return                            # уже доставлено (идемпотентность)

4. team_tags = await WebhookDeliveriesRepo.list_tags_for_team(message_id, recipient.group_id)
   if not team_tags:
       await WebhookDeliveriesRepo.rollback(delivery_id)     # defensive: не должно случаться (EXISTS в §3.2)
       return

5. try:
       secret_plaintext = mail_password_cipher.decrypt_with_aad(recipient.secret_encrypted, aad_key=recipient.webhook_id)
   except InvalidTag:
       await WebhookDeliveriesRepo.rollback(delivery_id)
       await WebhooksRepo.mark_dead(recipient.webhook_id, reason='secret_decrypt_failed')
       await audit.log(action='webhook_dead_marked', ...)
       return

6. payload = build_message_tagged_payload(ctx, team_tags, recipient.webhook_id, delivery_id)
7. headers = {"Content-Type":"application/json; charset=utf-8",
              "X-Webhook-Secret": secret_plaintext,
              "User-Agent": "mas-webhook/1.0",
              "X-Webhook-Event": "message_tagged",
              "X-Webhook-Delivery-Id": str(delivery_id)}

8. try:
       resp = await httpx_client.post(recipient.url, json=payload, headers=headers,
                                       timeout=httpx.Timeout(WEBHOOK_HTTP_TIMEOUT_SECONDS=10),
                                       follow_redirects=False)
   except (httpx.TimeoutException, httpx.NetworkError) as e:
       # transient → откатить row, оставить для recovery_scan, increment не делаем
       await WebhookDeliveriesRepo.rollback(delivery_id)
       await WebhooksRepo.touch_last_error(recipient.webhook_id, f"network: {type(e).__name__}")
       log warn event=webhook_transient ...
       return

9. if resp.status_code == 410:
       await WebhookDeliveriesRepo.mark_failed(delivery_id, status=410, excerpt=resp.text[:500])
       await WebhooksRepo.mark_dead(recipient.webhook_id, reason='410_gone')
       await audit.log(action='webhook_dead_marked', details={'webhook_id': recipient.webhook_id, 'reason': '410_gone'})
       return

10. if 400 <= resp.status_code < 500 AND resp.status_code not in (408, 429):
        # non-retriable client error
        await WebhookDeliveriesRepo.mark_failed(delivery_id, status=resp.status_code, excerpt=resp.text[:500])
        new_count = await WebhooksRepo.bump_failures_and_set_last_error(
            recipient.webhook_id, last_error=f"HTTP {resp.status_code}: {resp.text[:200]}"
        )
        if new_count >= WEBHOOK_MAX_FAILURES_BEFORE_DEAD:    # 10
            await WebhooksRepo.mark_dead(recipient.webhook_id, reason='consecutive_4xx')
            await audit.log(action='webhook_dead_marked', ...)
        return

11. if resp.status_code in (408, 429) OR 500 <= resp.status_code < 600:
        # retriable: НЕ инкрементим failures, rollback row → recovery_scan подхватит
        await WebhookDeliveriesRepo.rollback(delivery_id)
        await WebhooksRepo.touch_last_error(recipient.webhook_id, f"HTTP {resp.status_code} (will retry)")
        log warn event=webhook_retriable_status
        return

12. # 2xx
    await WebhookDeliveriesRepo.mark_sent(delivery_id, status=resp.status_code, excerpt=resp.text[:500])
    await WebhooksRepo.mark_success(recipient.webhook_id)   # last_fired_at=now(), consecutive_failures=0, last_error=NULL
```

**Замечания:**
- `mark_failed` пишет `sent_at = now(), response_code, response_excerpt` — это маркер «попытались, отказались навсегда». Row остаётся в `webhook_deliveries` для audit.
- `rollback` делает DELETE row → recovery_scan через час подхватит. Используется только для transient ошибок (network, 5xx, 408, 429).
- 410 Gone → немедленный `dead_at`. Стандартная семантика «endpoint больше не существует».
- 401/403/404 от receiver'а → инкремент `consecutive_failures`. При 10 подряд — `dead_at`. Receiver должен поднять корректно работающий endpoint и лидер сделает `PATCH is_active=true` для re-enable.

#### 3.5. APScheduler job `webhook_recovery_scan`

```python
# worker/app/webhook_recovery.py
async def webhook_recovery_scan() -> None:
    """Раз в WEBHOOK_RECOVERY_INTERVAL_SECONDS=3600 сек подбирает не-доставленные.
    Окно: WEBHOOK_RECOVERY_WINDOW_HOURS=24h.
    """
    threshold = now() - timedelta(hours=WEBHOOK_RECOVERY_WINDOW_HOURS)
    ids = await session.execute(text("""
        SELECT m.id
        FROM messages m
        JOIN mail_accounts ma ON ma.id = m.mail_account_id
        WHERE m.fetched_at > :threshold
          AND EXISTS (                                       -- тег КОМАНДЫ (не персональный super_admin)
              SELECT 1 FROM message_tags mt
              JOIN tags t ON t.id = mt.tag_id
              JOIN users u ON u.id = t.user_id
              WHERE mt.message_id = m.id
                AND (u.group_id = ma.group_id OR u.id = ma.user_id)  -- round-28: без super_admin (как §3.2)
          )
          AND EXISTS (
              SELECT 1 FROM webhooks w
              WHERE w.group_id = ma.group_id
                AND w.is_active = TRUE
                AND w.dead_at IS NULL
                AND m.internal_date >= w.created_at        -- «не флудим историей» — recovery соблюдает
          )
          AND NOT EXISTS (
              SELECT 1 FROM webhook_deliveries wd
              JOIN webhooks w ON w.id = wd.webhook_id
              WHERE wd.message_id = m.id
                AND w.group_id = ma.group_id
                AND wd.sent_at IS NOT NULL                 -- учитываем только успешные/mark_failed; rollback'утые row отсутствуют, message подхватится
          )
        ORDER BY m.id
        LIMIT 5000
    """), {"threshold": threshold})
    if not ids: return
    await redis.lpush("webhook_dispatch_queue", *[json.dumps({"message_id": i}) for i in ids])
    log.info("webhook_recovery_scan_finish", count=len(ids))
```

**Замечания:**
- Окно 24 ч покрывает выходные и multi-hour outages. Старше 24 ч — намеренно не доставляем (письмо устарело, receiver уже либо знает, либо не нужно).
- recovery_scan уважает фильтр «не флудим историей» (`m.internal_date >= w.created_at`) — повторно симметрично §3.2.
- recovery_scan использует **тот же team-scoped tag-EXISTS**, что и §3.2 (`u.group_id = ma.group_id OR u.id = ma.user_id`, без `u.role='super_admin'`). Без этого письмо, помеченное **только** персональным тегом super_admin, проходило бы pre-filter, попадало в очередь каждый час в течение 24 ч и тихо отбрасывалось диспатчером (`find_active_for_message` → None) — бесполезный churn. round-28.
- `LIMIT 5000` — защита от взрывного попадания всех 75 000 message'й в очередь в случае многочасового outage.

### 4. Безопасность

#### 4.1. Хранение secret — AES-256-GCM, AAD=webhook_id

Переиспользуется `shared/crypto.py::MailPasswordCipher` (ADR-0005) с новой AAD-привязкой:

```python
# shared/crypto.py — добавляется новый AAD prefix.
WEBHOOK_SECRET_AAD_PREFIX = b"webhook_secret|"
def _webhook_aad(webhook_id: int) -> bytes:
    if webhook_id <= 0:
        raise ValueError("webhook_id must be a positive integer for AAD")
    return WEBHOOK_SECRET_AAD_PREFIX + str(webhook_id).encode("ascii")
```

Технически конкретное API можно реализовать одним из двух способов (backend-агент выбирает):
1. **Generic-cipher** — расширить `MailPasswordCipher` приёмом `aad: bytes` параметра напрямую, без зашивки prefix внутри. Тогда два helper'а — `encrypt_mail_password(...)`, `encrypt_webhook_secret(...)` — формируют свои AAD строки.
2. **Отдельный `WebhookSecretCipher`** — клонирующий `MailPasswordCipher` с заменой AAD-prefix.

Архитектор рекомендует **вариант 1** (расширить через параметр); это закрывает [TD-?] (никакого тех-долга от копи-пасты) и оставляет один источник истины для шифрования.

**Инвариант (тест):** decrypt с правильным `webhook_id` → plaintext; decrypt с другим `webhook_id` → `InvalidTag` (атакующий не сможет переставить blob между двумя webhook'ами в БД).

**При компрометации ключа `MAIL_ENCRYPTION_KEY`:**
- Все webhook secrets становятся скомпрометированными → массовый `POST /api/webhooks/me/rotate-secret` после ротации ключа (см. процедуру ротации ключа в `06-security.md` §10). Это автоматически решается через `mas-cli reencrypt` (поскольку blob использует общий `version_byte` механизм — см. ADR-0005); webhook secret'ы re-encrypt'ятся вместе со всеми остальными blob'ами.

#### 4.2. One-time-show secret в API response

`POST /api/webhooks/me` и `POST /api/webhooks/me/rotate-secret` возвращают `secret` в plaintext **только** в response этого конкретного запроса. Никакого `GET`-эндпоинта, отдающего secret, нет. Backend никогда не логирует plaintext secret (попадает в structlog redact-list по ключу `secret`, `X-Webhook-Secret`, `secret_plaintext`).

#### 4.3. SSRF protection для URL

Перед POST на webhook URL (и при валидации URL в POST/PATCH endpoint'ах, и в diaptcher'е, и в test-endpoint'е) backend выполняет DNS-резолв и блокирует приватные CIDR — тот же helper, что используется для IMAP/SMTP test (см. `06-security.md` §4):

```
Запрещённые сети:
  IPv4: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 127.0.0.0/8,
        169.254.0.0/16, 0.0.0.0/8, 100.64.0.0/10
  IPv6: ::1/128, fc00::/7, fe80::/10
```

При попадании хотя бы одного резолвленного адреса в запрещённую сеть — отказ с `400 webhook_url_private_ip` (на CRUD-endpoint'ах) или `dead_at` + audit (в dispatcher'е, если cache отравлен между created и dispatch).

**Lexical-parse запрет** (до DNS-резолва): host == `localhost` / `127.0.0.1` / `0.0.0.0` / `[::1]` → отказ.

**Dev override:** `APP_ENV=dev` отключает SSRF-check (как для IMAP/SMTP) — нужно для тестов с локальным mock-receiver'ом.

**Redirect-policy:** `httpx.AsyncClient` создаётся с `follow_redirects=False`. 3xx ответы трактуются как failed (incremennt counter, `dead_at` на ≥10). Цель — не дать receiver'у через redirect обойти SSRF-check. Если в будущем потребуется поддержка redirect — каждый hop должен проходить SSRF-check заново (отдельный ADR).

#### 4.4. Логирование

Все user-controlled значения логируются с осторожностью:
- `webhook.url` — логируется как-есть (это публичный URL, не sensitive); добавляется в structlog event.
- `secret_plaintext` / `X-Webhook-Secret` header — **никогда** не логируется (попадает в redact-list).
- `last_error` в БД хранит receiver-response-excerpt (первые 200 байт `resp.text`); если receiver случайно вернул в body своё secret/токен — это не наша проблема, но мы ограничиваем длину 500 байт суммарно в `response_excerpt`/`last_error`.

### 5. Rate-limit и dead-mark

Сводка лимитов:

| Endpoint | Лимит | Окно | Ключ |
| --- | --- | --- | --- |
| `POST /api/webhooks/me` | 10 | 1 час | `group_id` |
| `PATCH /api/webhooks/me` | 30 | 1 час | `webhook_id` |
| `DELETE /api/webhooks/me` | 10 | 1 час | `webhook_id` |
| `POST /api/webhooks/me/rotate-secret` | 5 | 1 час | `webhook_id` |
| `POST /api/webhooks/me/test` | 10 | 1 час | `webhook_id` (env `WEBHOOK_TEST_LIMIT`) |

**Dead-mark state machine:**

```
[normal] ── 2xx ─────────────────────→ [normal] (last_fired_at=now, consecutive_failures=0)
[normal] ── 4xx (non-408/429) ───────→ [normal] (consecutive_failures += 1, last_error)
[normal] ── consec >= 10 ─────────────→ [dead] (dead_at=now, audit webhook_dead_marked)
[normal] ── 410 Gone ────────────────→ [dead] (dead_at=now, immediate, audit)
[normal] ── 5xx / 408 / 429 / net ────→ [normal] (last_error, no counter; recovery_scan retry)
[dead]   ── PATCH is_active=true ─────→ [normal] (dead_at=NULL, consecutive_failures=0, last_error=NULL)
[dead]   ── successful test ──────────→ [dead] (test НЕ меняет state — это диагностика)
```

`webhook_dispatch` фильтрует `WHERE is_active=true AND dead_at IS NULL` — dead webhook'и пропускаются.

### 6. Payload format — закрепляем как контракт

Схема payload `event="message_tagged"` (см. §2.9) — **публичный контракт**. Любые breaking changes требуют:
1. Нового ADR (`ADR-0023.1+`).
2. Версионирования через поле `event` (например, `message_tagged.v2`) или через `X-Webhook-Schema-Version` header.

Non-breaking добавления (новое опциональное поле) **разрешены без ADR** — это normal evolution.

---

## Consequences

### Positive

- **Универсальный outbound-канал** — закрывает запросы на CRM/Slack/n8n/Zapier/самодельные интеграции без вмешательства разработки в каждый частный кейс.
- **Полное переиспользование инфраструктуры ADR-0022.** Та же модель: queue → APScheduler job → reservation-table с UNIQUE → recovery_scan. Нагрузка на backend-агент уменьшается — реализует знакомый паттерн ещё раз.
- **Изоляция от TG-канала.** Падение Bot API не влияет на webhook'и (отдельная очередь, отдельный диспатчер). И наоборот: receiver'ы webhook'ов могут отказывать массово, TG не страдает.
- **Безопасность secret'а.** AES-GCM с AAD по webhook_id; one-time-show через API; SSRF-защита; redirect-follow disabled; structlog redact.
- **Идемпотентность гарантирована БД** (`UNIQUE(webhook_id, message_id)`), не Redis.
- **Симметрия с TG в семантике «не флудим историей»** — лидер не получает уведомлений о письмах ДО создания webhook'а.
- **Расширяемость payload schema** — добавление новых полей без breaking change.

### Negative / risks

- **Дублирование dispatch-кода с TG-каналом.** `worker.tg_notify_dispatch` и `worker.webhook_dispatch` — 80% общая логика (LPOP батч → per-item try/except → mark-dead / retry). Сознательный выбор: пытаться обобщить в `GenericDispatcher` сейчас, не имея третьего канала, — преждевременная абстракция. Если в будущем появится третий канал (например, push в браузер через web-push) — рассмотреть рефакторинг в отдельном ADR.
- **Receiver-side rate-limiting вне нашего контроля.** Если receiver медленный — мы будем mark-dead его при 5xx-storm'ах через 24 ч; лидер должен сам разбираться. Backend выставляет `consecutive_failures` и `dead_at` корректно, но diagnostic UI на `/my/integrations` обязан их показать (frontend-агент).
- **Один webhook на команду — может быть тесно** для крупных команд, которые хотят отдельные интеграции для disputes vs subscriptions. Решение в MVP — receiver сам диспетчеризует по `tags[]` в payload. Если поступит явный запрос — multiple webhooks per team — отдельный ADR (см. [Alternative 1](#alternatives-considered)).
- **Body truncate 16 KiB** может потерять важный контент в длинных письмах (например, цитированные threads). Соглашаемся: receiver'у не нужно полное тело — это нотификация, не передача данных; для full-message receiver вызывает наш API.
- **Compromise scenario: receiver leak secret в свои логи** → атакующий может слать поддельные события на receiver. Mitigation: ротация secret через `rotate-secret`. Receiver-side сам отвечает за защиту своих логов.
- **`group_leader` не видит чужие команды через API** — это ОК (изоляция). Но super_admin при работе с `?group_id=` должен явно передавать параметр на каждом запросе; **отдельной admin-страницы со списком всех webhook'ов системы НЕТ** в MVP (super_admin может через psql). См. [Q-WH-2](#open-questions).

### Migration plan

1. **Миграция `005_outbound_webhooks.py`** (Alembic):
   ```sql
   CREATE TABLE webhooks (...);
   CREATE TABLE webhook_deliveries (...);
   CREATE INDEX webhooks_active_idx ON webhooks(is_active) WHERE is_active = TRUE;
   CREATE INDEX webhook_deliveries_webhook_id_idx ON webhook_deliveries(webhook_id);
   CREATE INDEX webhook_deliveries_message_id_idx ON webhook_deliveries(message_id);
   -- Триггер BEFORE UPDATE ON webhooks для updated_at.
   ```
2. **Никакой data-миграции** — обе таблицы стартуют пустыми. Первые записи появятся:
   - `webhooks` — после первого `POST /api/webhooks/me` от лидера.
   - `webhook_deliveries` — после первого письма с тегами в команде, у которой настроен webhook.
3. **`backend/app/audit/service.py`**: расширить `ALLOWED_ACTIONS` на 4 новых action'а (см. §«Audit events» ниже).
4. **`shared/crypto.py`**: расширить API на `aad: bytes`-параметр (вариант 1 из §4.1). Добавить helper `encrypt_webhook_secret(plaintext: str, webhook_id: int) -> bytes` + `decrypt_webhook_secret(blob, webhook_id) -> str`.
5. **`shared/config.py`**: добавить env-переменные:
   - `WEBHOOK_DISPATCH_INTERVAL_SECONDS=5`
   - `WEBHOOK_RECOVERY_INTERVAL_SECONDS=3600`
   - `WEBHOOK_RECOVERY_WINDOW_HOURS=24`
   - `WEBHOOK_BATCH_SIZE=30`
   - `WEBHOOK_HTTP_TIMEOUT_SECONDS=10`
   - `WEBHOOK_MAX_FAILURES_BEFORE_DEAD=10`
   - `WEBHOOK_TEST_LIMIT=10`
6. **Backend implementation** (см. §«Implementation plan» ниже).
7. **Frontend implementation**: `templates/my/integrations.html`, ссылка «Интеграции» в topnav для `group_leader` / `super_admin`.
8. **`worker.sync_cycle`**: добавить try/except-блок `WebhookDispatchService.enqueue_message_ids(notified_message_ids)` после существующего TG-блока. Симметрично, без вмешательства в TG-логику.
9. **`worker/app/main.py`**: зарегистрировать `webhook_dispatch` и `webhook_recovery_scan` jobs (после `_safe_tg_notify_*`).
10. **DevOps**: на prod применить миграцию автоматически при следующем `docker compose up -d`; новые env-переменные опциональны (default'ы достаточно).

---

## Alternatives considered

1. **Multiple webhooks per team** (отвергнуто).
   - Pro: гибкость, разные subscribers на разные tag'и.
   - Contra: усложняет config UI (CRUD списка), фрагментирует payload (нужно мечать tag→webhook), нет явного запроса.
   - **Решение:** один webhook на команду (UNIQUE group_id); receiver сам диспетчеризует по `tags[]` в payload. Если появится явный запрос — отдельный ADR с поддерживаемым data-migration (split UNIQUE → drop, добавить таблицу `webhook_tag_filters`).

2. **HMAC-signature вместо static secret** (отвергнуто на MVP).
   - Pro: secret не уходит «по проводу» — receiver валидирует подпись body+timestamp; replay-resistance.
   - Contra: усложняет интеграцию (receiver должен пересчитывать HMAC; многие no-code платформы не умеют). Пользователь явно выбрал static secret.
   - **Решение:** static `X-Webhook-Secret`. Если будет явный запрос — добавить параллельно `X-Webhook-Signature-256` header (Stripe-style) в отдельном ADR-0023.1 без breaking change.

3. **Push-vs-poll: receiver polls our API вместо нас push'им** (отвергнуто).
   - Pro: receiver сам решает, когда забирать.
   - Contra: latency (poll-interval, обычно 5–60 сек); требует от receiver'а долговечной сессии (наш Auth — cookie-based, не API-key); полностью перекладывает rate-limit на нашу сторону. Не соответствует семантике «push-нотификации».
   - **Решение:** push.

4. **Inline в `worker.sync_cycle.save_message`** вместо очереди (отвергнуто).
   - Pro: меньше движущихся частей.
   - Contra: receiver-side timeout 10s блокирует IMAP cycle; falling receiver валит sync. Те же причины, что для TG (ADR-0022 §2.1).
   - **Решение:** APScheduler job + Redis queue, копия паттерна TG.

5. **Postgres-based queue вместо Redis** (отвергнуто).
   - Pro: атомарность с insert messages, видна в psql.
   - Contra: VACUUM/index-bloat при тысячах INSERT/DELETE; усложняет диспатчер (нужно SELECT FOR UPDATE SKIP LOCKED). Идемпотентность всё равно через таблицу — а Redis дешевле для FIFO.
   - **Решение:** Redis list (тот же паттерн, что и TG).

6. **Один Redis-list `notify_queue` для TG и webhook'ов** с message_id, разные consumer'ы (отвергнуто).
   - Pro: SSO для disptach: один LPUSH в sync_cycle.
   - Contra: нельзя независимо drainить (BRPOP вытащит элемент только один раз; нужны два queue или fan-out). Усложняет mental model.
   - **Решение:** два независимых queue (`tg_notify_queue` + `webhook_dispatch_queue`). Sync_cycle делает два LPUSH в try/except blocks.

7. **Хранить secret в plain в БД** (отвергнуто).
   - Contra: компрометация БД даёт всё. Уже есть `MailPasswordCipher` инфраструктура — переиспользуем.
   - **Решение:** AES-256-GCM + AAD по webhook_id.

8. **Retry с exponential backoff per-message** вместо recovery_scan (отвергнуто).
   - Pro: быстрее доставка при transient.
   - Contra: усложняет state (нужно tracking `next_retry_at`); recovery_scan покрывает повторную доставку 5xx через час, чего достаточно для MVP. Этот же подход TG. Если в будущем потребуется sub-минутная доставка после transient — отдельный ADR.

9. **Подписка на не-tagged-письма тоже** (отвергнуто).
   - Contra: спам receiver'у; webhook должен быть «событийным» — события в нашей системе это tagged-письма. Receiver'у не нужно «пришло письмо» — нужно «пришло важное письмо».
   - **Решение:** только tagged. Симметрично TG (ADR-0022).

10. **Отдельный admin endpoint `/api/admin/webhooks/{group_id}` для super_admin** (отвергнуто как избыточный).
    - Pro: чище разделение.
    - Contra: дублирует /api/webhooks/me с query-param; больше route definitions.
    - **Решение:** super_admin использует `?group_id=` поверх `/api/webhooks/me/*`. Backend dependency resolve'ит scope.

---

## Implementation plan

### A. Backend (FastAPI)

**Создаются:**

- `migrations/versions/005_outbound_webhooks.py` — DDL из §1.
- `shared/models/webhook.py` (или внутри `backend/app/models/`): ORM `Webhook` + `WebhookDelivery`.
- `backend/app/repositories/webhooks.py`:
  ```python
  class WebhooksRepo:
      async def get_by_group_id(group_id: int) -> Webhook | None
      async def create(group_id: int, url: str, secret_encrypted: bytes) -> Webhook
      async def reserve_id() -> int                          # nextval('webhooks_id_seq')
      async def insert_with_explicit_id(id_: int, group_id: int, url: str, secret_encrypted: bytes) -> Webhook
      async def update_url(webhook_id: int, url: str) -> Webhook
      async def update_secret(webhook_id: int, secret_encrypted: bytes) -> Webhook
      async def set_active(webhook_id: int, is_active: bool) -> Webhook  # при is_active=true сбрасывает dead_at + counters
      async def delete(webhook_id: int) -> None
      async def mark_dead(webhook_id: int, reason: str) -> None
      async def mark_success(webhook_id: int) -> None        # last_fired_at=now, consec=0, last_error=NULL
      async def bump_failures_and_set_last_error(webhook_id: int, last_error: str) -> int  # returns new count
      async def touch_last_error(webhook_id: int, last_error: str) -> None  # не инкрементит counter
      async def find_active_for_message(mail_account_id: int, group_id: int, message_id: int) -> WebhookRecipient | None
      # SQL §3.2: учитывает is_active, dead_at, history-filter, EXISTS(tags)
  ```
- `backend/app/repositories/webhook_deliveries.py`:
  ```python
  class WebhookDeliveriesRepo:
      async def try_reserve(webhook_id: int, message_id: int) -> int | None  # INSERT ... ON CONFLICT DO NOTHING RETURNING id
      async def mark_sent(delivery_id: int, status: int, excerpt: str) -> None  # sent_at=now, response_code, response_excerpt
      async def mark_failed(delivery_id: int, status: int, excerpt: str) -> None  # тоже sent_at=now + response_code/excerpt; маркер «отказались»
      async def rollback(delivery_id: int) -> None           # DELETE WHERE id=…
      async def list_tags_for_team(message_id: int, group_id: int) -> list[TagDTO]  # SQL §3.2 второй query
      async def list_missing_for_recovery(threshold_at: datetime, limit: int = 5000) -> list[int]  # recovery_scan SQL
  ```
- `backend/app/webhooks/` модуль:
  - `__init__.py`
  - `schemas.py` (Pydantic):
    ```python
    class WebhookCreate(BaseModel):
        url: HttpUrl  # принудительно https; pydantic_extra_types для tighter validation

    class WebhookUpdate(BaseModel):
        url: HttpUrl | None = None
        is_active: bool | None = None

    class WebhookDTO(BaseModel):       # для GET / PATCH / DELETE response — БЕЗ secret
        id: int
        group_id: int
        url: str
        is_active: bool
        last_fired_at: datetime | None
        last_error: str | None
        dead_at: datetime | None
        consecutive_failures: int
        created_at: datetime
        updated_at: datetime

    class WebhookCreatedDTO(WebhookDTO):  # для POST + rotate-secret response
        secret: str                        # plaintext, one-time-show
    ```
  - `service.py`:
    ```python
    class WebhooksService:
        async def create_for_scope(scope: VisibilityScope, url: str) -> WebhookCreatedDTO
        async def get_for_scope(scope: VisibilityScope, group_id: int | None = None) -> WebhookDTO  # 404 if missing
        async def update_for_scope(scope: VisibilityScope, url: str | None, is_active: bool | None) -> WebhookDTO
        async def delete_for_scope(scope: VisibilityScope) -> None
        async def rotate_secret(scope: VisibilityScope) -> WebhookCreatedDTO  # возвращает new secret
        async def send_test(scope: VisibilityScope) -> TestResultDTO
    ```
  - `dispatch_service.py`:
    ```python
    class WebhookDispatchService:
        async def enqueue_message_ids(message_ids: list[int]) -> int
            # 1. Pre-filter: SELECT message_ids → mail_accounts → groups, drop те, у которых нет активного webhook'а.
            # 2. LPUSH webhook_dispatch_queue values=[json({"message_id": mid})...]
            # Return count pushed.
        async def dispatch_one_payload(message_id: int) -> DispatchOneResult
            # Алгоритм §3.4. Возвращает {kind: 'sent'|'skipped_idempotent'|'rolled_back'|'marked_dead'|'mark_failed'|'no_recipient'}
    ```
  - `router.py`:
    - `GET /my/integrations` (HTML, render template).
    - `GET /api/webhooks/me` / `POST /api/webhooks/me` / `PATCH /api/webhooks/me` / `DELETE /api/webhooks/me`.
    - `POST /api/webhooks/me/rotate-secret`.
    - `POST /api/webhooks/me/test`.
    - Sibling form-fallback routes (по ADR-0015):
      - `POST /api/webhooks/me/delete` + `_method=DELETE`.
      - `POST /api/webhooks/me` (без `_method`) — на create (form-encoded body).
      - `POST /api/webhooks/me/update` + `_method=PATCH` ИЛИ `POST /api/webhooks/me` + `_method=PATCH` (выбрать единый паттерн, см. accounts/tags) — backend-агент сверяется с существующими form-fallback паттернами.

**Изменяется:**

- `backend/app/main.py` — подключить `webhooks.router`.
- `backend/app/audit/service.py` — расширить `ALLOWED_ACTIONS`:
  ```python
  "webhook_created",
  "webhook_updated",
  "webhook_deleted",
  "webhook_secret_rotated",
  "webhook_dead_marked",
  ```
- `backend/app/rate_limit.py` — добавить slowapi-лимиты `LIMIT_WEBHOOK_TEST` (`10/h per webhook_id`) + остальные из §5.
- `backend/app/templates/base.html` — в `topnav` для `group_leader` и `super_admin` ссылка «Интеграции» → `/my/integrations`. Bottom-nav: при наличии — добавить «Интеграции» (frontend-агент решает, как утрамбовать с уже существующими 4-5 пунктами).
- `shared/crypto.py`:
  - Добавить `WEBHOOK_SECRET_AAD_PREFIX = b"webhook_secret|"`.
  - Расширить `MailPasswordCipher.encrypt/decrypt` приёмом параметра `aad: bytes` (или ввести второй метод `encrypt_with_aad(plaintext, aad)`); если уже есть `aad`-параметр — переиспользовать.
  - Добавить helper `encrypt_webhook_secret(plaintext, webhook_id) -> bytes` + `decrypt_webhook_secret(blob, webhook_id) -> str`.
- `shared/config.py`:
  - `WEBHOOK_DISPATCH_INTERVAL_SECONDS: int = 5`
  - `WEBHOOK_RECOVERY_INTERVAL_SECONDS: int = 3600`
  - `WEBHOOK_RECOVERY_WINDOW_HOURS: int = 24`
  - `WEBHOOK_BATCH_SIZE: int = 30`
  - `WEBHOOK_HTTP_TIMEOUT_SECONDS: int = 10`
  - `WEBHOOK_MAX_FAILURES_BEFORE_DEAD: int = 10`
  - `WEBHOOK_TEST_LIMIT: int = 10`

### B. Worker (APScheduler)

- `worker/app/webhook_dispatch.py` — новый файл с `webhook_dispatch()` (см. §3.3).
- `worker/app/webhook_recovery.py` — новый файл с `webhook_recovery_scan()` (см. §3.5).
- `worker/app/sync_cycle.py` — после `tg_notify_service.enqueue_message_ids(...)` (строки 314–328) добавить аналогичный блок `WebhookDispatchService.enqueue_message_ids(notified_message_ids)` с тем же try/except паттерном. См. §3.1.
- `worker/app/main.py` — зарегистрировать `webhook_dispatch` (interval 5s, max_instances=1, coalesce=True) и `webhook_recovery_scan` (interval 1h, max_instances=1, coalesce=True). Wrap в `_safe_webhook_dispatch` / `_safe_webhook_recovery` (тот же error-isolation паттерн, что у `_safe_tg_notify_*`).

### C. Shared (`shared/`)

- `shared/crypto.py` — расширение API (см. §B/A выше).
- `shared/config.py` — новые env (см. §A).

### D. Database migrations

- `005_outbound_webhooks.py` — DDL §1.

### E. Frontend

- `backend/app/templates/my/integrations.html` — новый шаблон. Сетка: URL input + form-actions (Save, Rotate, Test, Delete) + status table (last_fired_at, last_error, consecutive_failures, dead-indicator).
- Никакого нового JS-файла — всё через form-fallback (ADR-0015). Если frontend-агент решит улучшить UX — отдельный JS файл `static/js/integrations.js` с fetch-вариантом для AJAX-rotate / AJAX-test (опционально).
- Модальное окно для показа secret один раз — server-side через специальную flash-категорию `secret_reveal` (один-shot). Frontend-агент при рендере `integrations.html` ищет `flashes[?category=='secret_reveal']` и показывает секцию `<aside class="secret-reveal">` с copyable `<code>` блоком + кнопкой `Скрыть` (POST на noop endpoint или JS-removal).

### F. QA-инварианты (handover в qa)

- Создание webhook → audit `webhook_created`; response содержит `secret` plaintext; GET `/api/webhooks/me` НЕ возвращает `secret`.
- Rotate-secret → новый secret в response; старый decrypt с тем же `webhook_id` всё ещё работает в БД до COMMIT; после COMMIT — только новый. Audit `webhook_secret_rotated`.
- Создание webhook с `https://localhost` → 400 `webhook_url_private_ip`.
- Создание с DNS-резолвом в `10.0.0.1` → 400 (через resolver-mock).
- Создание с `http://...` (не https) → 400 `validation_error`.
- Сообщение с тегом → POST на webhook URL с правильным payload + header `X-Webhook-Secret`.
- Сообщение с `internal_date < webhook.created_at` → POST НЕ выполняется (history-filter).
- Сообщение без тегов → POST НЕ выполняется.
- Сообщение в чужой группе (другой `mail_accounts.group_id`) → POST НЕ выполняется.
- Повторный sync_cycle того же message → POST НЕ повторяется (UNIQUE webhook_id+message_id).
- Receiver 200 → row в `webhook_deliveries` с `sent_at, response_code=200`. `last_fired_at` обновлён.
- Receiver 5xx → row deleted (rollback); recovery_scan через час подберёт. `consecutive_failures` НЕ инкрементится.
- Receiver 4xx (404) × 10 раз подряд → `dead_at` выставлен, audit `webhook_dead_marked`.
- Receiver 410 → немедленный `dead_at`.
- PATCH `is_active=true` на dead → `dead_at=NULL, consecutive_failures=0, last_error=NULL`.
- `POST /api/webhooks/me/test` → POST на receiver с payload `event="test"`; ответ возвращён в API response; `webhook_deliveries` row НЕ создан.
- Test rate-limit: 11-й test в час → 429.
- super_admin может работать с любым webhook через `?group_id=<id>`; group_leader другой группы → 403; group_member → 403.
- `DELETE /api/admin/groups/{id}` (если группа пустая) → CASCADE удалит webhook + webhook_deliveries.
- DELETE message (retention) → CASCADE удалит webhook_deliveries row.
- Compromise: расшифровка `secret_encrypted` с другим `webhook_id` → `InvalidTag`.
- Логи: `X-Webhook-Secret` / `secret` НЕ появляются в structlog (redact-test).

### G. Audit events

Расширение `admin_audit.action` enum:

| action | actor_user_id | target_user_id | details |
| --- | --- | --- | --- |
| `webhook_created` | вызвавший (leader или super_admin) | leader группы (или `null` для super_admin self) | `{group_id, webhook_id, url}` |
| `webhook_updated` | вызвавший | leader группы | `{webhook_id, changed_fields: [...], previous_dead_at: ts\|null}` |
| `webhook_deleted` | вызвавший | leader группы | `{webhook_id, group_id, url}` |
| `webhook_secret_rotated` | вызвавший | leader группы | `{webhook_id}` |
| `webhook_dead_marked` | `0` (system) или actor user_id | leader группы | `{webhook_id, reason: '410_gone'\|'consecutive_4xx'\|'secret_decrypt_failed'\|...}` |

`actor_user_id=0` для system-action — backend-агент проверяет, не сломает ли это существующий FK/INDEX (у `admin_audit.actor_user_id` нет FK по дизайну ADR-0019, value `0` приемлемо; альтернатива — лидер команды в качестве actor'а, потому что доставка инициирована его настройкой; backend-агент выбирает с учётом существующих TG-аналогов: `telegram_link_dead_marked` пишется с `actor_user_id = user.id` пострадавшего user'а — то же сделать для `webhook_dead_marked` (`actor = leader_user_id`)).

---

## Open questions

| ID | Где задан | Кратко | Статус |
| --- | --- | --- | --- |
| Q-WH-1 | этот ADR §2.5 | «Двойной secret» (старый valid M минут после rotate)? Сейчас rotate — instant cut; receiver-side rotation требует downtime в обработке. | open — отложено в `100-known-tech-debt.md` как **TD-019** (нет реального запроса; rotation редкая, координируется ручно). |
| Q-WH-2 | этот ADR §«Consequences» | UI для super_admin — список всех webhook'ов всех команд? | open — в MVP super_admin использует `?group_id=` per request или psql; full UI — следующая итерация (если будет запрос). |
| Q-WH-3 | этот ADR §2.9 | Включать ли список attachment'ов (id, filename, size) в payload (без bytes)? | open — отложено. Receiver получает информацию через наш API (auth'd сессия). Если поступит запрос — non-breaking add `attachments: [...]` поле. |
| Q-WH-4 | этот ADR §4.1 | Compromise mode: если MAIL_ENCRYPTION_KEY ротируется (см. ADR-0005), пересохраняются ли webhook secrets автоматически (через mas-cli reencrypt)? | **closed by this ADR**: да, переиспользуем общий `version_byte` механизм — `mas-cli reencrypt` обрабатывает все blob'ы с `version_byte=0x00` (старый key) → `0x01` (новый key) независимо от AAD-домена. Backend-агент при реализации добавит `webhooks` в список таблиц `reencrypt`-команды. |

Q-WH-1 закрывается через TD-019 (см. `100-known-tech-debt.md` обновление).

---

## Cross-references

- `03-data-model.md` — две новые таблицы `webhooks`, `webhook_deliveries` (DDL + каскады + индексы + объёмные оценки); расширение `admin_audit.action` enum.
- `04-api-contracts.md` — новый раздел 4b «Outbound webhooks»: `GET/POST/PATCH/DELETE /api/webhooks/me`, `POST /api/webhooks/me/rotate-secret`, `POST /api/webhooks/me/test`, HTML `/my/integrations`; новые error-codes `webhook_url_private_ip`, `webhook_already_exists`.
- `05-modules.md` — новый модуль 19 «webhooks»: schemas, service, dispatch_service, repository, router; новая секция 14.2 «worker — webhook_dispatch + webhook_recovery_scan».
- `06-security.md` — расширение §2 (`secret_encrypted` через AES-GCM с AAD=`webhook_id`); расширение §4 (SSRF check для webhook URL); новая секция 1.10 (STRIDE для outbound webhook'ов); §7 (rate-limits для `/api/webhooks/me/*`); §8 (новые audit-actions).
- `100-known-tech-debt.md` — новый item TD-019 «Двойной secret при rotation» (Q-WH-1 закрытие).
- ADR-0005 — переиспользуется AES-256-GCM + version_byte + AAD; reencrypt-механизм покрывает `webhooks.secret_encrypted`.
- ADR-0009 — переиспользуется slowapi rate-limit.
- ADR-0010 — все state-changing webhook endpoints под CSRF.
- ADR-0011 — `webhook_deliveries` каскадно очищается через `messages.id ON DELETE CASCADE` при retention.
- ADR-0015 — все webhook endpoint'ы поддерживают form-fallback.
- ADR-0017 — теги (триггер для webhook); webhook'и не модифицируют tag-flow.
- ADR-0019 — visibility model; `group_leader` scope; `super_admin` через `?group_id=` override.
- ADR-0022 — параллельный канал доставки (TG-нотификации); webhook'и НЕ заменяют TG; sync_cycle делает оба LPUSH независимо.
