# Mail Aggregator — Документация

Это единственный источник истины по проекту. Любое расхождение между кодом и документацией решается в пользу документации либо через обновление документации архитектором.

## Состав

| Файл | Назначение | Статус |
| --- | --- | --- |
| [`README.md`](./README.md) | Карта документации (этот файл). | actual |
| [`01-architecture.md`](./01-architecture.md) | C4 (Context / Containers / Components), Mermaid-диаграммы, sequence-диаграммы ключевых сценариев. | actual |
| [`02-tech-stack.md`](./02-tech-stack.md) | Все используемые технологии, версии, обоснование. | actual |
| [`03-data-model.md`](./03-data-model.md) | Полная схема БД (таблицы, поля, FK, индексы), ER-диаграмма. | actual |
| [`04-api-contracts.md`](./04-api-contracts.md) | Все REST endpoints (auth / user / admin), схемы request/response, коды ошибок. | actual |
| [`05-modules.md`](./05-modules.md) | Модули backend + frontend: ответственность, публичный API, зависимости, инварианты. Базис для исполнителей. | actual |
| [`06-security.md`](./06-security.md) | STRIDE-анализ, шифрование почтовых паролей (AES-256-GCM), хеширование паролей (argon2id), сессии, CSRF, rate-limit, audit log, ротация ключей. | actual |
| [`07-deployment.md`](./07-deployment.md) | docker-compose, env-переменные, volumes, healthchecks, CI/CD, observability. | actual |
| [`08-frontend.md`](./08-frontend.md) | UX-флоу, wireframe-описания страниц, список Jinja2-шаблонов и JS-компонентов. | actual |
| [`100-known-tech-debt.md`](./100-known-tech-debt.md) | Реестр осознанных компромиссов и отложенных работ (TD-NNN). | actual |
| [`adr/INDEX.md`](./adr/INDEX.md) | Реестр всех архитектурных решений (ADR). | actual |

## Глоссарий

| Термин | Определение |
| --- | --- |
| **Super-admin** | Единственный аккаунт с правами управления пользователями. Заводится из env при первом старте. |
| **User** | Конечный пользователь сервиса. Создаётся super-админом. Логинится в UI, управляет своими почтовыми аккаунтами. |
| **Mail account** | Привязанный к пользователю IMAP+SMTP-аккаунт внешнего провайдера (Gmail и т.п.). |
| **Message** | Закэшированное входящее письмо (заголовки + plain-text + список вложений). |
| **Sent message** | Отправленное через сервис письмо (новое или ответ). |
| **Attachment** | Вложение письма; данные хранятся в MinIO, метаданные — в БД. |
| **Sync cycle** | Полный проход worker'а по всем активным mail-аккаунтам (раз в 5 минут). |
| **Audit log** | Журнал действий супер-админа (создание/удаление/сброс пользователя, логин). |
| **Tag** | Per-user классификационная метка для писем; rule-based авто-применение при синке (см. [ADR-0017](./adr/ADR-0017-tags.md)). |
| **Tag rule** | Условие срабатывания тега: `subject_contains` / `body_contains` / `sender_contains` / `sender_exact` (substring case-insensitive). Несколько rules одного тега соединены OR. |
| **Builtin tag** | Встроенный тег (`is_builtin=true`), создаётся автоматически при первом login пользователя. На старте — 4 штуки: `DPLA.PLA`, `Диспут`, `Отменить подписку`, `Продление аккаунта`. Удалять нельзя; rules/name/color редактируемы. |
| **Role** | Роль пользователя (`users.role`): `super_admin` \| `group_leader` \| `group_member`. Заменяет старое `is_admin: bool`. См. [ADR-0019](./adr/ADR-0019-groups-and-roles.md). |
| **Group** | Рабочая группа пользователей (таблица `groups`). У группы ровно один лидер (`groups.leader_user_id UNIQUE`) и 0..N участников; все участники видят и управляют mail-аккаунтами и письмами всех в группе. См. [ADR-0019](./adr/ADR-0019-groups-and-roles.md). |
| **Group leader** | Пользователь с `role='group_leader'`. Лидер ровно одной группы (1:1). Создаётся super-admin'ом; имя группы по умолчанию формируется как «Группа {display_name \| username}». Может управлять mail-аккаунтами/письмами всей группы; user-management не имеет (его делает только super_admin). |
| **Group member** | Пользователь с `role='group_member'`. Привязан к одной группе. Видит и управляет mail-аккаунтами/письмами всех участников своей группы (включая лидера). |
| **Display name (user)** | `users.display_name TEXT NULL` — человекочитаемое имя пользователя для UI (Алиса Иванова). Опционально; UI fallback на `username`. См. [ADR-0019](./adr/ADR-0019-groups-and-roles.md) §2. |
| **Mail account nickname** | `mail_accounts.display_name TEXT NULL` — короткий ярлык для ящика (например, «Apple Test 1»). Опциональный; UI fallback на `email`. Помогает быстро ориентироваться в общих группах ящиков. См. [ADR-0020](./adr/ADR-0020-mail-account-nickname.md). |
| **Visibility scope** | dataclass `(user_id, role, group_id)`, инкапсулирующий, какие mail-accounts и messages видит текущий пользователь. Создаётся в FastAPI dependency, пробрасывается в Service-методы для построения SQL WHERE-фильтра. См. [ADR-0019](./adr/ADR-0019-groups-and-roles.md) §7. |
| **Outbound webhook** | HTTP POST на пользовательский URL команды при появлении нового письма с тегом в любом из её ящиков. Один webhook на группу (`webhooks.group_id UNIQUE`). Auth — static `X-Webhook-Secret` header. Конфигурируется лидером команды; super_admin может создать для любой команды. См. [ADR-0023](./adr/ADR-0023-outbound-webhooks.md). |

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

| ID | Где задан | Кратко | Статус |
| --- | --- | --- | --- |
| Q-001-1 | [ADR-0022](./adr/ADR-0022-telegram-sso-and-notifications.md) §1.4 | Anti-replay set для `init_data` сверх TTL (`tg_seen:{auth_date}:{hash[:8]}` в Redis 5min)? | open — отложено в `100-known-tech-debt.md` (TD-018); реализовать при появлении реального риска. |
| Q-002-1 | [ADR-0022](./adr/ADR-0022-telegram-sso-and-notifications.md) §2.7 | UI toggle для opt-out push-нотификаций — где разместить (Settings page / admin)? | open — backend endpoint `PATCH /api/me/settings` готов; frontend-агенту решить в следующем sprint. |
| ~~Q-003-1~~ | [ADR-0022](./adr/ADR-0022-telegram-sso-and-notifications.md) §1.3 | Persistent SSO в `complete_set_password` flow? | closed by ADR-0022 — да, читаем `mas_tg_pending` cookie и создаём линковку. |
| Q-WH-1 | [ADR-0023](./adr/ADR-0023-outbound-webhooks.md) §2.5 | Двойной secret при rotate (старый valid M минут) для grace-period на receiver-side? | open — отложено в `100-known-tech-debt.md` (TD-019); MVP — instant cut. |
| Q-WH-2 | [ADR-0023](./adr/ADR-0023-outbound-webhooks.md) «Consequences» | UI для super_admin — список всех webhook'ов всех команд? | open — super_admin использует `?group_id=` per request или psql. Full UI — следующая итерация. |
| Q-WH-3 | [ADR-0023](./adr/ADR-0023-outbound-webhooks.md) §2.9 | Включать ли список attachments (id, filename, size) в payload (без bytes)? | open — receiver получает через наш API. Если будет запрос — non-breaking add `attachments: [...]`. |
| ~~Q-WH-4~~ | [ADR-0023](./adr/ADR-0023-outbound-webhooks.md) §4.1 | Обрабатывает ли `mas-cli reencrypt` `webhooks.secret_encrypted`? | closed by ADR-0023 — да, общий `version_byte` механизм; backend-агент добавляет таблицу `webhooks` в список re-encrypt. |

Следующий свободный `NNN`: **004**. (`Q-WH-*` — domain-specific префикс для webhook'ов, по аналогии с потенциальными доменными префиксами в будущем.)
