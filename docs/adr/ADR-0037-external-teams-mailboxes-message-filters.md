# ADR-0037 — External API: список команд, список ящиков и серверные фильтры сообщений

> **⚠️ Частично superseded — [ADR-0039](./ADR-0039-external-write-api.md) §3 (2026-07-09).** Описанное ниже **взаимоисключение** фильтров `mail_account_id` × `group_id` (`400 validation_error` `field=filter`; «UI выбирает либо ящик, либо команду») **отменено**: фильтры стали **повторяемыми (`list[int]`) и AND-комбинируемыми** (эффективный набор = canonical ∩ группы ∩ ящики; пустое пересечение → **пустая страница**; кода `field=filter` больше нет). Причина — headless-CRM (CRM `ADR-038`) инъектирует scope-`group_id` не-админу в **каждый** запрос ленты и обязан комбинировать его с пользовательским `mail_account_id`. Актуальная семантика — [04-api-contracts.md](../04-api-contracts.md) (раздел `GET /api/external/messages`) и [ADR-0039](./ADR-0039-external-write-api.md) §3. Остальное в этом ADR (endpoint'ы `/teams`, `/mailboxes`; семантика невалидного/чужого id → пустая страница; коды; совместимость) — **в силе**.

| | |
| --- | --- |
| Статус | accepted (взаимоисключение `mail_account_id` × `group_id` **superseded by ADR-0039** §3 — фильтры стали AND-комбинируемыми) |
| Дата | 2026-07-06 |
| Заменяет / отменён | взаимоисключение фильтров — **superseded by [ADR-0039](./ADR-0039-external-write-api.md) §3** (2026-07-09); в остальном **extends ADR-0029** (переиспользует canonical-дедуп ADR-0029 §5 и оба режима пагинации ADR-0036) |

## Context

Внешний API (ADR-0029 read-pull + ADR-0035 reply + ADR-0036 backward/latest) отдаёт **только** письма: `GET /api/external/messages` (`ExternalMessageDTO` с вложенным `mail_account{id,email,display_name}`) и `POST /api/external/messages/{id}/reply`. Появился новый потребитель — **CRM «Почты»** (внутренний прокси поверх того же `X-API-Key`-канала, ADR-0036 §Context). Ему для UX-слоя не хватает трёх вещей, данные для которых **уже есть** в БД, но наружу не отдаются:

1. **Список команд** — для фильтра «по команде» и подписей. Команда = строка таблицы `groups` (`shared/models/group.py`: `id`, `name`). **Команда ≠ тег** (теги — авто-метки, отдельная таблица `tags`, ADR-0017; они уже отдаются в `ExternalMessageDTO.tags` и здесь **не трогаются**).
2. **Список ящиков со статусом** — для дропдауна выбора почты, счётчиков «активные / неактивные» и маппинга ящик→команда. Ящик = `mail_accounts`; связь с командой — `mail_accounts.group_id` (1:1, `shared/models/mail_account.py:43`; `NULL` = персональный); статус — `mail_accounts.is_active` (`:83`; воркер авто-отключает сбойные, ADR-0033/ADR-0026 §3).
3. **Серверная фильтрация писем** по ящику и по команде — сейчас `GET /api/external/messages` отдаёт **все** письма системы без сужения; CRM вынужден фильтровать на своей стороне, перекачивая лишнее.

Требования и ограничения (не нарушать):
- **Read-only модель ADR-0029** — только `GET`, только поля-проекции, никаких паролей/токенов/секретов/IMAP-UID/owner-структур.
- **Тот же auth / rate-limit / CSRF-exempt / redact / super_admin-visibility / canonical-дедуп**, что у существующего read-канала. Никаких новых секретов и feature-флагов (read всегда включён при непустом `EXTERNAL_API_KEY`; write-gate `EXTERNAL_REPLY_ENABLED` ADR-0035 этого ADR **не** касается).
- **ADR-0035 reply / ADR-0036 backward** — не затрагиваются.
- **Обратная совместимость** — существующий B2B-forward-клиент (ADR-0029) не должен получить ни изменения дефолтного поведения, ни новых обязательных параметров: новые фильтры **опциональны**, дефолт (их отсутствие) = прежняя выдача «все письма».

Переиспользуемая инфраструктура (as-is, без нового кода-ядра):
- `GroupsRepo.list_all_groups()` (`backend/app/repositories/groups.py:61`) — плоский список всех групп, `ORDER BY id`. Уже используется `GET /api/my/groups` для super_admin (ADR-0031 §5). Тот же метод.
- `MailAccountsRepo.list_canonical_account_ids()` (`:141`) — `MIN(id)` per `LOWER(email)`; canonical-дедуп дубль-ящиков (ADR-0029 §5).
- `MailAccountsRepo.list_by_ids(account_ids)` (`:75`) — bulk-load ящиков по PK.
- `MailAccountsRepo.list_account_ids_in_group(group_id)` (`:136`) — `mail_accounts.id` одной команды.
- `ExternalMessagesRepo` keyset-методы (`list_since_id` / `list_before_id` / `list_latest`) — уже принимают `mail_account_ids: list[int]`; фильтр реализуется **сужением** этого списка в service-слое, без изменения repo.

## Decision

Добавить к внешнему API **два read-only endpoint'а** (`teams`, `mailboxes`) и **два опциональных query-фильтра** (`mail_account_id`, `group_id`) к существующему `GET /api/external/messages`. Всё — под тем же `X-API-Key`, тем же `LIMIT_EXTERNAL_API`, CSRF-exempt, super_admin-visibility, с тем же canonical-дедупом. Без миграции БД, без новых env/флагов, без нового rate-limit'а.

### 1. `GET /api/external/teams`

| | |
| --- | --- |
| Метод / путь | `GET /api/external/teams` (query-параметров нет). |
| Auth | `EXTERNAL_API_KEY` (`X-API-Key` / `Bearer`), тот же флоу ADR-0029 §4. |
| CSRF | exempt. |
| Rate-limit | `LIMIT_EXTERNAL_API` (тот же read-бюджет, 120/min per IP). Нового лимита нет. |
| Visibility | super_admin — **все** команды системы (эквивалент существующего external-scope: доверенный сервис видит всё). Источник — `GroupsRepo.list_all_groups()` (плоский, `ORDER BY id`). Фильтрации нет. |
| 200 | `{"teams": [{"id": int, "name": str}]}`. Пустая система → `{"teams": []}`. |
| 401 | `not_authenticated` — нет/неверный ключ **или** фича выключена (неперечислимо). |
| 429 | `rate_limited` (+`Retry-After`). |

DTO: `ExternalTeamDTO{id:int, name:str}`; обёртка `ExternalTeamsResponse{teams: list[ExternalTeamDTO]}`. **Только** `id` и `name` — никаких `leader_user_id` / `created_at` / `members_count` / owner-структур (в отличие от admin-`GET /api/admin/groups`, который тяжёлый и раскрывает лидера/счётчики). Проекция минимальна и осознанна.

### 2. `GET /api/external/mailboxes`

| | |
| --- | --- |
| Метод / путь | `GET /api/external/mailboxes` (query-параметров нет). |
| Auth / CSRF / Rate-limit | как §1 (тот же ключ, `LIMIT_EXTERNAL_API`, exempt). |
| Visibility | super_admin + **canonical-дедуп** (ADR-0029 §5): ящики = `MailAccountsRepo.list_by_ids(MailAccountsRepo.list_canonical_account_ids())` — ровно один канонический (`MIN(id)`) ящик на `LOWER(email)`. |
| 200 | `{"mailboxes": [{"id": int, "email": str, "display_name": str\|null, "group_id": int\|null, "is_active": bool}]}`. Нет ящиков → `{"mailboxes": []}`. |
| 401 / 429 | как §1. |

DTO: `ExternalMailboxDTO{id:int, email:str, display_name:str|null, group_id:int|null, is_active:bool}`; обёртка `ExternalMailboxesResponse{mailboxes: list[ExternalMailboxDTO]}`.

**Поля побуквенно и их назначение для CRM:**
- `id` — `mail_accounts.id`; совпадает с `ExternalMessageDTO.mail_account.id` (см. §4 «Консистентность») → CRM джойнит письма с ящиками по этому ключу и с ящик→команда-маппингом.
- `email` / `display_name` — подпись ящика в дропдауне (`display_name` nullable, БД; CRM-хелпер `display_name || email`).
- `group_id` — маппинг ящик→команда (`mail_accounts.group_id`, nullable; `null` = персональный/без команды). **Осознанно раскрывается** (см. §Security) — нужен CRM для группировки ящиков по командам.
- `is_active` — статус (`mail_accounts.is_active`; `false` = авто-отключён воркером, ADR-0033). CRM считает счётчики «активные/неактивные» **на своей стороне** (server-side агрегаты не вводим — минимализм).

**Почему canonical-дедуп для ящиков.** Множество ящиков в `mailboxes` **совпадает** с множеством, чьи письма возвращает `GET /api/external/messages` (тот же `list_canonical_account_ids()`). Если один email подключён двумя командами (два `mail_accounts`-ряда, ADR-0029 §5), CRM получает **один** канонический ящик — консистентно с письмами (каждый `mail_account.id` в письмах гарантированно присутствует в `mailboxes`). Раскрытие обеих копий сломало бы дропдаун (дубликат email с разными id, из которых один никогда не даёт писем).

### 3. Фильтры `GET /api/external/messages` (расширение)

Два **опциональных** query-параметра; работают в **обоих** режимах пагинации (`asc` forward ADR-0029 и `desc` backward/latest ADR-0036) — фильтр только **сужает** набор `mail_account_ids`, курсорная семантика (`since_id`/`before_id`/`next_since_id`/`next_before_id`/`has_more`) **не меняется**.

| Параметр | Тип / границы | Default | Семантика |
| --- | --- | --- | --- |
| `mail_account_id` | `int`, `ge=1` | *(отсутствует)* | Только письма этого ящика. Эффективный набор = `{mail_account_id} ∩ canonical_ids`. |
| `group_id` | `int`, `ge=1` | *(отсутствует)* | Только письма ящиков этой команды. Эффективный набор = `list_account_ids_in_group(group_id) ∩ canonical_ids`. |

**Резолв набора ящиков (service-слой, до repo):**
```
base = MailAccountsRepo.list_canonical_account_ids()            # canonical-дедуп, как ADR-0029 §5
if mail_account_id is not None and group_id is not None:
    raise ValidationError(field="filter")                       # 400 — взаимоисключение (см. ниже)
elif mail_account_id is not None:
    effective = [mail_account_id] if mail_account_id in base else []
elif group_id is not None:
    in_group = MailAccountsRepo.list_account_ids_in_group(group_id)
    effective = [a for a in in_group if a in base]               # пересечение с canonical
else:
    effective = base                                            # прежнее поведение (все письма)
# effective → тот же ExternalMessagesRepo keyset-метод (asc/desc), что и без фильтра
# (методы уже принимают mail_account_ids и корректно возвращают [] на пустом списке).
```

> **⚠️ Superseded частично — [ADR-0039](./ADR-0039-external-write-api.md) §3 (2026-07-09).** Взаимоисключение `mail_account_id` × `group_id` (`400 field=filter`), описанное в этом абзаце, **отменено**: фильтры стали **AND-комбинируемыми** (пересечение). Причина — headless-CRM (`ADR-038`) обязан инъектировать scope-`group_id` не-админу в каждый запрос ленты И одновременно пропускать пользовательский `mail_account_id`; при взаимоисключении это давало либо `400` на свой ящик, либо утечку чужого. Предположение ниже «UI выбирает либо ящик, либо команду» перестало быть верным. Актуальная семантика фильтров — [04-api-contracts §4d](../04-api-contracts.md) + ADR-0039 §3.

**Взаимоисключение `mail_account_id` × `group_id` → `400 validation_error`.** *(историческое решение, отменено ADR-0039 — см. заметку выше).* Оба заданы одновременно ⇒ детерминированный `field="filter"`, message «mail_account_id и group_id взаимоисключающи». Выбран **явный отказ**, а не «комбинация (ящик внутри команды)» и не молчаливый приоритет — по прецеденту ADR-0036 §5 (конфликт `since_id`+`before_id` → `400`, `field="cursor"`): явность > неожиданность. Комбинация добавила бы семантику «проверить, что ящик принадлежит команде» без пользы для CRM (UI выбирает **либо** конкретный ящик, **либо** всю команду, не оба). Валидируется на шаге query-валидации (после auth), как прочие 400.

**Семантика невалидного / несовпадающего id — пустой результат, НЕ 404.** ADR-0029 §3 фиксирует: «404/403 не используются — канал не имеет ресурсной адресации и пользовательских scope'ов». Фильтр этого не меняет — он **сужает** выборку, а не адресует ресурс. Поэтому:
- `mail_account_id`, которого нет / он чужой / он **non-canonical дубль** (существует, но не в `canonical_ids`) ⇒ `effective = []` ⇒ **пустая страница** (`messages:[]`, курсор не двигается), как `since_id` «в будущем» (ADR-0029 Edge cases). **Не** 404 — это сохраняет инвариант «нет 404 во внешнем read-канале» и не раскрывает существование/несуществование конкретного ящика.
- `group_id`, которого нет / пустая команда / все её ящики non-canonical ⇒ `effective = []` ⇒ пустая страница.
- Невалидная граница (`<1` / нечисловой) ⇒ `400 validation_error` (`field=mail_account_id` / `group_id`) — это валидация ввода, а не адресация ресурса (отличается от «валидный, но несовпадающий id» выше).

Курсор при пустой странице: `asc` → `next_since_id = <входной since_id>`, `has_more=false`; `desc` → `next_before_id = null`, `has_more=false` (ADR-0029/0036, без изменений).

### 4. Консистентность идентификаторов (для CRM-джойна)

`ExternalMailboxDTO.id` = `ExternalMessageDTO.mail_account.id` = `mail_accounts.id`. Оба endpoint'а (`mailboxes` и `messages`) применяют **тот же** `list_canonical_account_ids()` → каждый `mail_account.id`, встречающийся в письмах, присутствует в `mailboxes`; ящик→команда-маппинг (`group_id`) и статус (`is_active`) CRM берёт из `mailboxes` и джойнит к письмам по `mail_account.id`. `teams[].id` = `groups.id` = `mailboxes[].group_id` → трёхуровневый джойн письмо → ящик → команда полностью замыкается на стороне CRM.

**`ExternalMessageDTO` / `ExternalMailAccountDTO` / `ExternalTagDTO` — БЕЗ изменений.** `group_id`/`is_active` **не** добавляются во вложенный `mail_account` письма (стабильность контракта ADR-0029 §6); маппинг доступен через отдельный `mailboxes`-endpoint. Это сохраняет узость message-DTO и переносит «расширенную» проекцию ящика в специализированный endpoint.

### 5. Схемы (`backend/app/external/schemas.py`)

Добавляются (аддитивно, отдельно от message-DTO):
- `ExternalTeamDTO{id:int, name:str}`
- `ExternalTeamsResponse{teams: list[ExternalTeamDTO]}`
- `ExternalMailboxDTO{id:int, email:str, display_name:str|None, group_id:int|None, is_active:bool}`
- `ExternalMailboxesResponse{mailboxes: list[ExternalMailboxDTO]}`

`ExternalMessageDTO` / `ExternalMailAccountDTO` / `ExternalTagDTO` / `ExternalMessagesResponse` / `ExternalMessagesResponseDesc` — **не меняются**.

### 6. Версионирование

Изменение **аддитивно и non-breaking**: два новых endpoint'а + два новых **опциональных** query-параметра + новые response-модели, появляющиеся только на новых путях. Дефолт `GET /api/external/messages` (без фильтров) — байт-в-байт ADR-0029/0036. Путь `/api/external/` (неявная v1) **не** бампается; нового версионного пути/ADR не требуется (правило ADR-0029 §6).

## Consequences

### Positive
- **CRM получает дропдаун почт, счётчики и фильтрацию из первоисточника** — без перекачивания всех писем ради клиентской фильтрации; сервер сужает выборку keyset'ом по подмножеству `mail_account_ids`.
- **Ноль новой инфраструктуры** — без миграций/индексов (reuse PK-keyset и существующего `INDEX (mail_account_id, internal_date DESC)`), без новых env/флагов, без нового rate-limit'а. Reuse `GroupsRepo.list_all_groups` / `MailAccountsRepo.{list_canonical_account_ids,list_by_ids,list_account_ids_in_group}` / `ExternalMessagesRepo` (методы уже принимают `mail_account_ids`).
- **Полная BC** — фильтры опциональны, message-DTO и оба курсора неизменны; B2B-forward-клиент ADR-0029 не затронут.
- **Консистентность** — `mailboxes`/`teams`/`messages` дают согласованные id для трёхуровневого джойна; canonical-дедуп единый во всех.
- **Минимальная поверхность** — только read (`GET`), только проекции; write ADR-0035 не расширяется.

### Negative / risks
- **Расширение read-поверхности** — ADR-0029 §Decision держал канал «минимальным» (один GET). Теперь наружу видны список команд (`id`,`name`) и список ящиков с `group_id`/`is_active`. Это **осознанное** расширение для внутреннего CRM-потребителя (не публичного B2B-партнёра): компрометация ключа теперь раскрывает ещё и структуру команд/ящиков (но не письма сверх уже раскрытых, не credentials). Митигация — та же, что ADR-0029 §Security: ключ в env/redact, ротация, TLS. Per-client-ключи/scope'ы — отдельный ADR при появлении нескольких партнёров (в MVP один доверенный потребитель).
- **`group_id`/`is_active` наружу** — раскрываются намеренно (нужны CRM для маппинга/счётчиков); НО `mail_accounts.user_id`/owner-структуры/leader/`members_count`/credentials **не** раскрываются. Проекция строго ограничена пятью полями ящика и двумя полями команды.
- **Скользящее окно / id-gaps** — фильтры не меняют курсорную семантику; риски ADR-0029/0036 (retention id-gaps, скользящее окно при активном ingest) в силе и одинаково безвредны на суженном наборе.
- **Нагрузка** — `mail_account_id IN (effective)` с ≤ сотен id + keyset по PK (та же оценка, что ADR-0029 §5); `teams`/`mailboxes` — плоские списки ≤ 5 групп / ≤ 500 ящиков (`03-data-model.md`). Тот же `LIMIT_EXTERNAL_API`.

### Migration plan
1. **Без миграции БД** — новых таблиц/колонок/индексов нет (reuse существующих).
2. **`backend/app/external/schemas.py`**: `ExternalTeamDTO` / `ExternalTeamsResponse` / `ExternalMailboxDTO` / `ExternalMailboxesResponse` (§5). Message-DTO не трогать.
3. **`backend/app/external/router.py`**: `GET /api/external/teams`, `GET /api/external/mailboxes`; к `GET /api/external/messages` — query-параметры `mail_account_id: int | None = None` (`ge=1`), `group_id: int | None = None` (`ge=1`).
4. **`backend/app/external/service.py`**: тот же auth-флоу (ADR-0029 §4, rate-limit до ключа); teams → `GroupsRepo.list_all_groups()`; mailboxes → `list_by_ids(list_canonical_account_ids())`; messages → резолв `effective` набора (§3) с взаимоисключением (`400 field=filter`) до вызова `ExternalMessagesRepo` (asc/desc — как ADR-0036).
5. **`ExternalMessagesRepo`** — **изменений нет** (методы уже принимают `mail_account_ids`; service передаёт суженный список).
6. **`backend/app/main.py`**: новые пути под тот же CSRF-exempt allowlist `/api/external/*`.
7. **Rate-limit / env / DevOps** — изменений нет (тот же `EXTERNAL_API_KEY` / `EXTERNAL_API_RATE_LIMIT_PER_MINUTE` / `LIMIT_EXTERNAL_API`).

## Alternatives considered

1. **Отдавать `group_id`/`is_active` внутри `ExternalMessageDTO.mail_account`** — отвергнуто. Раздувает и дестабилизирует message-DTO (ADR-0029 §6 требует узость/стабильность), дублирует ящик-поля в каждом письме. Отдельный `mailboxes`-endpoint отдаёт маппинг один раз; CRM джойнит по `mail_account.id`.
2. **Server-side счётчики активные/неактивные (агрегатный endpoint)** — отвергнуто на MVP. `mailboxes` уже даёт `is_active` на ящик; CRM считает клиентски. Агрегат — лишний endpoint без нужды.
3. **Комбинация «`mail_account_id` внутри `group_id`»** вместо взаимоисключения — отвергнуто. Добавляет проверку принадлежности ящика команде без пользы (UI выбирает либо ящик, либо команду). Явный `400` проще и по прецеденту ADR-0036 §5.
4. **Молчаливый приоритет при обоих фильтрах** (напр. `mail_account_id` важнее) — отвергнуто в пользу `400 validation_error` (ADR-0036 §Alternatives 4: явная ошибка предотвращает тихую подмену намерения).
5. **404 на несуществующий/чужой `mail_account_id`/`group_id`** — отвергнуто. Нарушило бы инвариант ADR-0029 §3 «нет 404/403 в внешнем read-канале» и раскрыло бы существование ящиков/команд по коду ответа. Пустая страница консистентна с «`since_id` в будущем» (ADR-0029 Edge cases).
6. **Переиспользовать admin-`GET /api/admin/groups`** для teams — отвергнуто: он session-based (super_admin cookie), тяжёлый (`members_count`, leader), раскрывает лишнее. Внешний канал — key-based, минимальная проекция; reuse только repo-метода `list_all_groups()`.
7. **Фильтр по тегу (`tag_id`)** — вне scope этого ADR. Команда ≠ тег; CRM запросил фильтры ящик/команда. Теги уже в `ExternalMessageDTO.tags` (клиентская фильтрация возможна). Server-side tag-фильтр — отдельный ADR при явном запросе.

## Security

- **Тот же ключ и флоу** — `EXTERNAL_API_KEY` (`compare_digest`, `X-API-Key`/`Bearer`), rate-limit `LIMIT_EXTERNAL_API` **до** работы с ключом, CSRF-exempt, super_admin-visibility. Новых секретов нет; redact-list ADR-0029 (`EXTERNAL_API_KEY`/`X-API-Key`/`Authorization`) достаточен — новые endpoint'ы логируют только `client_ip`/`returned_count`/`filter`-параметры, никогда ключ.
- **Проекция строго ограничена.** teams → `id`,`name`. mailboxes → `id`,`email`,`display_name`,`group_id`,`is_active`. **Никаких** `encrypted_password`/`oauth_*`/`smtp_*`/`imap_*`/`user_id`/owner-структур/`leader_user_id`/`members_count`. `group_id`/`is_active` раскрыты **осознанно** для CRM-маппинга/счётчиков.
- **Нет новой write-поверхности** — только `GET`; ADR-0035 write-gate не затронут.
- **Расширение доверия** — потребитель (CRM) остаётся доверенной стороной, как B2B-партнёр ADR-0029; раскрытие структуры команд/ящиков — осознанный trade-off, зафиксированный здесь (не «минимально» как ADR-0029 §Decision, а «минимально-необходимо для внутреннего CRM»).

## Edge cases

| Случай | Поведение |
| --- | --- |
| Нет команд в системе | `GET /teams` → `{teams:[]}` (200). |
| Нет ящиков | `GET /mailboxes` → `{mailboxes:[]}` (200). |
| Дубль-ящик (email в двух командах) | `mailboxes` отдаёт **один** канонический (`MIN(id)`) ящик; его `group_id` — канонического ряда. Консистентно с `messages`. |
| `mail_account_id` не существует / чужой / non-canonical | Пустая страница (`messages:[]`, курсор не двигается). **Не** 404. |
| `group_id` не существует / пустая команда / все ящики non-canonical | Пустая страница. **Не** 404. |
| `mail_account_id` **и** `group_id` заданы вместе | `400 validation_error`, `field="filter"`. |
| `mail_account_id` / `group_id` `< 1` или нечисловой | `400 validation_error`, `field=mail_account_id` / `group_id`. |
| Фильтр + `order=desc` (ADR-0036) | Работает: суженный набор `mail_account_ids` в `desc`-latest/older; `next_before_id`/`has_more` без изменений. |
| Фильтр + `order=asc` (default) | Работает: суженный набор в forward-keyset; `next_since_id`/`has_more` без изменений. |
| Персональный ящик (`group_id=null`) | Виден в `mailboxes` с `group_id:null`; попадает в `group_id`-фильтр **только** если запрошенная команда его содержит (не содержит — `null` не матчит никакой `group_id`). |

## Open questions

Нет. Контракт закрыт полностью (endpoint'ы, поля, фильтры, взаимоисключение, семантика невалидного id, коды, совместимость определены выше).
