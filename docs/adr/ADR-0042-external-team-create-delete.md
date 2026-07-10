# ADR-0042 — External team (group) create + guarded delete для ленивого провижининга почтовых групп из headless-CRM

Статус: **superseded by [ADR-0043](./ADR-0043-strip-to-connector-push-to-crm.md)** (2026-07-10) — external `POST/DELETE /api/external/teams` снимаются (групп нет; ящик привязан к команде в CRM `mail_accounts.team_id`). Ранее: `accepted` · Дата: 2026-07-10

Extends [ADR-0029](./ADR-0029-external-pull-api.md) (pull) / [ADR-0037](./ADR-0037-external-teams-mailboxes-message-filters.md) (`GET /api/external/teams`) / [ADR-0039](./ADR-0039-external-write-api.md) (write-раздел + `crm-service` + write-gate). Парный ADR в CRM — `ADR-043` (ленивый провижининг «команда = почтовая группа»).

## Context

CRM — единственный UI агрегатора (headless, `ADR-0039`/[ADR-0041](./ADR-0041-disable-jinja-ui.md)). Модель CRM: **команда = почтовая группа**, связь `teams.mail_group_id ↔ groups.id` 1:1 (CRM `ADR-038`). Владелец продукта решил: пользователь **не** сопоставляет команду и группу вручную; группа в агрегаторе создаётся **лениво — по первому добавлению почты** в команду, у которой ещё нет группы. Для этого CRM должен уметь **создать группу** через external API (имя = имя CRM-команды), получить её `id`, записать в `teams.mail_group_id` и создать ящик уже с этим `group_id`.

External API сегодня read-only на группы: `GET /api/external/teams` (ADR-0037, `{teams:[{id,name}]}`, `groups` под именем «teams»). Write на группы нет. Внутренний CRUD групп (`POST/PATCH/DELETE /api/admin/groups`, `backend/app/groups/*`) человеко-обращён (super_admin VisibilityScope) и в headless-режиме (ADR-0041) его HTML-UI демонтирован — то есть **из-под безличного `EXTERNAL_API_KEY` создать/удалить группу нечем**.

### Аудит модели `groups` (факты из кода)

- **`groups.name` НЕ уникально.** `shared/models/group.py`: единственные табличные ограничения — `UniqueConstraint("leader_user_id", name="uq_groups_leader_user_id")` и `CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="ck_groups_name_length")`. **UNIQUE по `name` отсутствует.** Имена групп штатно совпадают: авто-создание лидера даёт `"Команда {display_name|username}"` (`groups/service.py::_auto_group_name`), и два лидера с одинаковым лейблом → две группы с одним именем.
- **Leaderless-группа поддержана.** `GroupsRepo.insert(name, leader_user_id=None)` (`repositories/groups.py:127`) вставляет группу без лидера (FE-FIX round-2 #3 — orphan group). `groups.leader_user_id` nullable (`group.py:36`).
- **`mail_accounts.group_id` — FK `ON DELETE SET NULL`** (`shared/models/mail_account.py:45`). Удаление группы **не** каскадит ящики — оно **обнуляет** их `group_id` (ящики теряют командную видимость/доставку, но не удаляются).
- **`GroupsService.delete`** (`groups/service.py:503`) отказывает при наличии **участников** (`user_groups`), но **не** проверяет ящики.

## Decision

### §1. Feature-gate, rate-limit, auth-flow — как весь write-раздел (ADR-0039 §1)

Оба новых эндпоинта — под тем же контуром, что mailboxes/tags-write:
- `EXTERNAL_API_KEY` (пусто ⇒ весь external выключен) + write-гейт **`EXTERNAL_WRITE_ENABLED`** (default `false`; `false` ⇒ `403 forbidden` даже с валидным ключом);
- бюджет **`LIMIT_EXTERNAL_WRITE`** (`EXTERNAL_WRITE_RATE_LIMIT_PER_MINUTE`, default 60/min per IP);
- порядок строго ADR-0029 §4 / ADR-0035 §3: `consume(LIMIT_EXTERNAL_WRITE, ip)` → `X-API-Key`/`Bearer` (constant-time) → `external_api_enabled` → **write-гейт `EXTERNAL_WRITE_ENABLED`** → валидация тела → delegate. Реализуется существующим хелпером `_authorize_write(request, ip, settings)` (`backend/app/external/router.py`). CSRF exempt (`/api/external/`).

Наименование `/api/external/teams` (не `/groups`): read-раздел ADR-0037 уже назвал `groups` «командами» для CRM (`GET /api/external/teams`, «Команда = `groups`»). Create/delete **расширяют тот же `/teams`-семейство**. Имя поля `group_id` в payload'ах ящиков/сообщений **не меняется** (оно ссылается на тот же `groups.id`): `ExternalTeamDTO.id` — ровно то значение, которое CRM затем передаёт как `group_id` при создании ящика.

### §2. `POST /api/external/teams` — создание leaderless-группы

- **Тело** `ExternalTeamCreateRequest { name: str }` — `Field(min_length=1, max_length=100)` (совпадает с `ck_groups_name_length`). Нарушение длины → `400 validation_error` (класс `ValidationError`, `code="validation_error"`; см. «Коды»).
- **Поведение:** вставка leaderless-группы `GroupsRepo.insert(name=name, leader_user_id=None)` в транзакции `async with db.begin()` (как `external_mailbox_create`). Лидера **нет**, участников **нет**. Атрибуция аудита — техпользователь **`crm-service`** (`ADR-0039` §Q-0039-1): `AuditWriter.log(actor_user_id=crm_service.id, action="group_create", details={group_id, group_name, auto_created: true})`.
- **Ответ** `201 ExternalTeamDTO { id: int, name: str }` — зеркалит элемент `GET /api/external/teams` (`{id, name}`). `leader_user_id` всегда `null` и **не** раскрывается (CRM он не нужен; секрет-инвариант ADR-0029 не расширяется).

**Идемпотентность — НЕ идемпотентно by design, БЕЗ `409` на дубль имени.** Поскольку `groups.name` не уникально (аудит выше), повтор создаёт **новую** группу; никакого DB-конфликта и, соответственно, **никакого `409`** на совпадение имени. Идемпотентность по имени (вернуть существующую / `UNIQUE(name)`+`409`) **отклонена**:
1. **Имена штатно коллизируют** (auto-`"Команда X"` + ручные) — `UNIQUE(name)` сломал бы легитимные одноимённые группы;
2. Связь CRM-команда↔группа — **по `id`, а не по имени** (CRM `ADR-038` §2; независимые пространства имён). Возврат «существующей по имени» **слил бы** несвязанные команды в одну группу — прямое нарушение модели.

Ретрай-безопасность обеспечивает **CRM-сторона** (CRM `ADR-043` §3/§4): (а) write-ретраи только на `Connect*` (ADR-0035 §3 / CRM ADR-038 §1 — read-timeout/`5xx` на write НЕ ретраятся → нет двойного создания из ретрай-слоя); (б) row-lock на строке команды + перечитывание `mail_group_id` (гонка); (в) порядок «записать `mail_group_id` ДО создания ящика». Остаточный редкий сценарий (группа создана, но CRM получил read-timeout и не записал привязку) даёт **осиротевшую пустую группу** — она безвредна (0 ящиков ⇒ 0 видимости/доставки, не связана ни с одной командой) и **реклеймится через §3 DELETE**.

- **Коды:** `201` · `400 validation_error` (`name` вне 1..100 / битое тело) · `401 not_authenticated` (нет/неверный ключ или `EXTERNAL_API_KEY` пуст) · `403 forbidden` (`EXTERNAL_WRITE_ENABLED=false`) · `429 rate_limited` (`LIMIT_EXTERNAL_WRITE`). **Нет `404`, нет `409`.**

### §3. `DELETE /api/external/teams/{id}` — guarded-реклейм пустой группы

Единственный in-band способ удалить группу в headless-режиме (внутренний admin-UI демонтирован ADR-0041) — нужен для: (а) реклейма осиротевших пустых групп (остаток §2); (б) удаления группы команды при удалении CRM-команды.

- **Guard (нормативно), в одной локальной транзакции с `SELECT ... FOR UPDATE` (анти-TOCTOU):** сначала `SELECT ... FROM groups WHERE id = :id FOR UPDATE` (блокирует строку `groups`), затем EXISTS-проверки и сам `DELETE` — всё внутри одной короткой БД-транзакции **без сетевых вызовов**. Row-lock закрывает окно TOCTOU (ящик/участник, добавленный конкурентно между проверкой пустоты и `DELETE`, не проскользнёт). Это **легитимно и обязательно** — в отличие от CRM-стороны, здесь лок **локальный, короткий, без внешнего I/O под ним**. Проверки:
  - неизвестный `id` (строка не найдена) → `404 not_found` (класс `NotFoundError`, `code="not_found"`);
  - группа имеет **ящики** (`EXISTS mail_accounts WHERE group_id = :id`) **или** участников (`EXISTS user_groups WHERE group_id = :id`) **или** лидера (`groups.leader_user_id IS NOT NULL`) → `409 conflict` (класс `ConflictError`, `code="conflict"`; message описывает непустоту). Отказ обязателен, т.к. FK `mail_accounts.group_id` — `ON DELETE SET NULL`: удаление непустой группы **молча обнулило бы** `group_id` её ящиков (потеря командной видимости/доставки), а не защитило бы данные;
  - пусто (0 ящиков, 0 участников, без лидера) → `GroupsRepo.delete(id)` → `204`. Аудит `group_delete` (actor=`crm-service`).
- **Реализация:** новый метод сервиса (расширяет проверку `GroupsService.delete` дополнительным count ящиков + `FOR UPDATE` на строке `groups`; либо отдельный external-метод) — internal-`delete` проверяет только участников, external-вариант **добавляет** count `mail_accounts` и row-lock.
- **Коды:** `204` · `401` · `403` (write-гейт) · `404 not_found` · `409 conflict` (непустая) · `429`.

### §4. `PATCH /api/external/teams/{id}` (переименование) — НЕ добавляется сейчас

Имя группы в headless-режиме **косметическое**: CRM показывает имя **CRM-команды**, а не группы; собственный UI агрегатора демонтирован (ADR-0041). Синхронизация имени группы с переименованием CRM-команды — nice-to-have, **отложено** (см. Consequences). `GET /api/external/teams` (список, ADR-0037) — без изменений.

## Consequences

- CRM получает ленивый провижининг групп: создать группу (имя = имя команды) → привязать → создать ящик, всё под `EXTERNAL_API_KEY`+write-гейтом. Ручное сопоставление команда↔группа в агрегаторе больше не обязательно (CRM `ADR-043`, частично закрывает CRM `TD-036`).
- `POST /teams` **не идемпотентно** и **не** конфликтит по имени — осознанно (имена коллизируют, связь по id). Двойное **привязывание** предотвращается CRM-стороной (**CAS** на `teams.mail_group_id` + connect-only-ретрай + порядок записи привязки до ящика; CRM `ADR-043` §4). `DELETE`-guard берёт короткий **локальный** `SELECT ... FOR UPDATE` на строке `groups` (§3, анти-TOCTOU) — это принято (нет сетевого I/O под локом), в отличие от отвергнутого на CRM-стороне row-lock через внешний вызов.
- Осиротевшая **пустая** группа (редкий remnant) безвредна и реклеймится `DELETE /api/external/teams/{id}`.
- `DELETE` guarded: непустую группу (ящики/участники/лидер) не удалить (`409`) — защита от молчаливого обнуления `mail_accounts.group_id` (FK `SET NULL`).
- Изменений схемы БД нет (leaderless-insert и все проверки — на существующих таблицах/ограничениях).
- **Отложено (tech-debt — [TD-048](../100-known-tech-debt.md)):** `PATCH /teams` (rename) для синхронизации имени группы с переименованием CRM-команды; фоновая уборка осиротевших пустых групп. Обе — низкоприоритетны (имя косметическое; remnant редок и безвреден). Парный CRM `TD-037` (группа без команды).

## Alternatives considered

- **`UNIQUE(groups.name)` + `409` на дубль (идемпотентность по имени).** Отклонён: имена штатно коллизируют (auto-`"Команда X"`), UNIQUE сломал бы легитимные одноимённые группы; связь команда↔группа — по id, не по имени (возврат существующей слил бы несвязанные команды).
- **Идемпотентность по клиентскому ключу (`external_ref` в `groups`).** Отклонён: требует миграции БД (`ADR-0039` намеренно без миграций); выигрыш мал — orphan безвреден и реклеймится DELETE.
- **Назначать создаваемой группе лидера (`crm-service` или первый член).** Отклонён: `crm-service` — super_admin, а инвариант `super_admin ⇒ group_id IS NULL` (`users_role_group_invariant`) и `uq_groups_leader_user_id` делают его лидерство недопустимым/бессмысленным; leaderless-группа детерминирована и штатно поддержана (FE-FIX #3). Лидер headless-группе не нужен (доставка резолвится по `group_id`/`user_groups`, `ADR-0039` §Q-0039-1).
- **Каскадное удаление группы с ящиками.** Отклонён: FK `mail_accounts.group_id` — `SET NULL`; каскад ящиков не предусмотрен, а тихое обнуление привязки — потеря видимости/доставки. Guarded-`409` безопаснее — CRM сперва переносит/удаляет ящики.
- **Оставить create-only (без DELETE).** Отклонён: headless-режим убрал внутренний admin-UI удаления групп → без external-DELETE осиротевшие/ненужные группы нечем убрать in-band.
