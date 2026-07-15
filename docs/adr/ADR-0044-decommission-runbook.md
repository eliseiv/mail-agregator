# ADR-0044 — Runbook демонтажа: снос снятых подсистем агрегатора до чистого connector'а + отключение фронта

| | |
| --- | --- |
| Статус | accepted (**§2 и Фаза A2 амендированы [ADR-0048](./ADR-0048-external-send-contract-and-reply-restore.md)** — контракт ответа send + расщепление A2 на A2.1/A2.2; **§3/§4 Фаза C амендированы §3.1** — миграция сама сидит `crm-service` (self-sufficient, устранён CI-блокер DDL) + форсирует отложенный FK `users_group_id_fkey` через `SET CONSTRAINTS ALL IMMEDIATE` перед drop-миграциями) |
| Дата | 2026-07-10 |

**Операционализирует** [ADR-0043](./ADR-0043-strip-to-connector-push-to-crm.md) §4/§5 (что снимаем) и финализирует [ADR-0041](./ADR-0041-disable-jinja-ui.md) (снос UI). ADR-0043 принял *решение* о переходе в connector; настоящий ADR — **исполнимый, необратимый runbook** для backend/devops: точная инвентаризация таблиц с вердиктами, FK-безопасный порядок drop'ов, что бэкапить перед точкой невозврата, что удалить из фронта. Закрывает `Q-0041-1` (судьба `oauth_router`). Ведётся под `TD-049` (объём/порядок drop'ов) и `TD-050` (бэкап `admin_audit` + docs-sync).

**Контекст готовности.** Cut-over на CRM выполнен 10.07.2026: push-outbox (`messages.pushed_at`, миграция `20260710_024`), `crm_push_dispatch`/`crm_push_recovery`/`crm_status_dispatch` — **уже в коде и на проде** (2874 письма, 7 реальных уведомлений через CRM). Настоящий демонтаж — **последний шаг** (decommission), после подтверждённой end-to-end доставки; до drop-миграций откат бесплатен (ADR-0043 §5).

## Context

Агрегатор всё ещё несёт полную функциональность headless-прокси (теги, Telegram, webhooks, forwarding, группы/роли/пользователи, Jinja-UI/static, MinIO-вложения) — код, таблицы, worker-jobs, env, docs-разделы. CRM теперь durable system of record. Всё лишнее нужно снести необратимо, а фронт — снять так, чтобы сервис нельзя было открыть (владелец: «убери фронт, чтобы сервис нельзя было открыть»).

Демонтаж **не тривиален** из-за связей, проверенных по коду:

- `mail_accounts.user_id` — **NOT NULL FK на `users`, `ondelete=CASCADE`** (`shared/models/mail_account.py:34-37`). Дроп `users` каскадом снёс бы ящики. Решение ADR-0043 §4 (альтернатива каскадного дропа отклонена): `users` **остаётся** технической таблицей с единственным рядом `crm-service`; `user_id` всех ящиков переводится на него; FK/NOT NULL/CASCADE **сохраняются** (безопасно, пока `crm-service` не удаляется).
- `mail_accounts.group_id` — FK на `groups`, `ondelete=SET NULL`, nullable (`mail_account.py:43-45`). Колонка **удаляется** до дропа `groups`.
- `worker/app/sync_cycle.py:321` до сих пор вызывает `TagsService.apply_tags_to_message`; строки 386-470 — enqueue в `tg_notify`/`webhook`/`push_notify`/`forward` очереди; 284-313 — скачивание вложений в MinIO. Сначала снять код-читателей, потом дропать таблицы.
- HTML-фронт монтируется в `backend/app/main.py`: `StaticFiles /static` (`:159`), проверки `templates/`+`static/` (`:127-158`), HTML-роутеры (`auth`/`accounts`/`messages`/`send`/`tags`/`admin`/`groups`/`telegram`/`webhooks`/`forwarding`), friendly-redirect `→/login` (`:180-186`).

## Decision

### §1. Инвентаризация таблиц агрегатора (вердикт по каждой)

Полный набор — 19 таблиц (`shared/models/*`). `__tablename__` и FK процитированы по моделям.

#### (а) ОСТАВЛЯЕМ — движок connector'а

| Таблица | Модель | Вердикт | Обоснование |
| --- | --- | --- | --- |
| `mail_accounts` | `mail_account.py` | **KEEP** (+ изменить схему, см. §3) | Ядро: IMAP/SMTP-креды, AES-шифрование, OAuth-Outlook. Убрать колонку `group_id`; `user_id` перевести на `crm-service`. |
| `messages` | `message.py` | **KEEP** (push-outbox) | Рабочий буфер: sync вставляет идемпотентно (`uq_messages_account_uidv_uid`), `pushed_at` маркер доставки в CRM, ретенция 30 дней. Без изменений схемы (`pushed_at` уже добавлен миграцией `20260710_024`). |
| `users` | `user.py` | **KEEP как техническая** (1 ряд `crm-service`) | Нельзя дропнуть: `mail_accounts.user_id` NOT NULL CASCADE ссылается сюда. Оставляем только `crm-service` (super_admin, `group_id` NULL), human-ряды удаляются (§4 Фаза F). Убрать колонку `group_id` (§4 Фаза E). Колонки `password_hash`/`role`/`lockout_*` становятся рудиментарными — оставляем как есть (дроп не обязателен; отдельный необязательный cleanup — `TD-051`). |

#### (б) ПОД СНОС — мигрировано в CRM / больше не нужно

| Таблица | Модель | FK, влияющие на порядок drop | Обоснование сноса |
| --- | --- | --- | --- |
| `sent_attachments` | `sent_attachment.py` | → `sent_messages` (CASCADE) | Вложения-на-отправку (MinIO, ADR-0007). Таблица-заглушка, TD-005. |
| `sent_messages` | `sent_message.py` | → `users`, `mail_accounts` (CASCADE) | Durable-лог отправленного уходит в CRM (`mail_sent_messages`, ADR-0043 §4). Обобщённый send (§2) НЕ пишет локальный лог. |
| `attachments` | `attachment.py` | → `messages` (CASCADE) | Вложения/MinIO (ADR-0007) — в CRM не нужны. 646 объектов удаляются. |
| `message_tags` | `tag.py` | → `messages`, `tags` (CASCADE) | Теги (ADR-0017/0040) — матчинг переехал в CRM побуквенно. |
| `tag_rules` | `tag.py` | → `tags` (CASCADE) | Теги. |
| `tags` | `tag.py` | → `users` (CASCADE) | Теги. |
| `telegram_notifications` | `telegram_notification.py` | → `messages`, `users` (CASCADE) | Telegram (ADR-0022/0024) — SSO/нотификации в CRM. |
| `telegram_links` | `telegram_link.py` | → `users` (CASCADE) | Telegram-привязки. |
| `webhook_deliveries` | `webhook.py` | → `webhooks`, `messages` (CASCADE) | Webhooks (ADR-0023). В проде 0 webhooks — переносить нечего. |
| `webhooks` | `webhook.py` | → `groups` (CASCADE) | Webhooks. |
| `message_forwards` | `message_forwards.py` | → `messages`, `groups` (CASCADE) | Forwarding (ADR-0034). В CRM отложено (TD-040 CRM), 1 правило прода НЕ мигрируется. |
| `group_forwarding` | `group_forwarding.py` | → `groups` (CASCADE) | Forwarding-конфиг. |
| `user_groups` | `user_group.py` | → `users`, `groups` (CASCADE) | Multi-group membership (ADR-0030). Владение командой — только в CRM. |
| `groups` | `group.py` | ← `mail_accounts.group_id` (SET NULL), `users.group_id` (SET NULL), `user_groups`, `group_forwarding`, `message_forwards`, `webhooks`; сама → `users.leader_user_id` (**RESTRICT**) | Группы/роли (ADR-0019/0030/0031). Дропается ПОСЛЕ снятия всех входящих FK-колонок/таблиц. |
| `users_settings` | `user_settings.py` | → `users` (CASCADE) | Opt-out TG (ADR-0022 §2.7). На проде 0 строк. |
| `admin_audit` | `admin_audit.py` | **нет FK** | Журнал super-admin (248 записей, 2026-05-14…2026-07-10). НЕ мигрируется в CRM (CRM аудит лог-based). **Обязателен `pg_dump` перед drop** (TD-050). |

**Итог:** KEEP — 3 таблицы (`mail_accounts`, `messages`, `users`); DROP — 16 таблиц.

### §2. Предусловие: обобщённый send (ADR-0043 §3) должен быть жив ДО сноса reply/`sent_messages`

Эндпоинт `POST /api/external/mailboxes/{id}/send` (ADR-0043 §3) в коде **ещё не реализован** (в `backend/app/external/router.py` есть только message-scoped reply ADR-0035). Реализация §3 — **backend-предусловие** демонтажа: пока CRM не переключён на новый send, message-scoped reply `POST /api/external/messages/{id}/reply` и таблица `sent_messages` НЕ снимаются. Порядок в §4 (Фаза A2) это фиксирует. Валидация переносится из ADR-0035 (см. ADR-0043 §3), новый send НЕ пишет `sent_messages`.

> **⚠️ Амендмент [ADR-0048](./ADR-0048-external-send-contract-and-reply-restore.md) — это НЕ только предусловие демонтажа, а ЖИВОЙ ПРОД-БАГ.** CRM уже сегодня зовёт несуществующий `POST /api/external/mailboxes/{id}/send` (`CRM backend/app/infra/mail_client.py:225`) ⇒ **ответ на письмо из CRM не работает с cut-over** (`404`). Поэтому: **(1)** ответ эндпоинта — **`200 { smtp_message_id }`**, без `sent_id` (у агрегатора нет его источника после снятия writer'а `sent_messages`, `send/service.py:460`; идентификатор выдаёт CRM из `mail_sent_messages` — `ADR-0048` §1); **(2)** Фаза A2 **расщеплена** на **A2.1** (аддитивная реализация send + переключение CRM — деплоится НЕМЕДЛЕННО, без единого DDL, ничего не удаляя) и **A2.2** (снятие reply/`EXTERNAL_REPLY_ENABLED`/writer'а `sent_messages` — в атомарном A1+A3-релизе). Нормативный порядок — `ADR-0048` §3.

### §3. ГЛАВНОЕ ПРАВИЛО (lock-step): снять весь код-читатель/writer/ORM-мэппинг/импорт таблицы ДО любого DDL по ней

Демонтаж ломается не FK-порядком (он верен, §1), а тем, что SQLAlchemy/ FastAPI/worker продолжают ссылаться на таблицу/колонку, которую уже дропнули. Поэтому по **каждой** DROP-таблице и **каждой** DROP COLUMN действует инвариант:

> Сначала — в коде — снимается ВСЁ, что таблицу/колонку читает или пишет: ORM `mapped_column`/`relationship`/класс модели, импорты в `external/`, `worker/`, репозитории, writer'ы аудита. ТОЛЬКО ПОТОМ выполняется DDL (DROP COLUMN / DROP TABLE). Правка модели/кода деплоится **не позже** релиза с миграцией; обратный порядок (миграция раньше кода) ЗАПРЕЩЁН — он даёт `UndefinedColumn`/`ImportError` на живом коннекторе.

Конкретные лок-степ-пары (проверены по коду):

- **`mail_accounts.group_id`** (DDL — Фаза C). До DROP COLUMN снять `MailAccount.group_id` = `mapped_column` (`shared/models/mail_account.py:43-47`) и все его читатели/писатели: `account.group_id`-ветка forwarding в `sync_cycle.py:282-283`, `group_id` в `to_external_mailbox_dto` (`external/service.py:57`) и в `MailAccountService.create/update`. Иначе `MailAccountsRepo.list_active()` (`sync_cycle.py:967`) при `select(MailAccount)` эмитит `group_id` → `UndefinedColumn` → останавливается синк ВСЕХ ящиков.
- **`users.group_id`** (DDL — Фаза E). До DROP COLUMN снять `User.group_id` = `mapped_column` **и** `relationship User.group` (`shared/models/user.py:60-64, 83-88`). Если `Group`-класс удаляется, а `relationship User.group` или `Group.leader` (`shared/models/group.py:51-55`) остаются — падает конфигурация мэпперов **глобально** (любой ORM-запрос).
- **Классы моделей дропаемых таблиц** (`Group`, `UserGroup`, `Tag`, `TagRule`, `MessageTag`, `TelegramLink`, `TelegramNotification`, `Webhook`, `WebhookDelivery`, `GroupForwarding`, `MessageForward`, `UserSettings`, `AdminAudit`, `Attachment`, `SentMessage`, `SentAttachment`) — удаляются из `shared/models/*` + экспортов `shared/models/__init__.py` в Фазе A (снятие мэппинга безвредно, пока таблица ещё в БД), строго ДО их DROP TABLE (Фаза D). Удаляемый класс не должен оставлять висячих `relationship` на себя (актуально только для пары `User.group`/`Group.leader`).
- **KEEP-модули, импортящие удаляемые репозитории/сервисы/`get_storage`/audit** (тот же класс дефекта, что external): `deps.py`, `accounts/service.py`, `send/service.py`, `health/router.py`, `auth/service.py`. Каждый роняет `create_app()` целиком (`ImportError` на старте, недоступны `/healthz`+external) сразу после удаления символа, ДО всякого DDL. Их детач прописан в Фазах A1–A3 и сведён в **§8 (таблица полноты: удаляемый символ → KEEP-потребители → фаза детача)** — это проверяемый чек-лист, а не «на глаз». Правило: ни один KEEP-роутер/сервис/lifespan/worker-путь не остаётся с импортом удаляемого символа к моменту его удаления.

**Изменение схемы `mail_accounts`** (отдельная alembic-ревизия, `down_revision = 20260710_024`, Фаза C):

**§3.1 (НОРМАТИВНО — миграция самодостаточна: сама сидит `crm-service`, НЕ полагается на то, что приложение когда-то стартовало).** Repoint из шага 1 нуждается в `users`-ряде `crm-service` как в целевом owner'е. Этот ряд сидит ТОЛЬКО app-lifespan `seed_crm_service_user` (`backend/app/main.py:60` → `backend/app/auth/service.py:33`), который отрабатывает при **старте приложения**. Но миграции применяются в контекстах, где приложение ещё/уже не стартовало:
- **CI** прогоняет `alembic upgrade head` (`.github/workflows/ci.yml:200`) на **пустой** БД **ДО** запуска приложения → `crm-service` нет → repoint не может резолвить owner'а. Это ловит и qa-прогон на свежей БД (эмпирически воспроизведено).
- **Восстановление из бэкапа / новый инстанс** — тот же класс: схема накатывается миграциями до первого буста app.

Поэтому миграция Фазы C **ОБЯЗАНА сама идемпотентно засидить `crm-service` ПЕРЕД резолвом его id** (INSERT-если-нет), а НЕ бросать `RuntimeError` при отсутствии ряда. Это устраняет скрытую связность «миграция схемы зависит от того, что приложение однажды засидило данные» — тот же класс дефекта, что уже кусал проект; после фикса миграция самодостаточна одинаково на проде, в CI и при restore. Резолвер id (`migrations/versions/20260715_025_*.py::_crm_service_id`) становится «seed-if-missing → SELECT»; прежний `raise RuntimeError(... 'crm-service' not found ...)` — не удаляется целиком, а низводится до defensive-инварианта «не должен срабатывать после self-seed» (belt-and-suspenders).

Идемпотентный self-seed (raw SQL уровня alembic — **не** через ORM/`seed_crm_service_user`, т.к. миграция не поднимает app-граф). Поля сверены с `seed_crm_service_user` (`auth/service.py:63-71`) и со схемой `users` **на момент Фазы C**:

```sql
INSERT INTO users (username, role, password_reset_required)
VALUES ('crm-service', 'super_admin', false)
ON CONFLICT (username) DO NOTHING;
```

Обоснование каждого поля (схема `users` на момент 025 — до Фазы E, `group_id` ЕЩЁ существует):
- `username='crm-service'` — NOT NULL, lowercase (удовлетворяет CHECK `ck_users_username_lower`, `20260505_002`); `ON CONFLICT (username)` опирается на `uq_users_username` (`20260505_001:87`).
- `role='super_admin'` — задаём явно (совпадает с `seed_crm_service_user`), удовлетворяет CHECK `ck_users_role` (`20260508_004`). Колонка NOT NULL с DB-дефолтом `'group_member'`, но технический owner обязан быть `super_admin`.
- `password_reset_required=false` — совпадает с seed (DB-дефолт `true`; задаём `false` для точного соответствия технического ряда).
- **Прочие NOT NULL берут server-default** и в INSERT не перечисляются: `id` (autoincrement, `20260505_001:45-51`), `failed_login_attempts` (`0`), `created_at`/`updated_at` (`now()`).
- **Nullable → NULL** (совпадает с seed): `email`, `display_name`, `password_hash`, `password_encrypted`, а также **`group_id`** — колонка на момент Фазы C nullable (FK → `groups` ON DELETE SET NULL, `20260508_004`); CHECK `users_role_group_invariant` в живой схеме **отсутствует** (снят `20260508_005`), leader-consistency-триггер снят (`20260508_006/007`) ⇒ `super_admin` с `group_id IS NULL` валиден (тот же инвариант закреплён в Фазе E).

1. **Данные (repoint):** `UPDATE mail_accounts SET user_id = <crm-service.id> WHERE user_id <> <crm-service.id>`, где `<crm-service.id>` резолвится SELECT'ом ПОСЛЕ self-seed §3.1 (всегда находится). На пустой БД (CI) затрагивает 0 рядов; на проде owner'ы уже переведены — идемпотентно.
2. **Форсировать отложенные FK-события — `SET CONSTRAINTS ALL IMMEDIATE` (НОРМАТИВНО; НЕ удалять как «лишнее»).** Self-seed §3.1 (`INSERT` ряда в `users`) ставит в очередь **отложенное** событие FK `users_group_id_fkey` (`users.group_id → groups`, объявлен **`DEFERRABLE INITIALLY DEFERRED`** — `migrations/versions/20260508_004_groups_and_roles.py:124-128`): любая вставка/апдейт ряда `users` откладывает проверку этого FK до COMMIT транзакции. Alembic гонит ВСЮ цепочку decommission-миграций (025→028) в **ОДНОЙ транзакции** (`transaction_per_migration` не задан — `migrations/env.py:55-62`), поэтому непроверенное событие дожило бы до Фазы E (`20260715_027`, `ALTER TABLE users DROP COLUMN group_id`) и уронило бы её с `cannot ALTER TABLE users because it has pending trigger events`. `SET CONSTRAINTS ALL IMMEDIATE` немедленно проверяет и опустошает очередь отложенных FK; проверка тривиально проходит (`crm-service.group_id IS NULL`; все ящики repointed на валидного owner'а — шаг 1). Выполняется ПОСЛЕ self-seed + repoint и ДО `DROP COLUMN`. Реализовано: `migrations/versions/20260715_025_decommission_phase_c_mail_accounts.py:134` (развёрнутое обоснование — там же, `:123-133`).
3. **Схема:** `ALTER TABLE mail_accounts DROP COLUMN group_id`. Индекс `ix_mail_accounts_group_id` в живой схеме **существует** (создан миграцией `20260509_009`; редуцированный ORM `__table_args__` в `mail_account.py` его больше не перечисляет — оставлены только `ix_mail_accounts_user_id`, `ix_mail_accounts_active_partial`, поэтому чтение ORM вводит в заблуждение). Он снимается в этой же Фазе C: PostgreSQL снимает индекс автоматически при `DROP COLUMN group_id`, миграция дополнительно делает явный `DROP INDEX IF EXISTS ix_mail_accounts_group_id`.
4. `user_id` (NOT NULL, FK CASCADE, `uq_mail_accounts_user_email`) — **без изменений**; `UNIQUE(user_id,email)` при едином owner = глобальная уникальность email (ADR-0043 §4).

`downgrade()` восстанавливает `group_id` nullable + FK (данные не восстановимы — необратимо by design). **`downgrade()` НЕ удаляет засиженный `crm-service`** — это KEEP-ряд (owner всех ящиков); self-seed §3.1 by design не откатывается (удаление owner'а нарушило бы `mail_accounts.user_id NOT NULL`).

### §4. Порядок демонтажа по фазам (каждая фаза оставляет connector рабочим)

**Инвариант каждой фазы:** после неё `sync_cycle` (IMAP-синк + push в CRM), status-канал и внешний mailbox-API/pull остаются рабочими. Фазы A1→A3 — только код (без DDL, откат бесплатен); B — бэкап; C→G — необратимый DDL.

**Атомарность code-релиза A1+A3 (обязательно).** Снятие импортов/модулей/ORM-мэппинга разрывает паутину перекрёстных import'ов между KEEP- и удаляемыми модулями (напр. `health/router.py` импортит `CurrentUser` из `deps.py`, который перестаёт его отдавать после снятия session-машинерии; `repositories/messages.py` импортит DROP-ORM). Поэтому **все code-детачи фаз A1 и A3 деплоятся ОДНИМ атомарным релизом агрегатора** — дробление A1/A3 на отдельные деплои создаёт промежуточные `ImportError`-состояния и роняет `create_app()`. **A2.1** (обобщённый send) — аддитивен и **деплоится ОТДЕЛЬНЫМ, более ранним релизом** (он чинит живой прод-баг — [ADR-0048](./ADR-0048-external-send-contract-and-reply-restore.md)); **A2.2** (снятие reply/writer'а `sent_messages`) идёт внутри A1+A3-релиза и гейтится подтверждённым переключением CRM на новый send. Разбиение A1→A2→A3 — **логический порядок детача**, а НЕ обязательные отдельные деплои. Перед деплоем релиза — обязателен **§9-гейт полноты** (import/mypy/тесты).

#### Фаза A1 — Детач external-API от tags/groups (backend) — ПЕРЕД удалением модулей tags/groups

external остаётся рабочим, но сейчас импортит снимаемое. Снять ДО удаления модулей (CRITICAL-2, MAJOR-3):
- В `external/router.py`: удалить tags-роуты (`GET/POST/PATCH/DELETE /api/external/tags*`), teams-роуты (`GET /api/external/teams`, `POST/DELETE /api/external/teams`) и импорты `ExternalTagsService`, `ExternalTags*`/`ExternalTeams*`-схем (`router.py:55-68`); удалить фильтр `group_id` из `GET /messages` и `GET /mailboxes`.
- В `external/service.py`: убрать сборку тегов в pull-DTO (`MessageTagsRepo.list_for_messages_bulk`, ~`:282`) и поле `tags` из `ExternalMessageDTO`; убрать `list_teams`/group-логику; снять импорты `GroupsRepo` (`:38`), `MessageTagsRepo` (`:41`), `Tag` (`:42`), `ExternalTagDTO`/`ExternalTeam*` (`:34-36`). **После этого сохраняемый pull `GET /messages` больше не JOIN'ит `message_tags`/`tags`** — их дроп (Фаза D) его не ломает.
- В `external/write_service.py`: удалить `ExternalTagsService` и импорты `TagsService` (`:56`), `tags.schemas` (`:55`); `MailAccountService`-реюз оставить, но убрать проброс `group_id`. Синтетический super_admin `VisibilityScope` (`write_service.py:84`, строится напрямую, НЕ через `deps.build_scope`) — сохраняется.
- В `deps.py` (KEEP — источник `DbSession`/`VisibilityScope` для external): снять импорт `UserGroupsRepo` (`:30`) и импорт `sessions` (`SessionData`/`SessionStore`, `:32`), т.к. `sessions.py` сносится в §5. **Удалить поимённо** session/UI-символы: `current_session`+`CurrentSession`, `current_user`+`CurrentUser`, `build_scope`, `current_scope`+`CurrentScope`, `require_super_admin`+`SuperAdminScope`, `require_admin_or_leader`+`AdminOrLeaderScope`, `require_admin`+`AdminUser`, `get_session_token`+`SessionToken`, `is_form_request` (form-fallback). **Оставить поимённо:** `get_db`/`DbSession` и dataclass `VisibilityScope` — их использует external (`external/router.py` → `DbSession`; `write_service.py:36,84` → `VisibilityScope`, строится синтетически). Иначе `create_app()` падает.

*После A1 ни `external/`, ни `deps.py` не импортят `tags/`/`groups/`/`user_groups`/их репозитории; pull-DTO без тегов.*

#### Фаза A2 — Обобщённый send + переключение CRM + детач writer'а `sent_messages` (backend, предусловие §2)

> **Расщеплена [ADR-0048](./ADR-0048-external-send-contract-and-reply-restore.md) §3 на A2.1 и A2.2** — прежняя единая формулировка делала восстановление сломанной прод-функции (reply) заложником всего демонтажа. Ответ эндпоинта — **`{ smtp_message_id }`**, `sent_id` НЕТ (`ADR-0048` §1).

**A2.1 — восстановление отправки (аддитивно, БЕЗ DDL, деплоится немедленно и отдельно от A1/A3):**
- Реализовать `POST /api/external/mailboxes/{id}/send` (контракт — `ADR-0048` §1: запрос `{to, cc?, subject?, body_text, in_reply_to?, refs?}` → `200 {smtp_message_id}`; реюз `send/service.py::_send_core` + `send/mime.py`, валидация из ADR-0035), **новый путь НЕ пишет `sent_messages`** (`INSERT` `send/service.py:460` из него не зовётся).
- **Ничего не удалять:** `POST /api/external/messages/{id}/reply`, `EXTERNAL_REPLY_ENABLED`, `SentMessagesRepo` — остаются (откат = снять новый роут).
- Парный релиз CRM (их `ADR-057` §2/§3): CRM перестаёт ждать `sent_id` в ответе агрегатора и маппит внешний `404` как «ящик не найден».
- §9-гейт (import/mypy/тесты) обязателен и здесь.

**A2.2 — снятие старого пути (в атомарном A1+A3-релизе; гейт — подтверждённая работа A2.1 на проде):**
- Удалить `POST /api/external/messages/{id}/reply` (`external/router.py:280`), `_parse_reply_body` (`:260`), `ExternalReplyRequest`/`ExternalReplyResponse` (`external/schemas.py:152-200`), `SendService.send_external_reply` (`send/service.py:306-364`); вывести из употребления `EXTERNAL_REPLY_ENABLED` (`shared/config.py:208`) + `EXTERNAL_REPLY_RATE_LIMIT*` (env-чистка — Фаза G).
- Убрать все записи в `sent_messages` из `send/service.py`: снять импорт `SentMessagesRepo` (`send/service.py:32`), поле (`:284`) и `INSERT` (`:460`) + session-visibility-методы (не нужны машинному send'у). `send/mime.py` + SMTP-ядро — оставить. Writer таблицы `sent_messages` снят ДО её дропа (Фаза D).

Pull `GET /api/external/messages` и `GET /api/external/mailboxes` (обестеженные в A1) — **оставить** (сверка/reconcile; снимаются позже, ADR-0043 §4).

#### Фаза A3 — Детач worker/backend читателей+writer'ов + снятие ORM-мэппинга/классов + снос фронта

- `worker/app/sync_cycle.py`: убрать `apply_tags_to_message` (`:315-349`) + импорт `TagsService` (`:48`); убрать enqueue `tg_notify` (`:386-400`) + импорт `TelegramNotifyService` (`:49`); `webhook` (`:402-420`); `push_notify` (`:422-447`) + **top-level импорт `_QUEUE_KEY` (`:60`)**; `forward` (`:449-470`); скачивание вложений/MinIO (`:284-313`) + импорт `storage`; `_enqueue_mailbox_alert` (`:694-717`) + **top-level импорт `MAILBOX_ALERT_QUEUE_KEY` (`:59`)**; `account.group_id`-ветку (`:282-283`, см. §3 лок-степ). **MAJOR-4 — снять audit-writer'ы ЗДЕСЬ (до дропа `admin_audit`):** в `_disable_after_failures` удалить `AuditWriter.log(... "account_auto_disabled" ...)` (`:767-775`) + `UsersRepo.get_admin()` (`:765`); **удалить функцию `_audit_mass_failure_suppressed` ЦЕЛИКОМ** (`:791-818`, включая `get_admin()` `:805` и `AuditWriter.log` `:807-818`) **и её вызов** в `_run_for_accounts` (`:911-920`) — после снятия audit-INSERT функция становится no-op (MINOR-5); снять импорт `AuditWriter` (`:41`). Логику disable/circuit-breaker/`disable_and_stamp_alert`/`sync_breaker_tripped`-лог — **оставить** (аудит-INSERT'ов там нет). **Оставить:** `crm_push` enqueue (`:359-378`), `_enqueue_crm_status` (`:720-740`), `mark_sync_success`, классификацию ошибок.
- `worker/app/main.py`: снять регистрацию + `_safe_*`-обёртки + импорты jobs `tg_notify_dispatch`, `tg_notify_recovery`, `push_notify_dispatch`, `mailbox_alert_dispatch`, `webhook_dispatch`, `webhook_recovery`, `forward_dispatch`. **Оставить:** `sync_cycle`, `force_sync_dispatcher`, `retention_cleanup`, `alive_touch`, `crm_push_dispatch`, `crm_push_recovery`, `crm_status_dispatch`.
- `worker/app/cleanup.py`: убрать удаление вложений/MinIO; оставить ретенцию `messages`.
- Удалить worker-модули: `tg_notify_dispatch.py`, `tg_notify_recovery.py`, `push_notify_dispatch.py`, `mailbox_alert_dispatch.py`, `webhook_dispatch.py`, `webhook_recovery.py`, `forward_dispatch.py`.
- Удалить backend-модули целиком: `tags/`, `telegram/`, `webhooks/`, `forwarding/`, `groups/`, `admin/`, `audit/`, `messages/` (HTML), `oauth/router.py` (**session-роутер — снимается ТОЛЬКО когда external-OAuth-роуты ADR-0045 в `external/router.py` уже реализованы, §7; в том же/более раннем релизе**), `auth/router.py` (HTML; `auth/service.py` — оставить: `seed_crm_service_user`, `CRM_SERVICE_USERNAME` для `external/write_service.py:35`), `accounts/router.py` (HTML; `accounts/service.py` — оставить), `send/router.py` (form-fallback; `send/service.py`+`mime.py` — оставить), репозитории `repositories/{tags,telegram_links,telegram_notifications,user_settings,webhooks,group_forwarding,message_forwards,groups,user_groups,audit,sent_messages}.py`. **Оставить:** `accounts/service.py`, `send/service.py`+`mime.py`, `oauth/service.py` (**`OutlookTokenService` refresh + `OutlookOAuthService` — адаптируется под external-consent ADR-0045, НЕ удаляется**), `crm_push/`, `external/`, `health/`, `repositories/{mail_accounts,messages,users}.py`.
- **Детач KEEP-`accounts/service.py`** (реюзается external write, `write_service.py:34`; §3 lock-step): снять импорт `AuditWriter` (`:30`) + `self._audit` (`:127`) + все `self._audit.log(...)` (`:548` и др.); снять `GroupsRepo` (`:41`, `:126`) с `_validate_target_group`/`_transfer_group` и всей `group_id`-логикой create/update; синхронно с Фазой G снять `get_storage` (`:49`, `:128`) и MinIO-каскад удаления вложений в `delete` (`:774-776`). Реюз `MailAccountService` (create/test/update/delete/sync + SSRF-guard) — оставить рабочим.
- **Детач KEEP-`repositories/messages.py`** (KEEP-репозиторий; потребители: `external/service.py:40` pull-DTO, `worker/sync_cycle.py:45`, `worker/cleanup.py:16`, `crm_push/service.py:35`, `accounts/service.py:43`, `send/service.py:28`): строка `:18` `from shared.models import Attachment, MailAccount, Message, MessageTag, Tag, UserGroup` импортит **4 DROP-ORM** (`Attachment`/`MessageTag`/`Tag`/`UserGroup`) → после снятия классов из `shared/models/__init__.py` = `ImportError`, роняет весь граф (external → `create_app`, worker-синк). Вырезать из `:18` DROP-классы (оставить `MailAccount`, `Message`) и ВСЕ методы, их использующие: tag_id-фильтры в `list_for_user`/`list_for_user_ids`, `is_tag_owned`, `is_tag_visible_to_scope` (Tag/MessageTag/UserGroup); attachment-методы (`list_attachments_bulk`, `has_attachments_bulk`, `get_attachment_*`, `insert_attachment*`, `reserve_attachment_id`, `select_attachment_keys_*`, `has_any_attachments`, attachment-часть `stats_for_user`). **Проверить, что ни один KEEP-путь не зовёт вырезаемое:** external pull-DTO (после A1 без тегов), `crm_push` payload (вложений нет, ADR-0043 §2), `cleanup` (ретенция только `messages`) — все чисты; если найдётся вызов — переписать синхронно. §9-гейт (import/mypy) ловит пропуск машинно.
- **Детач KEEP-`health/router.py`** (смонтирован `main.py:178`; иначе `ImportError` роняет `/healthz`+external): удалить эндпоинты `/api/me` (`:81-87`, читает `user.group_id`+`GroupsRepo`, `users_settings`, `telegram_links`) и `/api/me/settings` (`:142-150`, пишет `users_settings`); снять импорты `GroupsRepo` (`:18`), `TelegramLinksRepo` (`:20`), `UserSettingsRepo` (`:21`) и **`CurrentUser` из `:16`** (`from backend.app.deps import CurrentUser, DbSession` → оставить только `DbSession`; `CurrentUser` снят из `deps.py` в A1, потому health и deps правятся ОДНИМ релизом, см. инвариант §4); из `readyz` убрать S3/MinIO-проверку `get_storage().health_check()` (`:54`) + импорт `get_storage` (`:24`) синхронно с Фазой G. **Оставить** `/healthz` и db/redis-проверку в `readyz`.
- **Детач KEEP-`auth/service.py`** (даёт `seed_crm_service_user` + `CRM_SERVICE_USERNAME` для `write_service.py:35`; иначе `AuditWriter`-импорт роняет граф): снять импорт `AuditWriter` (`:22`) + `self._audit` (`:87`) — удалить session-`AuthService` (login) целиком; удалить `seed_super_admin` (снят из lifespan §5). **Оставить** `seed_crm_service_user` и `CRM_SERVICE_USERNAME`.
- **ORM-мэппинг (§3):** снять `MailAccount.group_id`, `User.group_id`+`User.group`, удалить классы дропаемых таблиц из `shared/models/*` + `__init__.py`. Мэппинг снят — DDL (Фазы C/D/E) выполнять уже безопасно.
- **Снос фронта — `main.py::create_app` (§5).**
- Проверка `getWebhookInfo` 5 ботов → CRM (devops-предусловие снятия `telegram_router`, выполнить до деплоя A3).

*После A3 ни код, ни ORM-мэппинг не ссылаются на снятые таблицы/колонки. Таблицы ещё в БД — откат бесплатен.*

#### Фаза B — БЭКАП перед необратимым (devops, см. §6)

#### Фаза C — Миграция схемы `mail_accounts` (§3): self-seed `crm-service` (§3.1) → repoint `user_id`→`crm-service` → **`SET CONSTRAINTS ALL IMMEDIATE`** (флаш отложенного FK, §3.1) → DROP COLUMN `group_id`
Миграция **самодостаточна** (§3.1): идемпотентно сидит `crm-service` ПЕРЕД repoint'ом, поэтому проходит на пустой БД в CI (`alembic upgrade head` до старта app) и при restore — без зависимости от того, что приложение когда-то засидило ряд. НЕ бросает `RuntimeError` при отсутствии owner'а. **После repoint и ДО `DROP COLUMN` миграция форсирует `SET CONSTRAINTS ALL IMMEDIATE`** (§3.1 шаг 2): self-seed ставит отложенное событие FK `users_group_id_fkey` (`DEFERRABLE INITIALLY DEFERRED`, `20260508_004:124-128`), а single-transaction alembic-цепочка (`migrations/env.py:55-62`) донесла бы его до Фазы E и уронила бы `DROP COLUMN users.group_id` с `pending trigger events` — форсированная проверка снимает очередь здесь.

#### Фаза D — Drop 15 из 16 таблиц (backend, alembic), порядок «referencing → referenced»
`groups` — 16-я, отдельно в Фазе E (нужно предварительно снять входящие FK-колонки):
1. `sent_attachments` 2. `sent_messages` 3. `attachments` 4. `message_tags` 5. `tag_rules` 6. `tags` 7. `telegram_notifications` 8. `telegram_links` 9. `webhook_deliveries` 10. `webhooks` 11. `message_forwards` 12. `group_forwarding` 13. `user_groups` 14. `users_settings` 15. `admin_audit` (**после** дампа Фазы B; audit-writer'ы уже сняты в A3).

#### Фаза E — Развязка `users`↔`groups` и DROP `groups`
- `ALTER TABLE users DROP COLUMN group_id` — снимает зависящие от колонки объекты: FK `users_group_id_fkey` (`users.group_id → groups`, SET NULL) и partial-index `ix_users_group_id_partial` (`WHERE group_id IS NOT NULL`). CHECK `users_role_group_invariant` в живой схеме **отсутствует** — снят исторически миграцией `20260508_005` (auto-create-leader flow требовал его отмены); снимать при drop нечего. Мэппинг `User.group_id`/`User.group` уже снят (A3). `crm-service` (super_admin, group_id NULL) инвариант не нарушает. **Предпосылка:** отложенное событие FK `users_group_id_fkey`, поставленное self-seed'ом `crm-service` в Фазе C, уже снято там же через `SET CONSTRAINTS ALL IMMEDIATE` (§3.1 шаг 2); иначе single-transaction alembic-цепочка (`migrations/env.py:55-62`) донесла бы `pending trigger events` до этого `DROP COLUMN` и уронила бы Фазу E.
- `DROP TABLE groups` — на неё уже не ссылается никто (`mail_accounts.group_id` снят в C, `users.group_id` — выше, `user_groups`/`group_forwarding`/`message_forwards`/`webhooks` дропнуты в D; `groups.leader_user_id RESTRICT → users` уходит с таблицей). Класс `Group`/`relationship` сняты в A3.

#### Фаза F — Редукция `users` до `crm-service`
`DELETE FROM users WHERE username <> 'crm-service'`. Безопасно: ящики repointed (C) → CASCADE `mail_accounts.user_id` не трогает `mail_accounts`; прочие CASCADE-таблицы дропнуты (D); `groups.leader_user_id RESTRICT` снят (E). `crm-service` НЕ удаляется никогда.

#### Фаза G — MinIO/S3 + env-чистка (devops)
- Удалить bucket `mail-attachments` (646 объектов) после бэкапа (§6); убрать MinIO + `minio-bootstrap` из compose, S3-env, `shared/storage.py`; `ensure_bucket` из lifespan (§5).
- Env-чистка: Telegram (`TELEGRAM_BOT_TOKEN`, `BOT_*_TOKEN`/`_GROUP_ID`/`_WEBHOOK_SECRET`, `TELEGRAM_DELIVERY_ENABLED`, `TG_*`), `MAILBOX_DOWN_ALERT_ENABLED`, webhooks (`WEBHOOK_*`), forwarding (`FORWARDING_ENABLED`, `FORWARD_*`), `EXTERNAL_REPLY_ENABLED`/`EXTERNAL_REPLY_RATE_LIMIT_*`, MinIO/S3 (`MINIO_*`, `S3_*`). **Оставить:** `CRM_INGEST_URL`/`CRM_MAILBOX_STATUS_URL`/`CRM_PUSH_SECRET`/`CRM_PUSH_*`, `EXTERNAL_API_KEY`/`EXTERNAL_WRITE_ENABLED`/`LIMIT_EXTERNAL_*`, `MAIL_ENCRYPTION_KEY`, sync/IMAP-параметры. **Outlook-OAuth env (амендмент ADR-0045 §4 — НЕ удалять):** `OUTLOOK_CLIENT_ID`, `OUTLOOK_CLIENT_SECRET`, `OUTLOOK_REDIRECT_URI` (обновить на `{APP_BASE_URL}/api/external/mailboxes/oauth/callback`), `OUTLOOK_TENANT`, `OUTLOOK_OAUTH_STATE_TTL_SECONDS`; **добавить** `CRM_OAUTH_INGEST_URL`. Точный список — за devops (grep по `shared/config.py`), сверяясь с ADR-0045 §4.

**Точка невозврата** — Фазы C–G (drop-миграции + удаление MinIO). До них откат = reverse `setWebhook` + откат CRM.

### §5. Снос фронта (`backend/app/main.py::create_app`)

**Удалить:**
- `app.mount("/static", StaticFiles(...))` (`:159`) и импорты `StaticFiles`/`Path`; блок проверок `static_dir`/`templates_dir` (`:127-158`).
- `include_router` для: `auth_router` (`:166`), `accounts_router` (`:167`), `messages_router` (`:169`), `send_router` (`:170`), `tags_router` (`:171`), `admin_router` (`:172`), `groups_router` (`:173`), `telegram_router` (`:174`), `webhooks_router` (`:175`), `forwarding_router` (`:176`) + их импорты (`:30-57`). **Оставить** `external_router` (`:177`), `health_router` (`:178`). **`oauth_router` (`:168`) — снимается ТОЛЬКО после готовности ADR-0045-замены (§7), не безусловно** в этом релизе.
- Friendly-redirect handler `NotAuthenticatedError → RedirectResponse("/login")` (`:180-186`) и импорт `NotAuthenticatedError`/`RedirectResponse`.
- Middlewares, обслуживавшие только cookie-UI: `CSRFMiddleware`, `MethodOverrideMiddleware`, `SessionMiddleware` (`:121-123`). **Оставить** `SecurityHeadersMiddleware`, `RequestIDMiddleware`.
- В lifespan (`:78-85`): убрать `seed_super_admin`, `seed_builtin_tags`; **оставить** `seed_crm_service_user` (owner ящиков). `ensure_bucket` (`:88-92`) убрать вместе с MinIO (Фаза G).
- Каталоги/модули: `backend/app/templates/` (Jinja), `backend/app/static/` (если есть), `backend/app/templates.py`, `flash.py`, `cookies.py`, `sessions.py`, `csrf.py`; session/CSRF/method-override части `middlewares.py`.

**Оставить работающим:** `health_router` (`/healthz`, `/readyz`), `external_router` (`/api/external/*` — mailbox write §2 + pull для reconcile). `docs_url`/`openapi_url` — на усмотрение devops (можно закрыть).

**Критерий приёмки (наблюдаемый):** после деплоя открытие любого HTML-URL агрегатора (`/`, `/login`, `/accounts`, `/messages`, `/tags`, `/admin`, `/static/*`) → **404**; `/healthz`/`/readyz` → 200; `POST /api/external/mailboxes/{id}/send` и pull-эндпоинты → работают под ключом. Проверяется фактическим запросом (не только чтением кода).

### §6. Что бэкапить перед drop (§4 Фаза B)

`pg_dump` в архив decommission (хранить по ретенции оператора) ДО Фаз D/E:
- **`admin_audit`** — обязательно (TD-050, 248 записей, единственный носитель журнала; в CRM аналога нет).
- Полный снимок дропаемых таблиц как safety-net: `sent_messages`+`sent_attachments`, `attachments`, `tags`+`tag_rules`+`message_tags`, `groups`+`user_groups`, `telegram_links`+`telegram_notifications`, `webhooks`+`webhook_deliveries`, `group_forwarding`+`message_forwards`, `users_settings`, и **`users`** (human-ряды до редукции Фазы F).
- **MinIO bucket `mail-attachments`** (646 объектов) — снапшот перед удалением (Фаза G).
- `mail_accounts.disabled_alert_sent_at` (2 ящика) — уже перенесён в CRM `down_alert_sent_at` при cut-over (ADR-0043 §2); повторно не требуется, зафиксировано для аудита.

Рекомендованная форма — единый `pg_dump -Fc` по списку `--table` + `mc mirror`/`mc cp --recursive` для bucket. Точные команды — devops-runbook (`07-deployment.md`/`SERVER-SETUP.md`).

### §7. Q-0041-1 — судьба `oauth_router` (РЕШЕНО)

`backend/app/oauth/router.py` — **человеко-обращён**: `authorize` требует `CurrentUser` (session), `callback` редиректит на `/accounts` и создаёт ящик от `account.user_id` (session-владелец). В headless-режиме сессий/UI нет → роутер функционировать не может.

**Решение (обновлено — гейт на ADR-0045, не безусловный снос):** session-based `oauth_router` **снимается только ПОСЛЕ** того как headless-замена по **[ADR-0045](./ADR-0045-external-outlook-oauth-headless.md)** (external-OAuth-роуты в сохраняемом `external/router.py` — по ADR-0045 это `POST /api/external/mailboxes/oauth/authorize` + `GET /api/external/mailboxes/oauth/callback`) **реализована и подтверждена**. Убирать человеко-обращённый consent-flow, не имея headless-эквивалента, ЗАПРЕЩЕНО — иначе теряется онбординг/переподключение Outlook-ящиков без пути восстановления. Порядок: (1) ADR-0045 external-oauth-роуты живут → (2) снос `oauth_router` в A3-релизе.

`backend/app/oauth/service.py::OutlookTokenService` (refresh access-token по `oauth_refresh_token_encrypted`) — **остаётся** (worker-синк существующих `oauth_outlook`-ящиков). Согласно ADR-0045 `OutlookOAuthService` и `OUTLOOK_REDIRECT_URI` **НЕ удаляются**, а адаптируются под external-consent — поэтому строки §5 (снятие `oauth_router`) и Фазы G (env-чистка) НЕ трогают эти символы: `redirect_uri`/consent-креды сохраняются (ранее ошибочно помечались «не нужны» — исправлено согласованием с ADR-0045).

**TD-052** (headless re-onboarding) — помечен **РЕШЁН ADR-0045** (замена реализуется отдельной задачей). Существующие ящики с валидным refresh работают без участия человека и до, и после переключения.

> **Источник истины по OAuth-замене — [ADR-0045](./ADR-0045-external-outlook-oauth-headless.md)** (уже принят в дереве, парный CRM `ADR-045`; амендмент к настоящему §7/Фазе A3/Фазе G описан в ADR-0045 §4). Точные имена external-oauth-роутов, объём адаптации `OutlookOAuthService` и перечень сохраняемых `OUTLOOK_*`-env — нормативны по ADR-0045; при расхождении источник истины — ADR-0045.

### §8. Таблица полноты детача: удаляемый символ → его KEEP-потребители → фаза детача

Составлена **исчерпывающим** repo-wide grep по `backend/` + `worker/` для каждого удаляемого символа/модуля. Инвариант приёмки: к моменту удаления символа НИ ОДИН KEEP-модуль (смонтированные `main.py` роутеры `external`/`health`; `deps.py`; реюзаемые external `accounts/service.py`/`send/service.py`; `auth/service.py`-сиды; lifespan; worker sync-путь) его не импортирует. Потребители внутри УДАЛЯЕМЫХ модулей (напр. `messages/`, `admin/`, `tags/router.py`, worker `tg_notify_*`/`webhook_*`/`forward_*`) в таблицу не входят — они уходят вместе с модулем.

| Удаляемый символ / модуль | KEEP-потребитель (`файл:строки`) | Фаза детача |
| --- | --- | --- |
| `ExternalTagsService` (`external/write_service.py`) | `external/router.py:67,455,543-616` | A1 |
| `MessageTagsRepo` (`repositories/tags.py`) | `external/service.py:41,282` | A1 |
| `GroupsRepo` (`repositories/groups.py`) | `external/service.py:38,79`; `accounts/service.py:41,126,413`; `health/router.py:18,82` | A1 (external); A3 (accounts, health) |
| `Tag` (ORM, `shared/models`) | `external/service.py:42`; `repositories/messages.py:18` | A1 (external); A3 (messages.py) |
| `Attachment` / `MessageTag` / `UserGroup` (ORM, `shared/models`) | `repositories/messages.py:18` (+ attachment/tag/visibility-методы) | A3 |
| `TagsService` (`tags/service.py`) | `external/write_service.py:56,193`; `worker/app/sync_cycle.py:48` | A1 (external); A3 (worker) |
| `UserGroupsRepo` (`repositories/user_groups.py`) | `deps.py:30,153` | A1 |
| `SentMessagesRepo` (`repositories/sent_messages.py`) | `send/service.py:32,284,460` | **A2.2** (не A2.1 — новый send просто не зовёт writer; удаление репозитория идёт с A1/A3-релизом, [ADR-0048](./ADR-0048-external-send-contract-and-reply-restore.md) §3) |
| `AuditWriter` (`backend/app/audit/`) | `accounts/service.py:30,127,548`; `auth/service.py:22,87`; `worker/app/sync_cycle.py:41,767,807` | A3 |
| `TelegramLinksRepo` (`repositories/telegram_links.py`) | `health/router.py:20,87` | A3 |
| `UserSettingsRepo` (`repositories/user_settings.py`) | `health/router.py:21,86,142` | A3 |
| `TelegramNotifyService` (`telegram/notify_service.py`) | `worker/app/sync_cycle.py:49,389` | A3 |
| `MAILBOX_ALERT_QUEUE_KEY` (`worker/app/mailbox_alert_dispatch.py`) | `worker/app/sync_cycle.py:59,710` | A3 |
| `_QUEUE_KEY`→`_PUSH_NOTIFY_QUEUE_KEY` (`worker/app/push_notify_dispatch.py`) | `worker/app/sync_cycle.py:60,436` | A3 |
| `get_storage` / MinIO (`shared/storage.py`) | код-читатели: `accounts/service.py:49,128,774`; `health/router.py:24,54`; `worker/app/sync_cycle.py:56,171`; `worker/app/cleanup.py:20,40`; lifespan `backend/app/main.py:62,89` + `worker/app/main.py:36,223` | A3 (снятие вызовов) → G (удаление `storage.py`/сервиса/env) |

**Примечание.** `oauth/service.py` (KEEP, `OutlookTokenService`) в таблице отсутствует намеренно — grep подтвердил, что он НЕ импортит `AuditWriter`/удаляемые репозитории (аудит-вызов был только в `oauth/router.py:20,99`, который сносится, §7).

**⚠️ Статус §8-таблицы.** Эта таблица — **guidance по порядку детача, а НЕ доказательство полноты**. За три раунда ручной перечень обратных зависимостей в ~29k LOC трижды оказывался неполным — это предел метода (чтение не гарантирует нахождение ВСЕХ импортёров), а не невнимательность. **Доказательство полноты даёт машинный §9-гейт** (import/mypy/тесты): любой пропущенный в §8 висячий импорт удалённого символа = красный `import backend.app.main`/`mypy` → фаза не деплоится. §8 экономит итерации (правильный порядок), §9 — ловит остаток.

### §9. Гейт полноты фазы (нормативно — машинное доказательство отсутствия висячих ссылок)

Полнота снятия импортов/чтений удалённых символов гарантируется **машинно, а не §8-таблицей**. Перед деплоем КАЖДОГО code-релиза фаз A (и после каждой DDL-фазы C–G — на предмет рассинхрона ORM↔схема) исполнитель ОБЯЗАН прогнать и получить **зелёное на ВС�ём**:

1. **Импорт API-графа:** `python -c "import backend.app.main"` — весь `create_app()` (все смонтированные роутеры + их транзитивные импорты) поднимается без `ImportError`/`NameError`. Ловит любой висячий импорт удалённого символа в KEEP-графе (external/health/deps/accounts/send/auth/repositories).
2. **Импорт worker-графа:** `python -c "import worker.app.main"` (worker запускается как `python -m worker.app.main`, docstring `worker/app/main.py:2`). Ловит висячие импорты в sync-пути (`sync_cycle`/`cleanup`/`crm_push*`/`crm_status*`).
3. **`mypy` по всему пакету** (команда/конфиг — из `docs/07-deployment.md`/CI). Висячая ссылка на удалённый символ/атрибут (напр. вызов вырезанного метода репозитория, обращение к снятой ORM-колонке) = type-error → красный.
4. **Полный CI-scope тест-прогон** (та же команда и scope, что гейтящий CI-job, `docs/07-deployment.md`/`.github/workflows`), с coverage-порогом.

**Правило:** красный import (§9.1/§9.2) или mypy (§9.3) = висячий импорт/атрибут удалённого символа в KEEP-графе → **СТОП, фаза не деплоится**, пока не зелено. Это и есть настоящие блокеры. §8-таблица указывает ОЖИДАЕМЫЕ места детача (экономит итерации); §9.1-9.3 **доказывают**, что не осталось ни одного непойманного import/attribute-случая — даже если §8 что-то не назвала, компилятор/импорт/тайпчекер поймают до прода. Это снимает с исполнителя (и ревьюера) невыполнимую нагрузку «перечислить ВСЕХ импортёров вручную».

**Оговорка A — orphaned-тесты ≠ блокер (§9.4).** A3 намеренно по этому ADR удаляет модули (`tags`/`telegram`/`webhooks`/`forwarding`/`groups`/`admin`/`messages`) и ORM-классы; существующие тесты, импортящие их на module-level (напр. `tests/worker/test_forwarding_service.py`, `test_forward_dispatch_pipeline.py`, `test_message_forwards_repo.py`, ряд `tests/integration/external/*` с `Tag`/`Group`/`SentMessage`), ожидаемо падают на collection. Красный §9.4 **исключительно из-за orphaned-тестов на удалённые ЭТИМ ADR модули/сущности** — это **штатный qa-хендофф** (`blame: test`, глобальная норма CLAUDE.md), а НЕ ошибка импорта исполнителя: `qa` удаляет/переписывает такие тесты под новый контракт **в том же релизе** фазы, после чего §9.4 обязан быть зелёным. Отличать от §9.1/§9.2 (import) и §9.3 (mypy) — те красные = настоящие висячие ссылки в KEEP-графе, чинит исполнитель. **A3-зависимость:** снятие qa-тестов удаляемых модулей планируется синхронно с A3-релизом.

**Оговорка B — что §9.1-9.3 НЕ ловят.** Два класса висячих ссылок import/mypy пропускают: (а) **сырой SQL по дропнутой таблице** (не ORM — напр. `text("SELECT nextval('attachments_id_seq')")`, `repositories/messages.py:485`, внутри attachment-методов, снимаемых в A3); (б) **динамический импорт/`getattr`**. Их гарантирует **§3 lock-step** (снятие ВСЕХ код-читателей, включая `text()`-SQL по дропаемой таблице) + **рантайм §9.4** (обращение к дропнутой таблице/колонке в тестируемом пути бросит `ProgrammingError`/`UndefinedColumn`/`UndefinedTable`). §9.1-9.3 покрывают именно import/attribute-класс; raw-SQL/динамика — на §3 + §9.4. (В текущем коде живого KEEP-экземпляра raw-SQL по дропаемой таблице нет — единственный сидит в снимаемых attachment-методах `messages.py`.)

## Consequences

- Поверхность агрегатора сжимается до connector'а: `mail_accounts`+`messages`+`users(1 ряд)`, IMAP-синк, SMTP-send, push-outbox в CRM, mailbox status-канал, внешний mailbox-API. 16 таблиц, весь UI/static, MinIO, Telegram/webhooks/forwarding/tags/groups — удалены.
- Сервис нельзя открыть: все HTML-URL → 404; живы только `/healthz`, `/readyz`, `/api/external/*`.
- Необратимость локализована в Фазах C–G и покрыта бэкапом (§6). До них — bloodless rollback.
- Новые заведены: `TD-051` (рудиментарные колонки `users` — необязательный cleanup). `TD-052` (headless OAuth-consent re-onboarding) — **закрыт [ADR-0045](./ADR-0045-external-outlook-oauth-headless.md)** (external-consent-flow восстанавливает онбординг Outlook-ящиков; снос session-`oauth_router` гейтится готовностью ADR-0045-роутов, §7). `TD-049`/`TD-050` — ссылаются на этот ADR как на исполнимый runbook.
- docs-sync нормативных документов агрегатора (`03-data-model`/`04-api-contracts`/`05-modules`/`06-security`/`07-deployment`/`08-frontend`/`README` глоссарий) под снятые подсистемы выполняется architect'ом **синхронно с реализацией** соответствующего шага backend/devops (чтобы docs не описывали уже/ещё несуществующий код раньше времени) — ведётся под `TD-050` (в). Настоящий ADR — план; вычистка разделов — по мере исполнения.
- Статус-колонка `INDEX.md` для 15 ADR, superseded ADR-0043, и narrowed-пометки ADR-0039/0041 — под `TD-050` (б).
- **Амендмент §3.1 (self-seed `crm-service` в миграции Фазы C).** Миграция `20260715_025` из зависимой от app-lifespan (бросала `RuntimeError` без owner'а) переведена в самодостаточную: сама идемпотентно сидит `crm-service` перед repoint'ом. Устраняет CI-блокер (падение `alembic upgrade head` на пустой БД до старта app, `ci.yml:200`) и скрытую связность «схема зависит от засиженных приложением данных»; корректна на проде, в CI и при restore. **Побочно** self-seed (`INSERT` в `users`) ставит отложенное событие FK `users_group_id_fkey` (`DEFERRABLE INITIALLY DEFERRED`, `20260508_004:124-128`), которое в single-transaction alembic-цепочке (`migrations/env.py:55-62`) дожило бы до Фазы E и уронило бы `DROP COLUMN users.group_id` с `pending trigger events`; §3.1 поэтому нормирует **`SET CONSTRAINTS ALL IMMEDIATE`** после repoint'а — форсированная проверка тривиально проходит (`crm-service.group_id IS NULL`) и опустошает очередь до drop-миграций. Реализация — `backend` (правка `_crm_service_id` + INSERT + `SET CONSTRAINTS ALL IMMEDIATE` в `migrations/versions/20260715_025_*.py::upgrade`, `:134`) по §3.1; дополнительных DDL/фаз не требует. Новый TD не заводится (устранение дефекта, не отложенный долг).

## Alternatives considered

- **Дропнуть `users`/`mail_accounts.user_id` каскадно.** Отклонён (ADR-0043 §4): дороже по миграции, риск потери ящиков; единый `crm-service`-owner достаточен и уже существует.
- **Оставить `mail_accounts.group_id` (просто NULL).** Отклонён: `groups` дропается, висячий FK на несуществующую таблицу невозможен — колонку надо снять. Дроп колонки чище, чем оставлять FK-less int без смысла.
- **Одна большая drop-миграция без промежуточного снятия кода.** Отклонён: `sync_cycle`/`main.py` читают таблицы; дроп до снятия кода → рантайм-падения connector'а. Сначала код, потом схема (§4).
- **Миграция Фазы C полагается на app-lifespan `seed_crm_service_user` (бросает `RuntimeError`, если `crm-service` нет).** Отклонён (см. §3.1): миграция схемы, зависящая от того, что приложение когда-то стартовало и засидило данные, — скрытая связность. Она падает на пустой БД в CI (`alembic upgrade head` до старта app, `ci.yml:200` → red `Test` job → build/deploy `skipped` → DDL не доезжает до прода) и при restore из бэкапа. На проде «проходит» лишь потому, что прежние бусты app уже засидили ряд — недопустимая неявная предпосылка. Принят вариант **A** (self-seed в миграции).
- **CI-workflow сидит `crm-service` отдельным шагом перед DDL-миграциями (вариант B).** Отклонён: чинит только CI, оставляя миграцию несамодостаточной (restore из бэкапа/новый инстанс по-прежнему требуют внешнего сидирования), смешивает данные и схему в CI-конфиге и сохраняет ту же скрытую связность. Self-seed в самой миграции (A) устраняет причину, а не симптом.
- **Оставить session-`oauth_router` для consent.** Отклонён: зависит от session/UI, которые сносятся; не работает headless (§7). Consent-flow восстановлен headless внешними роутами [ADR-0045](./ADR-0045-external-outlook-oauth-headless.md) (снос session-роутера гейтится их готовностью); `OutlookOAuthService`/`OUTLOOK_*` сохранены и адаптированы, gap закрыт (не TD).
- **Снять pull-эндпоинты (`GET /messages`,`/mailboxes`) сразу.** Отклонён (владелец, ADR-0043 §4): нужны для сверки/reconcile после миграции; снимаются отдельным поздним шагом.
