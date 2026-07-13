# ADR-0043 — Агрегатор → чистый mail-connector: push нового письма в CRM, снятие тегов/Telegram/groups/webhooks/forwarding/UI/MinIO

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-07-10 |

**Отменяет курс** headless-прокси-интеграции: агрегатор перестаёт быть носителем тегов/Telegram/групп/webhooks/forwarding/UI и становится **тонким mail-connector'ом** (подключение ящиков, IMAP-поллинг, SMTP-отправка, **push нового письма в CRM**). Парный ADR в CRM — `ADR-044` (полный перенос модуля «Почты» в CRM). Решение владельца продукта (дословно — см. CRM `ADR-044` Context).

**Supersedes / отменяет решения:** `ADR-0017`/`ADR-0040` (теги), `ADR-0022`/`ADR-0024` (Telegram SSO + нотификации + мульти-линк), `ADR-0027` (push-боты по командам), `ADR-0033` (mailbox-down TG-алерт), `ADR-0023` (outbound webhooks), `ADR-0034` (leader forwarding), `ADR-0019`/`ADR-0030`/`ADR-0031` (роли/группы/multi-group/team-selection ящика), `ADR-0037`/`ADR-0042` (external teams + tags read/write в части групп/тегов), `ADR-0018` (telegram launcher). **Дополняет/сужает** `ADR-0039` (external write API — раздел tags/teams снимается, mailbox-write + новый send остаётся), `ADR-0041` (headless — демонтаж UI финализируется). Реализация — по спринтам CRM `ADR-044`.

## Context

Агрегатор сегодня — полнофункциональный сервис: IMAP/SMTP + теги + Telegram (SSO, нотификации, 4 push-бота, mailbox-down алерт) + webhooks + forwarding + группы/роли/пользователи + Jinja-UI + Mini App + MinIO-вложения. CRM интегрировался с ним как headless-прокси (внешний read+write API).

**Владелец решил:** агрегатор остаётся **только** сервисом «добавления почт»; команды, теги, пользователи, Telegram-уведомления, фронт — **всё переезжает в CRM**. Агрегатор сохраняет лишь движок: подключение ящиков (креды, шифрование), синк, отправку, и **сам присылает новое письмо в CRM push'ем** (не CRM поллит). Вложения в CRM не нужны.

## Decision

### §1. Что агрегатор ОСТАВЛЯЕТ (движок connector'а)

- `mail_accounts` (IMAP/SMTP-креды, AES-256-GCM шифрование `ADR-0005`, OAuth-Outlook `ADR-0025`/`ADR-0028`), sync-worker (UIDNEXT-инкремент `ADR-0008`, circuit-breaker/transient `ADR-0026`), SMTP-отправка.
- `messages` — **переводится в роль push-outbox** (рабочий буфер): sync по-прежнему вставляет письма (идемпотентно, UNIQUE `(mail_account_id, uidvalidity, uid)`), но их назначение — быть отправленными в CRM и подчищаться ретенцией (`ADR-0011`, 30 дней). CRM — durable system of record.
- External управляющий API для CRM: `POST/PATCH/DELETE /api/external/mailboxes*`, `test`, `sync` (`ADR-0039`, mailbox-раздел) + **новый обобщённый send** (§3).
- Health-роутер (`/healthz`,`/readyz`).

### §2. Push нового письма в CRM (агрегатор — инициатор)

**Схема колонки-outbox:** `messages += pushed_at TIMESTAMPTZ NULL` (новая alembic-ревизия; `NULL` = ещё не доставлено в CRM). Прочие «hook»-поля синка (tag-apply, notify-enqueue) — удаляются (§4).

**Диспетчер (реюз паттерна `tg_notify_dispatch`/`tg_notify_recovery`):**
- **Enqueue:** в `worker/app/sync_cycle.py` после COMMIT новых писем — `LPUSH crm_push_queue` их `message_id` (заменяет прежние LPUSH в `tg_notify_queue`/`push_notify_queue`/webhooks/forwarding).
- **`crm_push_dispatch`** (APScheduler, интервал ~5с, `max_instances=1, coalesce=True`): `LPOP crm_push_queue count=CRM_PUSH_BATCH_SIZE` (default 100) → загрузить письма → **`POST {CRM_INGEST_URL}/api/mail/ingest`** батчем → `2xx` → `UPDATE messages SET pushed_at=now()`; ошибка (CRM 5xx/сеть/таймаут) → item остаётся неотправленным (recovery подберёт) / ре-enqueue.
- **`crm_push_recovery`** (hourly): ре-enqueue писем `pushed_at IS NULL AND fetched_at > now()-interval` (защита от потери при рестарте между sync и push; окно ≤ ретенции).

**Контракт (нормативно, зеркалит CRM `ADR-044` §3):**
- Метод/путь: `POST /api/mail/ingest` на стороне CRM.
- **Аутентификация — HMAC-SHA256.** Заголовки `X-Mail-Signature: sha256=<hex>`, `X-Mail-Timestamp: <unix>`. **Каноническая форма — байтами, ИДЕНТИЧНО CRM `ADR-044` §3 (f-string над `bytes` ЗАПРЕЩЁН — даёт `repr` `b'...'`, а не байты):**
  ```python
  mac_input = str(timestamp).encode("ascii") + b"." + raw_body_bytes
  signature = hmac.new(secret.encode("utf-8"), mac_input, hashlib.sha256).hexdigest()
  ```
  `raw_body_bytes` — сырое тело до сериализации, разделитель `b"."`. Ключ — общий секрет **`CRM_PUSH_SECRET`** (= CRM `MAIL_PUSH_SECRET`; env, класс секретов, не в логах/URL). Timestamp-окно (CRM отвергает skew > 300с) **ограничивает окно валидности, но не является полным анти-replay** (без nonce); повтор в пределах окна гасится идемпотентностью приёмника — см. CRM `ADR-044` §3.
- Тело: `{ "messages": [ { mail_account_id, uidvalidity, uid, message_id_header, subject, from_addr, from_name, to_addrs, cc_addrs, internal_date, body_text, body_html, in_reply_to, refs_header } ] }`. **Вложений НЕТ.**
- Идемпотентность — на стороне CRM (`ON CONFLICT (mail_account_id, uidvalidity, uid) DO NOTHING`); повтор push (ретрай/recovery) безопасен.
- Ответ `2xx` ⇒ агрегатор ставит `pushed_at` и не ре-пушит.
- **Порядок доставки не гарантируется** — CRM присваивает свой `id`; лента CRM сортируется по `internal_date` (CRM `ADR-044` §2), не по порядку прихода.
- **`mail_account_id`** — тот же int, что CRM хранит как `mail_accounts.id` (создаётся через CRM, §CRM `ADR-044` §4).

**Status-канал ящика (нормативно — питает mailbox-down алерт CRM `ADR-0033`→CRM `ADR-044` §3/§6).** На проде функция живая: 7 ящиков `is_active=false`, 2 оталерчены. Агрегатор на **изменение статуса синка** ящика (успех/ошибка/авто-disable) POST'ит **`POST {CRM_INGEST_URL}/api/mail/mailbox-status`** (тот же HMAC) `{ mail_account_id, is_active, last_synced_at, last_sync_error, consecutive_failures }`. Идемпотентность «ровно один алерт на переход» — **на стороне CRM** (`mail_accounts.down_alert_sent_at`), поэтому агрегатор может слать статус на каждый цикл без дедупа. **Точки hook — исчерпывающий перечень H1–H6/H7a/H7b (и не-hook точки N1–N7) вынесен в [ADR-0046](./ADR-0046-mailbox-status-hook-points.md) §3/§4** (прежняя формулировка «`mark_sync_success`/`_disable_after_failures`/`mark_transient_error`» была **неполна**: не названы `mark_sync_failure`, re-enable/`set_active`, `oauth_needs_consent`). Все восемь точек **реализованы** (коммит `e7c7b52`) и зовут единый хелпер `enqueue_crm_status_best_effort` (`backend/app/crm_push/service.py:187`) **строго после COMMIT** (инвариант `ADR-0046` §2; в HTTP-слое — через deferred-flush `ADR-0046` §2.1: сервис копит id, роутер флашит вне `db.begin()`). Там же (§1) нормативная семантика `last_synced_at` = **время последней УСПЕШНОЙ синхронизации** (единственный писатель — `mark_sync_success`). **Миграция:** значение `mail_accounts.disabled_alert_sent_at` (2 ящика) экспортируется в CRM `down_alert_sent_at` при bulk-copy (иначе повторный алерт). Собственный агрегаторский `mailbox_alert_dispatch` — **снимается** (§4).

### §3. Обобщённый SMTP-send для reply/forward из CRM

Письма теперь в CRM; message-scoped reply `ADR-0035` (`POST /api/external/messages/{id}/reply`, требующий хранимого письма для threading/scope) заменяется на **обобщённый** эндпоинт:
- **`POST /api/external/mailboxes/{id}/send`** (под `EXTERNAL_WRITE_ENABLED` + `LIMIT_EXTERNAL_WRITE`, auth-flow `ADR-0039` §1): тело `{ to: string[], cc?: string[], subject?, body_text, in_reply_to?, refs? }` → SMTP-отправка кредами ящика `{id}` (reuse SMTP-ядра `ADR-0034` §5/`ADR-0035`) → `200 { sent_id, smtp_message_id }`. **Нормы валидации переносятся из `ADR-0035` (не теряются):** каждый адрес `to+cc` — валидный e-mail, суммарно ≤100; `subject` ≤998; `body_text` непустой, ≤1 MiB → иначе `400/422`. Threading-заголовки формирует CRM. Коды `200/400/401/403/404 (mailbox not found)/409/422/502`. Применяется для reply и forward (CRM `ADR-044` §8).
- `POST /api/external/messages/{id}/reply` (`ADR-0035`) — **снимается** (message-хранилище как источник threading уходит в CRM). `EXTERNAL_REPLY_ENABLED` → выводится из употребления. **Rate-limit:** прежний per-IP `EXTERNAL_REPLY_RATE_LIMIT` больше не нужен — reply инициирует CRM под JWT/RBAC пользователя (CRM `ADR-044` §8), апстрим-send идёт под `LIMIT_EXTERNAL_WRITE` как машинный; сдвиг границы осознан (аноним-IP → JWT-юзер).

### §4. Что агрегатор УБИРАЕТ

Демонтируются (код + таблицы + миграции-drop + env + worker-jobs + docs-разделы):
- **Теги** (`ADR-0017`/`ADR-0040`): `tags`/`tag_rules`/`message_tags`, `backend/app/tags/*`, sync-hook `apply_tags_to_message`, external `/api/external/tags*`. **Логика матчинга переносится в CRM ПОБУКВЕННО** (CRM `ADR-044` §5 берёт `backend/app/tags/sql.py` как есть, минус visibility-ветки).
- **Telegram** (`ADR-0018`/`ADR-0022`/`ADR-0024`/`ADR-0027`/`ADR-0033`): `telegram_links`/`telegram_notifications`/`users_settings`(tg-часть), `backend/app/telegram/*`, SSO, роутеры `/api/telegram/*`, все очереди/jobs (`tg_notify_dispatch`/`recovery`, `push_notify_dispatch`, `mailbox_alert_dispatch`), Mini App. **5 ботов переключают вебхуки на CRM** (CRM `ADR-044` §9; токены те же).
- **Webhooks** (`ADR-0023`): `webhooks`/`webhook_deliveries`, `webhook_dispatch`/`recovery`. В проде 0 webhooks — переносить нечего.
- **Forwarding** (`ADR-0034`): `group_forwarding`/`message_forwards`, `forward_dispatch`. Снимаются. **Функция в CRM ОТЛОЖЕНА** (владелец, CRM `ADR-044` §8/CRM `TD-040`): таблицы CRM не созданы, **1 правило прода НЕ мигрируется** — с cut-over пересылка не работает до реализации.
- **Группы/роли/пользователи** (`ADR-0019`/`ADR-0030`/`ADR-0031`): `groups`/`user_groups`, роли, human-users. `mail_accounts.group_id` **удаляется** (колонка + FK). `mail_accounts.user_id` → все указывают на единственного служебного **`crm-service`** (NOT NULL сохраняется; `users`-таблица оставляет только `crm-service`-ряд; `UNIQUE(user_id,email)` → эффективно глобальная уникальность email — штатно для headless). Владение ящиком командой — **только в CRM**.
- **External teams/tags** (`ADR-0037`/`ADR-0039`/`ADR-0042`): `GET /api/external/teams`, `POST/DELETE /api/external/teams`, `GET/POST/PATCH/DELETE /api/external/tags*`, фильтр `group_id` в `GET /messages`/`GET /mailboxes` — **снимаются** (групп/тегов в агрегаторе нет).
- **Pull-эндпоинты `GET /api/external/messages` и `GET /api/external/mailboxes` — ОСТАВЛЯЕМ** (решение владельца): нужны для **сверки после миграции и диагностики**, а `GET /mailboxes` — ещё и как reconcile-fallback status-канала (CRM `ADR-044` §3 вариант B). **Снимаются после успешного cut-over** (не на этапе strip). Фильтр `group_id` из них убирается (групп нет); прочие параметры/DTO — без изменений.
- **Вложения/MinIO** (`ADR-0007`): `attachments`/`sent_attachments`, `shared/storage.py`, скачивание в sync, S3-env, сервис MinIO из compose (devops). Существующие 646 вложений — удаляются при decommission. Sync перестаёт качать вложения.
- **`admin_audit` (MAJOR-3, 248 записей, период 2026-05-14…2026-07-10) — НЕ переносится в CRM by design** (у CRM аудит **лог-based** через structlog, БД-таблицы-аналога нет). Журнал остаётся в БД агрегатора read-only, доступен до decommission; **при decommission — дамп в бэкап** для ретенции (не теряется молча). Заведено как `TD-050`.
- **`users_settings` (opt-out, MAJOR-4): на проде 0 строк** — переносить нечего. Механизм отписки в агрегаторе (`PATCH /api/me/settings`) снимается; CRM-аналог (`PATCH /api/mail/me/settings`) — в scope CRM `ADR-044` §2.
- **Jinja-UI/static** (`ADR-0041` финал): демонтаж завершается.
- `sent_messages` — durable-запись отправленного уходит в CRM (`mail_sent_messages`); в агрегаторе снимаем (SMTP-send без локального лога).

### §5. Порядок и обратимость

Точный runbook cut-over/rollback — CRM `ADR-044` §10 (агрегатор — участник). Ключевое: демонтаж §4 выполняется **последним** (шаг decommission), после подтверждённой end-to-end доставки в CRM; до него агрегатор полностью функционален (таблицы не удалены) → откат бесплатный (reverse setWebhook + откат CRM). **Точка невозврата агрегатора** — drop таблиц/MinIO/вложений (§4) и снятие telegram-роутера (после переключения вебхуков ботов на CRM).

## Consequences

- Агрегатор ужимается до connector'а: IMAP-синк + SMTP-send + push-outbox. Убираются Redis-очереди Telegram/webhook/forward (остаётся `crm_push_queue` + `force_sync`), MinIO, groups/roles/tags/telegram/webhooks/forwarding, весь UI. Существенное упрощение поверхности.
- CRM — единственная система-запись; дрейф двух источников истины устранён.
- `messages` живёт как рабочий буфер (ретенция 30 дней) — достаточно как push-outbox; durable-хранилище в CRM.
- Вложения выпадают полностью (MinIO-зависимость снята).
- Множество ADR помечаются superseded — **forward-ссылки проставлены в шапках 15 ADR** (0017/0018/0019/0022/0023/0024/0027/0030/0031/0033/0034/0035/0037/0040/0042). **Статус-колонка `INDEX.md` этих строк ещё `accepted`, и пометка «narrowed by ADR-0043» у ADR-0039/0041 не проставлена — это follow-up `TD-050`** (выполняется вместе с нормативной синхронизацией docs агрегатора: `03-data-model`/`04-api-contracts`/`05-modules`/`07-deployment`). Честно фиксируем как незавершённое, а не заявляем сделанным. Историю решений не удаляем.
- Отложенное — `TD-049`/`TD-050` (см. `100-known-tech-debt.md`).

## Alternatives considered

- **Не хранить `messages` в агрегаторе вовсе** (dedup только по `mail_accounts.last_synced_uidnext`). Отклонён: `messages`-UNIQUE — проверенная страховка идемпотентности синка; ретенция и так делает таблицу рабочим буфером; убирать её = риск в проверенном движке ради малого выигрыша.
- **CRM поллит агрегаторский pull-API** (`ADR-0029`) вместо push. Отклонён владельцем: «агрегатор сам присылает». Push — near-real-time, без пустых опросов; очередь ретраев у агрегатора (Redis есть).
- **Оставить теги/Telegram/forwarding в агрегаторе, CRM только читает.** Отклонён владельцем: всё управление — в CRM.
- **Каскадно удалить `mail_accounts.user_id`/`users`.** Отклонён: дороже по миграции (FK, инварианты); единый `crm-service`-owner (`ADR-0039`) достаточен и уже существует.
- **Оставить message-scoped reply `ADR-0035`.** Отклонён: письма ушли в CRM → threading/scope нельзя резолвить по локальному письму; обобщённый send по `mailbox_id` с CRM-заголовками корректнее.
