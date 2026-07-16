# Mail Aggregator — Документация

Это единственный источник истины по проекту. Любое расхождение между кодом и документацией решается в пользу документации либо через обновление документации архитектором.

> **⚠️ ДЕМОНТАЖ ВЫПОЛНЕН (2026-07-15) — агрегатор сведён к headless mail-коннектору.** По [ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md) сняты: теги, Telegram (SSO/нотификации/боты), outbound webhooks, forwarding, группы/роли/пользователи, Jinja-UI и Mini App, вложения/MinIO, message-scoped reply.
>
> **Что такое агрегатор сегодня:** IMAP-синк → `messages` → push в CRM (`POST /api/mail/ingest`, HMAC) + статус-канал ящика ([ADR-0046](./adr/ADR-0046-mailbox-status-hook-points.md)); исходящее — `POST /api/external/mailboxes/{id}/send` ([ADR-0048](./adr/ADR-0048-external-send-contract-and-reply-restore.md)); mailbox-CRUD и Outlook-OAuth — `/api/external/*` ([ADR-0039](./adr/ADR-0039-external-write-api.md) / [ADR-0045](./adr/ADR-0045-external-outlook-oauth-headless.md)). **БД — 4 таблицы** (`alembic_version`/`mail_accounts`/`messages`/`users` с единственным рядом `crm-service`), ревизия `20260715_028`. Смонтированы только `external_router` + `health_router` (`backend/app/main.py:99-100`); **любой HTML-URL → `404`**. Всё человеко-обращённое — на стороне CRM.
>
> Документы ниже написаны ДО демонтажа и вычищены **посекционными историческими маркерами** (`TD-050`, закрыт 2026-07-16): раздел с маркером `⚠️ ИСТОРИЧЕСКИЙ РАЗДЕЛ` описывает подсистему, которой в коде НЕТ. Актуальную поверхность смотри по маркерам «Актуально».

## Состав

| Файл | Назначение | Статус |
| --- | --- | --- |
| [`README.md`](./README.md) | Карта документации (этот файл). | actual |
| [`01-architecture.md`](./01-architecture.md) | C4 (Context / Containers / Components), Mermaid-диаграммы, sequence-диаграммы ключевых сценариев. | **до-демонтажный**; актуальная топология — в баннере файла, разделы помечены посекционно |
| [`02-tech-stack.md`](./02-tech-stack.md) | Все используемые технологии, версии, обоснование. | **частично исторический** (MinIO/S3, Jinja2, Telegram-строки — сняты) |
| [`03-data-model.md`](./03-data-model.md) | Полная схема БД (таблицы, поля, FK, индексы), ER-диаграмма. | **до-демонтажный** (19 таблиц); в проде **4** — KEEP/DROP в баннере файла |
| [`04-api-contracts.md`](./04-api-contracts.md) | Все REST endpoints (auth / user / admin), схемы request/response, коды ошибок. | **до-демонтажный**; действующая поверхность — `/healthz`, `/readyz`, `/api/external/*` (см. баннер §8) |
| [`05-modules.md`](./05-modules.md) | Модули backend + frontend: ответственность, публичный API, зависимости, инварианты. Базис для исполнителей. | **до-демонтажный**; живые модули — в баннере §0 |
| [`06-security.md`](./06-security.md) | STRIDE-анализ, шифрование почтовых паролей (AES-256-GCM), хеширование паролей (argon2id), сессии, CSRF, rate-limit, audit log, ротация ключей. | **частично исторический** (сессии §5, audit §8, матрица ролей §9a, MinIO §12 — сняты); §2/§4/§6/§7/§9–§13 в силе |
| [`07-deployment.md`](./07-deployment.md) | docker-compose, env-переменные, volumes, healthchecks, TLS (host-certbot), CI/CD, observability, migration to a new server (sec. 15). | **частично исторический** (MinIO §12, Telegram bot setup §14 — сняты) |
| [`SERVER-SETUP.md`](./SERVER-SETUP.md) | Операторский runbook: провижининг хоста, DNS, host-certbot, GitHub Secrets, деплой, бэкапы, ротация секретов. Процедурный компаньон к `07-deployment.md`. | actual (сверить MinIO-шаги с `07` §12 — сняты) |
| [`08-frontend.md`](./08-frontend.md) | UX-флоу, wireframe-описания страниц, список Jinja2-шаблонов и JS-компонентов. | **ИСТОРИЧЕСКИЙ ЦЕЛИКОМ** — собственного frontend'а у агрегатора нет ([ADR-0041](./adr/ADR-0041-disable-jinja-ui.md)) |
| [`100-known-tech-debt.md`](./100-known-tech-debt.md) | Реестр осознанных компромиссов и отложенных работ (TD-NNN). | actual |
| [`adr/INDEX.md`](./adr/INDEX.md) | Реестр всех архитектурных решений (ADR). | actual |

## Глоссарий

> **⚠️ Термины ниже, помеченные `(СНЯТО)`, относятся к до-демонтажному агрегатору и в коде/проде НЕ существуют** ([ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md), 2026-07-15). Они сохранены для чтения исторических разделов и ADR.
>
> **Действующий словарь коннектора:** **Mail account** (ящик; владелец — всегда `crm-service`), **Message** (входящее письмо; push-outbox в CRM), **Sync cycle**, **External pull-API**, **Push в CRM** (`POST /api/mail/ingest`, HMAC `CRM_PUSH_SECRET`), **Статус-канал ящика** ([ADR-0046](./adr/ADR-0046-mailbox-status-hook-points.md) — `last_synced_at`/`last_sync_error`/`is_active` зеркалятся в CRM), **`crm-service`** (единственный технический владелец всех ящиков; ролей/логина нет), **Обобщённый send** (`POST /api/external/mailboxes/{id}/send`, [ADR-0048](./adr/ADR-0048-external-send-contract-and-reply-restore.md)).

| Термин | Определение |
| --- | --- |
| **Super-admin** | **(СНЯТО — ролей/логина нет; единственный ряд `users` = технический `crm-service`)** Единственный аккаунт с правами управления пользователями. Заводится из env при первом старте. |
| **User** | **(СНЯТО — человеко-пользователей нет)** Конечный пользователь сервиса. Создаётся super-админом. Логинится в UI, управляет своими почтовыми аккаунтами. |
| **Mail account** | Привязанный к пользователю IMAP+SMTP-аккаунт внешнего провайдера (Gmail и т.п.). **После демонтажа:** владелец — всегда технический `crm-service`; колонка `group_id` дропнута ([ADR-0044](./adr/ADR-0044-decommission-runbook.md) Фаза C). |
| **Message** | Закэшированное входящее письмо (заголовки + plain-text + список вложений). **После демонтажа:** вложений нет; `messages` — push-outbox в CRM (`+pushed_at`). |
| **Sent message** | **(СНЯТО — таблица `sent_messages` дропнута; идентификатор отправки выдаёт CRM, [ADR-0048](./adr/ADR-0048-external-send-contract-and-reply-restore.md))** Отправленное через сервис письмо (новое или ответ). |
| **Attachment** | **(СНЯТО — `attachments`/`sent_attachments` дропнуты, MinIO снят Фазой G)** Вложение письма; данные хранятся в MinIO, метаданные — в БД. |
| **Sync cycle** | Полный проход worker'а по всем активным mail-аккаунтам (раз в 5 минут). |
| **Audit log** | **(СНЯТО — `admin_audit` дропнут (задамплен в бэкап); аудит CRM лог-based)** Журнал действий супер-админа (создание/удаление/сброс пользователя, логин). |
| **Tag** | **(СНЯТО — теги целиком в CRM)** Per-user классификационная метка для писем; rule-based авто-применение при синке (см. [ADR-0017](./adr/ADR-0017-tags.md)). |
| **Tag rule** | **(СНЯТО — движок матчинга перенесён в CRM ПОБУКВЕННО)** Условие срабатывания тега: `subject_contains` / `body_contains` / `sender_contains` / `sender_exact` (substring case-insensitive). Несколько rules одного тега соединены OR. |
| **Builtin tag** | **(СНЯТО)** Встроенный тег (`is_builtin=true`), создаётся автоматически при первом login пользователя. На старте — 4 штуки: `DPLA.PLA`, `Диспут`, `Отменить подписку`, `Продление аккаунта`. Удалять нельзя; rules/name/color редактируемы. |
| **Role** | **(СНЯТО — ролей нет)** Роль пользователя (`users.role`): `super_admin` \| `group_leader` \| `group_member`. Заменяет старое `is_admin: bool`. См. [ADR-0019](./adr/ADR-0019-groups-and-roles.md). |
| **Group** | **(СНЯТО — `groups`/`user_groups` дропнуты; команда живёт в CRM)** Рабочая группа пользователей (таблица `groups`). У группы ровно один лидер (`groups.leader_user_id UNIQUE`) и 0..N участников; все участники видят и управляют mail-аккаунтами и письмами всех в группе. См. [ADR-0019](./adr/ADR-0019-groups-and-roles.md). |
| **Group leader** | **(СНЯТО)** Пользователь с `role='group_leader'`. Лидер ровно одной группы (1:1). Создаётся super-admin'ом; имя группы по умолчанию формируется как «Группа {display_name \| username}». Может управлять mail-аккаунтами/письмами всей группы; user-management не имеет (его делает только super_admin). |
| **Group member** | **(СНЯТО)** Пользователь с `role='group_member'`. Привязан к одной группе. Видит и управляет mail-аккаунтами/письмами всех участников своей группы (включая лидера). |
| **Display name (user)** | **(СНЯТО — вместе с человеко-пользователями)** `users.display_name TEXT NULL` — человекочитаемое имя пользователя для UI (Алиса Иванова). Опционально; UI fallback на `username`. См. [ADR-0019](./adr/ADR-0019-groups-and-roles.md) §2. |
| **Mail account nickname** | `mail_accounts.display_name TEXT NULL` — короткий ярлык для ящика (например, «Apple Test 1»). Опциональный; UI fallback на `email`. Помогает быстро ориентироваться в общих группах ящиков. См. [ADR-0020](./adr/ADR-0020-mail-account-nickname.md). |
| **Visibility scope** | **(СНЯТО — видимости нет; единственный владелец `crm-service`)** dataclass `(user_id, role, group_id)`, инкапсулирующий, какие mail-accounts и messages видит текущий пользователь. Создаётся в FastAPI dependency, пробрасывается в Service-методы для построения SQL WHERE-фильтра. См. [ADR-0019](./adr/ADR-0019-groups-and-roles.md) §7. |
| **Outbound webhook** | **(СНЯТО — подсистема удалена, в проде было 0)** HTTP POST на пользовательский URL команды при появлении нового письма с тегом в любом из её ящиков. Один webhook на группу (`webhooks.group_id UNIQUE`). Auth — static `X-Webhook-Secret` header. Конфигурируется лидером команды; super_admin может создать для любой команды. См. [ADR-0023](./adr/ADR-0023-outbound-webhooks.md). |
| **Mail forwarding (переадресация)** | **(СНЯТО — forwarding в CRM)** Пересылка **всех новых** входящих писем ящиков команды на один e-mail лидера (`group_forwarding`, одна запись на команду, `group_id UNIQUE`). Настраивается лидером на `/my/integrations` (super_admin — через `?group_id=`). Отправитель форварда = ящик, получивший письмо (его SMTP-креды; `From`=ящик, `To`=лидер, блок «пересланное сообщение»). Содержимое целиком (text+html+вложения из MinIO; oversized пропускаются). Только ящики команды (`group_id NOT NULL`); дедуп — `message_forwards` (UNIQUE `message_id,group_id`, claim до отправки); loop-guard через `X-Forwarded-By`. Fire-and-forget без retry/recovery (TD-043). См. [ADR-0034](./adr/ADR-0034-leader-mail-forwarding.md). |
| **External pull-API** | `GET /api/external/messages?since_id&limit` — доверенный B2B-партнёр инкрементально **сам забирает** (pull) ВСЕ письма системы (super_admin visibility) keyset'ом по `messages.id` (id ASC, без пропусков/дублей курсора) с canonical-дедупом дубль-ящиков (`mail_account_id IN list_canonical_account_ids()` — один email в двух командах отдаётся одной копией, консистентно с super_admin inbox). Сырое полное тело (`body_text`+`body_html` без collapse-нормализации), `to_addrs`/`cc_addrs` included, вложения нет. Auth — static `EXTERNAL_API_KEY` (`X-API-Key`/`Bearer`, `compare_digest`). Отдельный версионируемый `ExternalMessageDTO`. См. [ADR-0029](./adr/ADR-0029-external-pull-api.md). **После демонтажа:** canonical-дедуп дубль-ящиков более не нужен (дубли схлопнуты Фазой C, `8cb3099`); фильтр `group_id` **снят** (групп нет), фильтр `mail_account_id` — в силе. |

## Принципы

1. **Простота >> универсальность.** ~5 пользователей × 100 ящиков. Не строим то, что не нужно.
2. **Single source of truth = `docs/`.** Все архитектурные решения проходят через ADR.
3. **Безопасность на первом месте для секретов.** Почтовые пароли шифруются AES-256-GCM, мастер-ключ из env. Хешируем пароли argon2id. CSRF, rate-limit, audit log — обязательны.
4. **Никаких TODO/FIXME в документации.** Открытые вопросы выделяются в раздел Open Questions с ID Q-NNN-N.

## Open Questions

На момент написания документации не осталось неразрешённых вопросов, блокирующих имплементацию.

**Формат ID:** `Q-{NNN}-{N}`, где
- `NNN` — порядковый трёхзначный номер вопроса (`001`, `002`, …); глобальный счётчик ведётся прямо в этом файле, в реестре ниже.
- `N` — однозначный номер sub-вопроса внутри этого Q (для случая, когда один вопрос разворачивается в несколько связанных подвопросов; обычно `1`).

Примеры: `Q-001-1`, `Q-002-1`, `Q-002-2`, `Q-003-1`.

Любые новые вопросы по мере реализации архитектор добавляет в **реестр open questions** ниже и в соответствующий модульный/cross-cutting файл (`docs/modules/<M>/99-open-questions.md` либо `docs/99-open-questions.md`). При закрытии — пишется решение со ссылкой на ADR/диф документации; запись остаётся для аудита.

### Реестр open questions

> **⚠️ Вопросы по СНЯТЫМ подсистемам закрыты демонтажём (2026-07-16).** Q-001-1 / Q-002-1 / Q-WH-1 / Q-WH-2 / Q-WH-3 / Q-MTG-2 более не подлежат решению в агрегаторе: Telegram и webhooks целиком переехали в CRM ([ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md)). **Единственный действительно открытый вопрос — `Q-OAUTH-3`** (OAuth/XOAUTH2 жив, [ADR-0045](./adr/ADR-0045-external-outlook-oauth-headless.md); ведётся как `TD-030`/`TD-031`); `Q-OAUTH-1`/`Q-OAUTH-2` переформулированы демонтажём — consent теперь headless (`state` в Redis + `crm_state`, cookie не нужны — ровно то, что предлагал Q-OAUTH-1).

| ID | Где задан | Кратко | Статус |
| --- | --- | --- | --- |
| ~~Q-001-1~~ | [ADR-0022](./adr/ADR-0022-telegram-sso-and-notifications.md) §1.4 | Anti-replay set для `init_data` сверх TTL (`tg_seen:{auth_date}:{hash[:8]}` в Redis 5min)? | **closed 2026-07-16 — снято демонтажём** (Telegram-подсистема удалена, `TD-018` закрыт). SSO/`init_data` в агрегаторе нет; anti-replay переоценивается в CRM. |
| ~~Q-002-1~~ | [ADR-0022](./adr/ADR-0022-telegram-sso-and-notifications.md) §2.7 | UI toggle для opt-out push-нотификаций — где разместить (Settings page / admin)? | **closed 2026-07-16 — снято демонтажём**: UI и `users_settings` удалены, `PATCH /api/me/settings` не существует. Настройки нотификаций — в CRM. |
| ~~Q-003-1~~ | [ADR-0022](./adr/ADR-0022-telegram-sso-and-notifications.md) §1.3 | Persistent SSO в `complete_set_password` flow? | closed by ADR-0022 — да, читаем `mas_tg_pending` cookie и создаём линковку. |
| ~~Q-WH-1~~ | [ADR-0023](./adr/ADR-0023-outbound-webhooks.md) §2.5 | Двойной secret при rotate (старый valid M минут) для grace-period на receiver-side? | **closed 2026-07-16 — снято демонтажём**: webhook-подсистема удалена (`TD-019` закрыт), rotate-secret не существует. |
| ~~Q-WH-2~~ | [ADR-0023](./adr/ADR-0023-outbound-webhooks.md) «Consequences» | UI для super_admin — список всех webhook'ов всех команд? | **closed 2026-07-16 — снято демонтажём**: webhooks и UI удалены; вопрос неприменим. |
| ~~Q-WH-3~~ | [ADR-0023](./adr/ADR-0023-outbound-webhooks.md) §2.9 | Включать ли список attachments (id, filename, size) в payload (без bytes)? | **closed 2026-07-16 — снято демонтажём**: webhooks удалены, вложений у коннектора нет (MinIO снят Фазой G). |
| ~~Q-WH-4~~ | [ADR-0023](./adr/ADR-0023-outbound-webhooks.md) §4.1 | Обрабатывает ли `mas-cli reencrypt` `webhooks.secret_encrypted`? | closed by ADR-0023 — да, общий `version_byte` механизм; backend-агент добавляет таблицу `webhooks` в список re-encrypt. |
| ~~Q-MTG-1~~ | [ADR-0024](./adr/ADR-0024-multi-telegram-links.md) §5 | `POST /logout` сбрасывает ВСЕ TG-привязки user'а или только текущую? | closed (round-43, 2026-06-11) — по итогам прод-инцидента «само-разлогинивание push» пользователь решил: **logout НЕ сбрасывает привязки вовсе** (расцеплён с Telegram-привязкой). Отвязка — только явной кнопкой «Отвязать» (`DELETE /api/telegram/links/{id}`). См. ADR-0022 §1.5 «round-43», ADR-0024 §5. |
| ~~Q-MTG-2~~ | [ADR-0024](./adr/ADR-0024-multi-telegram-links.md) §8 | Допустимо ли при миграции потерять точный `chat_id` у исторических `telegram_notifications` без живого линка (ставим `0`)? | **closed 2026-07-16 — снято демонтажём**: таблица `telegram_notifications` дропнута (`TD-028` закрыт), legacy-строк не существует. |
| ~~Q-OAUTH-1~~ | [ADR-0025](./adr/ADR-0025-outlook-oauth2.md) §2 | Callback приходит в OctoBrowser-профиле без cookie сессии — как связать с инициировавшим user'ом? | **closed 2026-07-16 — решён демонтажём/[ADR-0045](./adr/ADR-0045-external-outlook-oauth-headless.md)**: consent headless, cookie не участвуют — `state` в Redis (`OAuthState{code_verifier, crm_state}`) связывает callback; владелец создаваемого ящика — всегда `crm-service`. Ровно предложенный вариант. |
| ~~Q-OAUTH-2~~ | [ADR-0025](./adr/ADR-0025-outlook-oauth2.md) §2 | PKCE S256 обязателен для confidential client (client_secret) + personal accounts? | **closed 2026-07-16 — подтверждено реализацией**: PKCE S256 + `client_secret` сосуществуют, конфликта нет (`backend/app/oauth/service.py`, ADR-0025 §4 / [ADR-0045](./adr/ADR-0045-external-outlook-oauth-headless.md)). |
| Q-OAUTH-3 | [ADR-0025](./adr/ADR-0025-outlook-oauth2.md) §Context/§4 | **БЛОКЕР e2e:** personal accounts реально выдают IMAP/SMTP XOAUTH2 + нужные scopes? Версии `imap-tools`/`aiosmtplib` строят XOAUTH2? | **open (единственный действующий)** — OAuth/XOAUTH2 **жив** и сохранён [ADR-0045](./adr/ADR-0045-external-outlook-oauth-headless.md); ведётся как `TD-030` (XOAUTH2 через внутренние API `imap-tools`/`aiosmtplib`) и `TD-031` (e2e на реальном Azure App не подтверждён). |
| ~~Q-0029-1~~ | [ADR-0029](./adr/ADR-0029-external-pull-api.md) §Open questions | External pull-API: передавать ли вложения? | closed by ADR-0029 — **no**. Только метаданные письма + тело. Расширение — отдельный ADR при запросе. |
| ~~Q-0029-2~~ | [ADR-0029](./adr/ADR-0029-external-pull-api.md) §Open questions | External pull-API: включать ли `to_addrs`/`cc_addrs`? | closed by ADR-0029 — **included** (`to_addrs` всегда строка, `cc_addrs` nullable). |

Домены open questions: `Q-WH-*` (webhooks), `Q-MTG-*` (multi-telegram, ADR-0024), `Q-OAUTH-*` (Outlook OAuth, ADR-0025), `Q-0029-*` (external pull-API, ADR-0029). Числовой `Q-NNN-*` — следующий свободный `NNN`: **004**.
