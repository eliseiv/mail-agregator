# ADR-0039 — External write API (mailboxes + tags CRUD) + расширение read-фильтров для headless CRM

Статус: `accepted` — **сужен [ADR-0043](./ADR-0043-strip-to-connector-push-to-crm.md)** (2026-07-10; демонтаж выполнен на проде 2026-07-15 по [ADR-0044](./ADR-0044-decommission-runbook.md)) · Дата: 2026-07-09

> **⚠️ Сужен ADR-0043 — часть этого ADR СНЯТА. Читатель этого ADR в отрыве не должен реализовывать снятое.**
>
> **В силе:** раздел `/api/external/mailboxes` write (`POST /mailboxes/test`, `POST /mailboxes`, `PATCH/DELETE /mailboxes/{id}`, `POST /mailboxes/{id}/sync`); гейт `EXTERNAL_WRITE_ENABLED` + отдельный budget `LIMIT_EXTERNAL_WRITE` (`EXTERNAL_WRITE_RATE_LIMIT_PER_MINUTE`); auth-flow (rate → key → gate → write-gate → body → delegate); владелец создаваемого ящика — техпользователь `crm-service` (после демонтажа — **единственный** владелец всех ящиков, ADR-0044 §1); повторяемый фильтр `mail_account_id` (`list[int]`) в `GET /messages`.
>
> **СНЯТО (НЕ реализовывать):** (а) раздел **`/api/external/tags` CRUD** — теги целиком уехали в CRM ([ADR-0017](./ADR-0017-tags.md)/[ADR-0040](./ADR-0040-global-tags.md) superseded by ADR-0043); (б) фильтр **`group_id`** в `GET /messages` и поле `group_id` в `GET /mailboxes` / `ExternalMailboxDTO` — групп в агрегаторе нет, колонка `mail_accounts.group_id` дропнута (ADR-0044 Фаза C). Поля `last_synced_at`/`last_sync_error`/`consecutive_failures` в `ExternalMailboxDTO` — **в силе**.
>
> **Дополнено:** обобщённый `POST /api/external/mailboxes/{id}/send` ([ADR-0048](./ADR-0048-external-send-contract-and-reply-restore.md), заменил reply ADR-0035) и external-OAuth-роуты ([ADR-0045](./ADR-0045-external-outlook-oauth-headless.md)) живут в том же write-разделе под тем же гейтом. Актуальный контракт — [04-api-contracts.md](../04-api-contracts.md) §4f.

Extends [ADR-0029](./ADR-0029-external-pull-api.md) (pull) / [ADR-0035](./ADR-0035-external-reply-endpoint.md) (reply) / [ADR-0037](./ADR-0037-external-teams-mailboxes-message-filters.md) (teams/mailboxes/filters). Парный ADR в CRM — `ADR-038` (headless-интеграция). Глобальные теги — [ADR-0040](./ADR-0040-global-tags.md).

## Context

CRM становится единственным UI агрегатора (headless-коннектор, CRM `ADR-038`). External API сегодня read-only (ADR-0029/0037) + узкий scoped `reply` (ADR-0035). Для управления почтами и тегами из CRM нужен **write-раздел** external API, а для ролевой видимости писем по CRM-командам — **повторяемый `group_id`** в read-фильтрах.

Всё write-расширение живёт под тем же ключом `EXTERNAL_API_KEY`, тем же auth-flow (rate-limit → key extract → feature gate → constant-time compare → **write-gate** → body validation → delegate), теми же CSRF-исключениями по префиксу `/api/external/`. Порядок из ADR-0029 §4 / ADR-0035 §3 — нормативный, соблюдается побуквенно (`backend/app/external/router.py`).

### Q-0039-1 (BLOCKING, решён): владелец ящика при создании через external API

`mail_accounts.user_id` — **NOT NULL** (`shared/models/mail_account.py:34`, владелец ящика). При создании через безличный `EXTERNAL_API_KEY` нужно назначить владельца. Опции: (а) `groups.leader_user_id` целевой группы; (б) технический пользователь `crm-service` с ролью `super_admin`.

**Проведён аудит: по какому полю выбираются получатели во всех каналах доставки.** Результат — получатели драйвятся `mail_accounts.group_id` через `user_groups`, а **НЕ** `mail_accounts.user_id`:

- **Telegram-уведомления** (`worker/app/tg_notify_dispatch.py` → `TelegramNotifyService.dispatch_one_payload` → `TelegramNotificationsRepo.list_recipients_for_message`, `backend/app/repositories/telegram_notifications.py:206-219`). Предикат видимости — в клаузе **`JOIN users u ON (...)`** (не в `WHERE`; в `WHERE` только `m.id = :message_id` + opt-out `tg_notifications_enabled`):
  ```sql
  JOIN users u
       ON (
           u.role = 'super_admin'
           OR (ma.group_id IS NOT NULL AND EXISTS (
                  SELECT 1 FROM user_groups ug
                  WHERE  ug.user_id = u.id AND ug.group_id = ma.group_id))
           OR u.id = ma.user_id )
  JOIN telegram_links tl ON tl.user_id = u.id AND tl.dead_at IS NULL AND ...
  ```
  Получатели-члены команды выбираются веткой `user_groups`/`ma.group_id`. `OR u.id = ma.user_id` добавляет **владельца** лишь как дополнительного получателя, и только если у него есть живой `telegram_links` (запрос делает `JOIN telegram_links tl ON tl.user_id = u.id AND tl.dead_at IS NULL`).
- **Push-боты по командам** (`worker/app/push_notify_dispatch.py:125-132`): `group_id = account.group_id`; бот выбирается `next(b for b in settings.push_team_bots if b.group_id == group_id)`. `user_id` не читается.
- **Webhooks** (`worker/app/webhook_dispatch.py` → `WebhookDispatchService.dispatch_one_payload` → `WebhooksRepo.find_active_for_message`, payload `"team": {"id": recipient.group_id}`): резолв по группе, `user_id` не читается. (`webhooks.group_id`, ADR-0023.)
- **Forwarding** (`worker/app/forward_dispatch.py:187`): `gf = GroupForwardingRepo.get_by_group_id(account.group_id)`. `user_id` не читается. (`group_forwarding.group_id`, ADR-0034.)
- **Mailbox-alert** (`list_recipients_for_mailbox`, `telegram_notifications.py:287-302`): та же группа-предикат + `OR u.id = ma.user_id`.

Дополнительно проверено: `groups.leader_user_id` — **nullable** (`shared/models/group.py:36`, «nullable so a group can be created leaderless»); несколько super_admin **допустимы** (`ix_users_role_super_admin_partial` — обычный, НЕ уникальный индекс, `shared/models/user.py:115-119`); инвариант `super_admin ⇒ group_id IS NULL` (`users_role_group_invariant`, `user.py:109-113`).

**Решение — опция (б): выделенный технический пользователь `crm-service`** (роль `super_admin`, `group_id = NULL`, без пароля входа и без `telegram_links`), владелец всех ящиков, созданных через external write API. Обоснование:
1. **Детерминизм.** `groups.leader_user_id` nullable → опция (а) ломается для команды без лидера. `crm-service` доступен всегда.
2. **Доставка не ломается** (доказано выше): все 4 канала резолвят получателей по `mail_accounts.group_id`/`user_groups`. Ветка `OR u.id = ma.user_id` добавляет `crm-service` только при наличии у него `telegram_links` — а их у `crm-service` нет → 0 лишних уведомлений, 0 подавленных уведомлений членам команды. Ветка `u.role = 'super_admin'` в предикате получателей (recipient SQL, `JOIN users`) и одноимённая `u.role = 'super_admin'` в APPLY_TAGS (ADR-0017 §5.1) для `crm-service` тоже безвредны: нет `telegram_links` → нет доставки; после `ADR-0040` builtin-теги глобальны (`user_id IS NULL`), персональных тегов у `crm-service` нет → нечего применять.
3. **Честная атрибуция.** Headless-ящик системно-владельческий, не принадлежит человеку; author reply/audit = `crm-service` — правдиво.

**Следствие-ограничение:** `uq_mail_accounts_user_email (user_id, email)` (`mail_account.py:131`) при едином владельце `crm-service` делает email ящика **глобально-уникальным** для headless-пути (один и тот же email нельзя завести дважды). Для модели CRM 1:1 команда↔группа это приемлемо (ящик живёт в одной команде) → конфликт отдаётся как `409 conflict`. Способность round-18 «две команды заводят один email независимо» через headless-путь недоступна (осознанно).

`crm-service` сидируется идемпотентно на старте приложения (lifespan, по образцу `seed_super_admin`, `backend/app/auth/service.py:334` / `backend/app/main.py:78`); username — `crm-service` (lowercase, `ck_users_username_lower`); `password_hash = NULL`, `password_reset_required` не участвует (интерактивный вход не предусмотрен).

## Decision

### §1. Feature-gate + rate-limit budget

- **`EXTERNAL_WRITE_ENABLED: bool = False`** (`shared/config.py`, по образцу `EXTERNAL_REPLY_ENABLED`). Все write-эндпоинты (mailboxes CRUD/test/sync + tags CRUD/rules/apply) при `false` → `403 forbidden` даже с валидным ключом. Default `false` — существующие read-only-деплои не получают write молча.
- **`LIMIT_EXTERNAL_WRITE = Limit(name="external_write", capacity=60, window_seconds=60)`** (`backend/app/rate_limit.py`, по образцу `LIMIT_EXTERNAL_REPLY`) + операторский override **`EXTERNAL_WRITE_RATE_LIMIT_PER_MINUTE: int = Field(default=60, ge=1, le=10000)`**. Отдельный budget (не делит с read `LIMIT_EXTERNAL_API=120` и reply `LIMIT_EXTERNAL_REPLY=30`) — write не вытесняет read/reply и наоборот. Ключ лимита — `f"ip:{ip}"`.
- Auth-flow каждого write-эндпоинта: `consume(LIMIT_EXTERNAL_WRITE, ip)` → `_authenticate` (key/gate/compare) → **write-gate `if not EXTERNAL_WRITE_ENABLED: raise ForbiddenError`** → body/param validation → delegate. Порядок строго как ADR-0035 §3 (write-gate ПОСЛЕ auth, ДО body).

### §2. Раздел `/api/external/mailboxes` — write (CRUD)

Переиспользует `backend/app/accounts/service.py` (create/test/update/delete — включая IMAP/SMTP-проверку и SSRF-guard `backend/app/security.py::assert_public_host`). External-путь **не** имеет интерактивного `VisibilityScope`; вместо ролевой резолюции владельца (`_resolve_target_user_id`/`_validate_target_group`) применяется системный путь: **owner = `crm-service`**, `group_id` = из тела запроса (провалидирован на существование `groups.id`, иначе `404 group_not_found`; `null` допустим — персональный/безкомандный ящик).

Эндпоинты (полные схемы — [04-api-contracts.md](../04-api-contracts.md#external-write-mailboxes)):
- `POST /api/external/mailboxes/test` — проверка соединения без сохранения. Тело `ExternalMailboxTestRequest{email, imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_ssl, smtp_starttls, smtp_username?, password, smtp_password?}` → `{imap_ok: true, smtp_ok: true}` (иначе доменная ошибка от переиспользуемого `MailAccountService.test`/`accounts/testers.py`: `422 imap_login_failed`/`smtp_login_failed` — сбой логина/коннекта; `422 invalid_host` — SSRF-guard `assert_public_host`; `400 validation_error` — битое тело). Путь `test`/create/update **не** отдаёт `502` — `502 smtp_failed` относится только к фактической отправке через send-ядро (ADR-0035).
- `POST /api/external/mailboxes` — создание. Тело `ExternalMailboxCreateRequest{email, imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_ssl, smtp_starttls, smtp_username?, password, smtp_password?, display_name?, group_id?}` → `ExternalMailboxDTO` (расширенный, §4). Owner=`crm-service`. `409 conflict field=email` при дубле `(crm-service, email)`.
- `PATCH /api/external/mailboxes/{id}` — правка, включая смену кредов, `is_active`, `group_id` (перенос между командами). Тело `ExternalMailboxUpdateRequest` (все поля опц.; `set_group_id`-семантика присутствия для смены `group_id`, `set_is_active` для активации/деактивации) → `ExternalMailboxDTO`. `404 not_found` для неизвестного id.
- `DELETE /api/external/mailboxes/{id}` → `204`. Каскад вложений/MinIO как в `MailAccountService.delete`.
- `POST /api/external/mailboxes/{id}/sync` — форс-синк: пишет Redis-маркер `force_sync:{id}` (ex=60), как `MailAccountService.force_sync`. → `202 {"queued": true}` (или `200`). `404` для неизвестного id.

Пароль (`password`/`smtp_password`) — только в запросе; в `ExternalMailboxDTO` **не** возвращается; не логируется (redact `shared/logging.py`).

### §3. `GET /api/external/messages` — повторяемые + **AND-комбинируемые** `group_id`/`mail_account_id`

`group_id` и `mail_account_id` становятся **повторяемыми** (`list[int]`, FastAPI `Query(default=None)` парсит `?group_id=1&group_id=2`) **и AND-комбинируемыми** — взаимоисключение ADR-0037 §mutual-exclusion (`400 field=filter`) **снимается** (данный ADR **supersedes** этот пункт ADR-0037; см. Alternatives).

**Мотивация снятия взаимоисключения (безопасность CRM).** Дропдаун «Почта» (`mail_account_id`) в CRM доступен под `mail:view` и не гейтится admin-уровнем; для не-админа CRM обязан инъектировать scope-`group_id` (`MailScope.group_ids`) в КАЖДЫЙ запрос ленты (анти-энумерация). При взаимоисключении «оба набора непусты → 400» это давало вилку: либо `400` при попытке не-админа отфильтровать даже свой ящик, либо неинъекция scope и утечка чужого ящика (локального маппинга «ящик→группа» в CRM нет, кэш каталога намеренно убран — CRM `ADR-038` §2). AND-комбинирование снимает вилку: scope-`group_id` **AND** пользовательский `mail_account_id` дают корректное пересечение (свой ящик виден, чужой — пустая страница). Тот же паттерн уже действует в CRM SMS (`GET /sms/messages`: `number_id` + `team_id` комбинируемы AND, вне scope → пустая страница).

**Резолв (нормативно).** Эффективный набор `mail_account_ids`:
```
canonical_ids
  ∩ (⋃ list_account_ids_in_group(g) for g in group_id)   # только если group_id непуст
  ∩ (set(mail_account_id))                                 # только если mail_account_id непуст
```
Пустой фильтр-набор не накладывает ограничения от этого фильтра. Пустое пересечение → **пустая страница** (не 404). Сохраняются:
- «пустая страница вместо 404» для незнакомых/чужих/non-canonical id в любом из фильтров (инвариант ADR-0029 §3) — незнакомый id просто ничего не добавляет в пересечение;
- **обратная совместимость** одиночного значения: `?group_id=5` → `[5]`, `?mail_account_id=7` → `[7]`; single-filter-вызов идентичен ADR-0037 (меняется только случай «оба заданы»: было `400`, стало пересечение).
- Правки — `backend/app/external/router.py`, `service.py` (`_resolve_account_ids` — убрать ветку взаимоисключения, добавить AND-пересечение), `schemas.py`. Кода `field=filter` больше нет.

### §4. `GET /api/external/mailboxes` — фильтры + статус синка

- Новые query: **`is_active: bool | None`** (None = все, `true`/`false` — фильтр) и повторяемый **`group_id: list[int]`** (union; пустой = без фильтра по группе). Оба опциональны, работают поверх canonical-дедупа.
- **`ExternalMailboxDTO` расширяется** полями статуса синка (нужны CRM для кружка/диагностики), в дополнение к текущим `id, email, display_name, group_id, is_active`:
  - `last_synced_at: datetime | null`
  - `last_sync_error: str | null`
  - `consecutive_failures: int`

  Источник — одноимённые поля `mail_accounts` (`accounts/service.py:_to_dto` их уже отдаёт для UI). Секреты (`encrypted_password`/`oauth_*`/`user_id`) по-прежнему НЕ раскрываются. Расширение аддитивно (существующие CRM-потребители не ломаются — новые поля читаются опционально).

## Consequences

- CRM получает полный headless-CRUD почт и статус синка через external API под одним ключом. Ролевая видимость CRM работает повторяемым `group_id`.
- `EXTERNAL_WRITE_ENABLED=false` по умолчанию — write не включается молча при апгрейде.
- Единый владелец `crm-service` → email ящика глобально-уникален для headless-пути (`409` на дубль). Многокомандный один email через headless недоступен (осознанно).
- `crm-service` — второй super_admin в системе; безвреден для доставки/тегов (нет `telegram_links`, нет персональных тегов). Не имеет интерактивного входа.
- Изменений схемы БД нет (owner=`crm-service` — обычная строка `users`, сидируется в lifespan; `mail_accounts`/`tags` не мигрируют в этом ADR — теги мигрируют в ADR-0040).

## Alternatives considered

- **Владелец = `groups.leader_user_id` (опция а).** Отклонён: `leader_user_id` nullable → ломается для команды без лидера. Плюс `find_by_user_email(leader, email)` дал бы разное поведение уникальности при смене лидера.
- **Владелец = вызывающий CRM-пользователь (проброс `user_id` в теле).** Отклонён: external key безличен; CRM-`user_id` (UUID) не мапится на `users.id` (BIGINT) агрегатора — разные пространства идентификаторов.
- **Один общий rate-limit budget для read+write.** Отклонён: write дороже/abuse-опаснее; общий budget → взаимное вытеснение (как отклонено в ADR-0035 §4 для reply).
- **Раскрыть `smtp_*`/`imap_*` креды в `ExternalMailboxDTO` для «полноты».** Отклонён: креды не нужны CRM (оно их не показывает и не открывает сессии), раскрытие — лишняя поверхность (инвариант ADR-0029 §Security).
- **Сохранить взаимоисключение `mail_account_id` × `group_id` (ADR-0037 §mutual-exclusion, `400 field=filter`).** Отклонён — **supersedes ADR-0037** в этой части. Причина в §3: при взаимоисключении CRM-инъекция scope-`group_id` для не-админа конфликтует с пользовательским `mail_account_id` → либо `400` на легитимный свой ящик, либо утечка чужого ящика (в CRM нет локального маппинга ящик→группа). ADR-0037 выбрал взаимоисключение как «UI выбирает либо ящик, либо команду» — но headless-CRM обязан комбинировать пользовательский фильтр со scope-командами, поэтому комбинация здесь **нужна**. AND-комбинирование безопасно (пересечение) и BC для single-filter. Альтернатива «CRM валидирует `mail_account_id` против scope-отфильтрованного списка ящиков перед проксированием» отклонена: требует доп. round-trip `GET /mailboxes` на каждый запрос ленты (или локального кэша, который §2 намеренно убирает), тогда как AND в external даёт то же пересечение за один запрос без кэша.
