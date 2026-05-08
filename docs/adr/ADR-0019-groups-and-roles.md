# ADR-0019: Роли пользователей и группы — иерархия доступа

- **Статус:** accepted
- **Дата:** 2026-05-08

## Context

До этой итерации у пользователей был **бинарный** признак прав — `users.is_admin: BOOLEAN`. Только две роли: `super_admin` (один, из env) и обычный пользователь. Каждый обычный пользователь видел **только свои** mail-аккаунты и письма (через `mail_accounts.user_id = current_user.id`); super-admin (как user в системе) видел только свои.

Запрос продукта (TZ Sprint feature "groups + roles"):

1. Ввести **трёхуровневую** иерархию ролей:
   - **`super_admin`** (один глобальный, креды из `.env` через `seed_super_admin`) — управляет всеми пользователями, группами, всеми mail-аккаунтами и всеми письмами.
   - **`group_leader`** — лидер одной группы. Видит mail-аккаунты и письма всех участников **своей группы** (включая себя). Управляет mail-аккаунтами в рамках своей группы (создаёт на себя или на участников группы). Не управляет другими лидерами / другими группами.
   - **`group_member`** — рядовой участник группы. Видит mail-аккаунты и письма всех участников **своей группы** (включая себя). Создаёт mail-аккаунты только на себя. Не управляет пользователями.
2. **Email-никнейм** (`mail_accounts.display_name`) — описано отдельно в **ADR-0020** (читать вместе с этим).
3. **Полная RU-локализация UI** — описана отдельно в **ADR-0021** (читать вместе с этим).

Ключевые ограничения, под которые мы выбираем дизайн:
- Объёмы малые. ≤ 5 пользователей × ≤ 100 mail-аккаунтов per user (см. `03-data-model.md` секция «Объёмные оценки»). Группы — единицы (1–3) на старте.
- Стек уже зафиксирован — Postgres 16 + FastAPI + SQLAlchemy + Jinja2 (см. ADR-0001).
- Tags остаются **per-user** (см. ADR-0017). Пере-делать на per-group теги — **отложено** (TD-015).
- Auth-flow остаётся ровно тем же (two-step login по ADR-0016, server-side sessions по ADR-0004); мы добавляем только поле `role` (вместо `is_admin`) в session-payload.
- CSRF, rate-limit, MinIO, ретенция, sync-cycle — без изменений.

## Decision

### 1. Замена `users.is_admin` на `users.role`

Колонка `users.is_admin: BOOLEAN` **удаляется**. Вместо неё — `users.role TEXT NOT NULL` с CHECK-constraint:

```sql
ALTER TABLE users
    ADD COLUMN role TEXT NOT NULL DEFAULT 'group_member'
        CHECK (role IN ('super_admin', 'group_leader', 'group_member'));
ALTER TABLE users DROP COLUMN is_admin;
```

**Data-миграция (Alembic, 003_groups_and_roles.py)** — выполняется в одной транзакции с DDL:

```sql
-- 1. Добавить role nullable (пере-затрётся ниже)
ALTER TABLE users ADD COLUMN role TEXT;

-- 2. Перенести данные
UPDATE users SET role = 'super_admin' WHERE is_admin = true;
UPDATE users SET role = 'group_member' WHERE is_admin = false;

-- 3. Завершить constraint'ы и удалить старую колонку
ALTER TABLE users ALTER COLUMN role SET NOT NULL;
ALTER TABLE users ALTER COLUMN role SET DEFAULT 'group_member';
ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('super_admin', 'group_leader', 'group_member'));
ALTER TABLE users DROP COLUMN is_admin;
```

Партиальный индекс `INDEX (is_admin) WHERE is_admin = true` пересоздаётся как:
```sql
CREATE INDEX users_role_super_admin_idx ON users(role) WHERE role = 'super_admin';
```

`seed_super_admin` (см. модуль 7 в `05-modules.md`) после миграции делает upsert с `role = 'super_admin'` (вместо `is_admin = true`). Никакая другая логика seed'а не меняется.

**Отказ от ENUM-type Postgres.** Используем `TEXT` + `CHECK` (как уже сделано в `tag_rules.type`). Преимущество — миграция при добавлении новой роли проще (никакого `ALTER TYPE`).

### 2. Колонка `users.display_name`

```sql
ALTER TABLE users ADD COLUMN display_name TEXT NULL CHECK (char_length(display_name) BETWEEN 1 AND 100);
```

Опциональное поле. При выводе в UI используется fallback: если `display_name IS NULL` — показывается `username`. См. helper `effective_user_name(user)` в `06-rbac.md` (ниже).

Используется как **источник** для авто-генерации `groups.name` при создании лидера (см. §4 ниже).

### 3. Новая таблица `groups`

```sql
CREATE TABLE groups (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
    leader_user_id  BIGINT NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX groups_leader_idx ON groups(leader_user_id);
```

Особенности:
- **`leader_user_id UNIQUE`** — один user не может быть лидером более одной группы. Простое 1:1 mapping `group → leader`.
- **`ON DELETE RESTRICT` на FK к `users`** — лидер не удаляется, пока существует группа. Если super-admin хочет удалить пользователя-лидера, он обязан **сначала** удалить группу (см. §5 ниже про каскад group_member → group_id=NULL).
  - Альтернатива (`CASCADE` от leader → group → членам ставит NULL) рассматривалась и **отвергнута**: невидимый side-effect (удаление user'а тихо распускает группу), super-admin должен делать это явно.
- **Нет soft-delete.** Объёмы единицы; `groups` удаляется hard.

### 4. Колонка `users.group_id`

```sql
ALTER TABLE users
    ADD COLUMN group_id BIGINT NULL REFERENCES groups(id) ON DELETE SET NULL;

CREATE INDEX users_group_id_idx ON users(group_id) WHERE group_id IS NOT NULL;
```

Семантика по ролям:
- **`super_admin`** — `group_id IS NULL` всегда. Super-admin не входит ни в какую группу.
- **`group_leader`** — `group_id = id` той `groups`-записи, в которой `groups.leader_user_id = users.id`. То есть лидер всегда входит «в свою же» группу.
- **`group_member`** — `group_id` указывает на группу, к которой он привязан (как рядовой участник).

`ON DELETE SET NULL` на FK к `groups`: при удалении группы super-admin'ом — все её участники получают `group_id = NULL`. Их `role` при этом **меняется на `group_member`** уже на уровне приложения (а не БД-триггером — см. §6 про инварианты). Лидер удаляемой группы теряет статус `group_leader` и становится `group_member` с `group_id = NULL`. Super-admin при выполнении DELETE группы обязан явно подтвердить этот эффект (UI-confirm).

### 5. Auto-create группы при создании `group_leader`

При создании пользователя с `role = 'group_leader'` super-admin'ом возможны два сценария:

1. **Только указана `role = 'group_leader'`, `group_id` пуст**: backend в одной транзакции:
   - Создаёт `users` (без `group_id` пока).
   - Создаёт `groups (name, leader_user_id)` где:
     - `leader_user_id` — id только что созданного user'а.
     - `name` — авто-имя по шаблону **«Группа {display_name | username}»**, например `"Группа Иван Иванов"` или `"Группа bob"` если `display_name` не задан.
   - Обновляет `users.group_id` на id только что созданной группы.
   - Audit: `group_create` (`actor=super_admin`, `details={group_id, leader_user_id, auto_created=true}`).
2. **`role = 'group_leader'`, `group_id` указан** (super-admin вручную создал группу заранее): backend проверяет, что `groups.id` существует и `groups.leader_user_id IS NULL` — это редкий случай, поэтому **отвергается на старт** (рекомендуем всегда auto-create). Если такой запрос придёт — `400 validation_error: group_id_must_be_null_for_new_leader`. Если очень понадобится — отдельный endpoint `PATCH /api/admin/groups/{id}` для назначения лидера в существующей пустой группе (не делаем сейчас, см. §11 «Out of scope»).

Создание `group_member`: super-admin или group_leader выбирает существующую `group_id`; backend проверяет, что группа существует. Если super-admin создаёт первого участника группы и хочет выбрать «без группы» — это **запрещено** для `group_member` (см. §6 invariants). Чтобы создать пользователя «в воздухе» (зарезервированное имя без группы), super-admin создаёт его и сразу назначает group_id; альтернатива «потом перевести» обсуждалась и отвергнута для упрощения инвариантов.

### 6. Инварианты (поддерживаются приложением и CHECK'ами)

| Роль | `users.role` | `users.group_id` | `groups.leader_user_id` |
| --- | --- | --- | --- |
| super_admin | `'super_admin'` | `NULL` | (никогда не лидер) |
| group_leader | `'group_leader'` | `= id` своей группы | `= users.id` |
| group_member | `'group_member'` | NOT NULL | (никогда не лидер) |

CHECK на уровне таблицы `users`:

```sql
ALTER TABLE users ADD CONSTRAINT users_role_group_invariant CHECK (
    (role = 'super_admin' AND group_id IS NULL) OR
    (role = 'group_leader' AND group_id IS NOT NULL) OR
    (role = 'group_member' AND group_id IS NOT NULL)
);
```

Обеспечение «`group_leader.group_id` ссылается на ту группу, где `groups.leader_user_id = users.id`» — на уровне БД CHECK не выразить (требуется join). Реализуется **трёх-местным** SQL-trigger'ом в той же миграции:

```sql
CREATE OR REPLACE FUNCTION check_group_leader_consistency()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.role = 'group_leader' THEN
        IF NOT EXISTS (
            SELECT 1 FROM groups g
            WHERE g.id = NEW.group_id AND g.leader_user_id = NEW.id
        ) THEN
            RAISE EXCEPTION 'group_leader_consistency_violation: user % role=group_leader but groups.leader_user_id != users.id for group_id=%', NEW.id, NEW.group_id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER users_group_leader_consistency_check
AFTER INSERT OR UPDATE OF role, group_id ON users
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION check_group_leader_consistency();
```

`DEFERRABLE INITIALLY DEFERRED` — потому что при auto-create lead'а сначала вставляется user (без group_id), потом groups (FK на user.id), потом UPDATE users.group_id; на момент INSERT user'а инвариант ещё не выполняется, проверка откладывается до COMMIT транзакции.

Аналогично, если `users` пишется до `groups` — `users.group_id` указывает на ещё не созданную строку groups. FK constraint `users.group_id → groups(id)` тоже ставится `DEFERRABLE INITIALLY DEFERRED` для этого же сценария.

**Backend дополнительно валидирует** инварианты в `AdminService.create_user` / `update_user` / `delete_group` ДО SQL — чтобы давать понятные `validation_error` с конкретным `field`, а не raw EXCEPTION от Postgres. БД-триггер — defense-in-depth.

### 7. Visibility — основа модели доступа

Главная декомпозиция — `VisibilityScope`, dataclass, инкапсулирующий: какие mail-аккаунты и сообщения видит **текущий пользователь**.

```python
# backend/app/deps.py (расположение — на усмотрение реализации; источник — этот ADR)

@dataclass(frozen=True)
class VisibilityScope:
    user_id: int                                # current user
    role: Literal['super_admin', 'group_leader', 'group_member']
    group_id: int | None                        # NULL для super_admin
```

`VisibilityScope` создаётся в FastAPI dependency `get_current_user` (читает из session) и пробрасывается во все `Service`-методы, которые что-то читают/листают.

#### 7.1 Mail-accounts visibility

Endpoints `GET /api/mail-accounts`, `GET /api/mail-accounts/{id}`, и любой админ-список — все используют `MailAccountService.list_for_scope(scope)`. SQL-фильтр строится по правилам:

| Role | WHERE-фильтр |
| --- | --- |
| `super_admin` | `WHERE TRUE` (без фильтра, видны все) |
| `group_leader` | `WHERE mail_accounts.user_id IN (SELECT id FROM users WHERE group_id = :scope.group_id)` |
| `group_member` | `WHERE mail_accounts.user_id IN (SELECT id FROM users WHERE group_id = :scope.group_id)` (та же логика, что у leader) |

То есть `group_leader` и `group_member` **видят одно и то же** — все mail-аккаунты всех участников своей группы (включая лидера).

#### 7.2 Messages visibility

`MessageService.list_for_user(scope, ...)`, `get(scope, message_id)` — SQL-фильтр через `JOIN messages m → mail_accounts ma → users u`:

| Role | WHERE-фильтр |
| --- | --- |
| `super_admin` | `WHERE TRUE` |
| `group_leader` | `WHERE u.group_id = :scope.group_id` |
| `group_member` | `WHERE u.group_id = :scope.group_id` |

#### 7.3 Sent messages visibility

`sent_messages.user_id` фиксирует **автора**. По умолчанию пользователь видит только свои отправленные. На текущей итерации UI «Sent» нет (см. `08-frontend.md`); если появится — `SentMessageService` использует ту же модель: super_admin видит все, лидер/участник видят все из своей группы.

#### 7.4 Tags visibility

Tags остаются **per-user** (см. ADR-0017). Visibility-scope не применяется к `/api/tags/*` — каждый пользователь видит и редактирует **только свои** теги. См. §11 ниже про TD-015 (per-group tags отложено).

В inbox таги в выдаче message'а — это таги **владельца ящика** (`mail_account.user_id`). То есть когда лидер видит письмо участника, рядом с письмом отображаются таги участника. Это согласовано: тег — атрибут владельца ящика, не зрителя; лидер не должен видеть свои таги, прицепленные к чужим письмам (поскольку правила лидера написаны под его ящики). Альтернатива «при просмотре чужого ящика применять свои таги» — отвергнута: запутывает mental model, ломает инвариант «per-user tags».

### 8. Создание mail-аккаунтов с учётом ролей

| Role | Доступ к POST /api/mail-accounts |
| --- | --- |
| `super_admin` | Может создать аккаунт на любого user'а. Поле `target_user_id` в payload (опционально; default = own id). Если указан — backend проверяет существование. |
| `group_leader` | Поле `target_user_id` опционально. Если опущено — `mail_account.user_id = scope.user_id`. Если указан — backend проверяет, что `target_user.group_id == scope.group_id` (т.е. участник той же группы, включая самого лидера). Чужой group → 403. |
| `group_member` | `target_user_id` запрещён в payload (если указан и `!= scope.user_id` → 400 `validation_error`). `mail_account.user_id` всегда `= scope.user_id`. |

Аккаунт **не дублируется** — один account per email per (target) user; visibility между лидером и участником — через JOIN (см. §7.1).

`PATCH`/`DELETE`/`sync-now` /`test` mail-account: разрешено любому, кто видит этот аккаунт через `VisibilityScope` (т.е. для лидера — управление аккаунтами всей группы; для участника — управление аккаунтами всей группы; для super-admin — всеми). Это **сильнее**, чем «видеть, но не управлять»; обоснование: продукт явно требует, чтобы все участники группы могли работать со всеми ящиками группы (например, отвечать на письмо коллеги от имени общего ящика). Если в будущем потребуется разделение «видит, но не редактирует» — отдельный ADR.

### 9. Audit log — новые actions

Расширяем enum `admin_audit.action` (см. `03-data-model.md` секция `admin_audit`):

| action | Когда пишется | actor | target_user_id | details |
| --- | --- | --- | --- | --- |
| `group_create` | super_admin создал группу (auto или manual) | super_admin | leader_user_id | `{group_id, group_name, auto_created: bool}` |
| `group_delete` | super_admin удалил группу | super_admin | (leader.id) | `{group_id, group_name, members_orphaned: int}` |
| `user_role_change` | super_admin изменил `users.role` через PATCH | super_admin | target user | `{from_role, to_role, group_id_before, group_id_after}` |
| `user_group_change` | super_admin изменил `users.group_id` через PATCH (без смены role) | super_admin | target user | `{from_group_id, to_group_id}` |

`create_user`, `reset_password`, `delete_user`, `account_auto_disabled`, `lockout_triggered`, `admin_login`, `admin_logout` — без изменений.

Audit log пишется **только** для действий super-admin'а. group_leader / group_member действия (создание mail_account, отправка писем) **не пишутся** в audit — это обычные user-actions, structlog в stdout достаточно.

### 10. Изменения в session-payload

Redis ключ `session:{token}` (см. `05-modules.md` модуль 3) — JSON-payload получает поле `role` вместо `role: "admin"|"user"`:

**Было** (см. модуль 7 в `05-modules.md`):
```json
{"user_id": 42, "role": "admin", "csrf_token": "...", ...}
```

**Стало**:
```json
{"user_id": 42, "role": "super_admin", "group_id": null, "csrf_token": "...", ...}
```

Поле `group_id` добавляется (`null` для super_admin, integer для остальных) — `SessionData.group_id`. Это позволяет middlewares/dependencies строить `VisibilityScope` без дополнительного DB-lookup на каждом запросе.

`SessionStore.create(user_id, role, group_id, ip, ua)` — сигнатура расширена.

При смене `role` или `group_id` пользователя через admin API — backend **немедленно** revoke'ит все его сессии (`SessionStore.revoke_all_for_user(target_user_id)`), как уже делает `reset_password`. Это исключает «застрявшие» сессии со старым `role`.

### 11. Что не меняется

Out of scope этого ADR (явно):

- **CSRF, rate-limit, cookies, secure flags, lockout, retention, sync-cycle, SMTP-send, MIME** — без изменений.
- **TZ.md F-features** (F1 inbox, F2 read message, F3 send) работают идентично; добавляется только visibility-фильтр.
- **Tags** остаются per-user (см. ADR-0017). Per-group tags — отложено (TD-015).
- **Назначение лидера в существующей пустой группе** — нет endpoint'а; super-admin при необходимости удаляет группу и создаёт заново.
- **Multi-leader группы**, **user в нескольких группах** — отвергнуто (см. Alternatives).
- **Sub-permissions внутри группы** (например, «лидер видит, но участник нет») — отвергнуто на старт.

## Consequences

### Положительные
- **Простая модель.** Три роли, 1:1 group→leader, 1:N group→members. Без матриц прав / ACL / RBAC-фреймворков.
- **Минимальная поверхность изменений.** Одна новая таблица (`groups`), 3 новые колонки в `users`, 1 новая в `mail_accounts` (см. ADR-0020), 4 новых action в `admin_audit`. Backend: новый модуль `groups`, расширенный `admin`, фильтры в `messages`/`accounts`.
- **Centralized authorization.** `VisibilityScope` строится в одном месте (dependency) и пробрасывается; легко тестировать.
- **Audit покрывает все super-admin actions** (group_create, group_delete, role_change, group_change).
- **Backward-compat для super-admin.** Существующий админ из `.env` продолжает работать; миграция переводит `is_admin=true → role='super_admin'`. Ни один endpoint не сломан для существующих пользователей.
- **Telegram Mini-App совместимо.** WebApp-launcher (ADR-0018) не зависит от ролей; cookie-сессия после login содержит новый `role`/`group_id` без изменений auth-flow.

### Отрицательные / компромиссы
- **`group_leader` и `group_member` имеют идентичные права на mail-аккаунты внутри группы.** Реальное отличие — только в том, что лидер «оформляет» группу (его display_name → name), удаление лидера невозможно без удаления группы. Если пользователь захочет «лидер модерирует, участники только читают» — потребуется новый ADR с capability'ями.
- **Per-user tags при group-видимости** = странный UX: лидер видит чужие письма со своими тегами не применёнными. Документируем как known UX-quirk до решения TD-015.
- **DEFERRABLE триггер.** Сложнее в отладке, чем простой CHECK. Документируем в `05-modules.md` модуль `groups` явный raise-handling в backend.
- **`ON DELETE RESTRICT` на leader.** Super-admin **не может** просто удалить лидера; должен сначала удалить группу. UI явно объясняет, что произойдёт.
- **Изменение SessionData breaking-change для существующих сессий.** При деплое все активные сессии становятся невалидными (старая модель `is_admin: bool` — нет в новой схеме). Devops в release-notes указывает: «после релиза все пользователи будут разлогинены». Альтернатива (двойная схема) — overengineering.
- **`group_id` хранится в session payload.** Если super-admin переместит участника в другую группу — старая сессия будет иметь stale `group_id` до следующего login. Решение: при PATCH role/group_id мы revoke_all_for_user (см. §10).

### Tech debt items, привнесённые этим решением
- **TD-015** (новый): Per-group tags. Сейчас теги per-user; если лидер хочет «общие теги для всей группы» — нужно отдельное решение (либо teach `tags.group_id` nullable, либо «копировать теги лидера всем участникам»). См. `100-known-tech-debt.md`.
- **TD-016** (новый): i18n framework. Сейчас весь UI на русском с error-code mapping в Jinja-macro (см. ADR-0021). Если когда-то понадобится EN-вариант — поднимать gettext/babel; пока — overkill.
- **TD-017** (новый): Назначение лидера в существующей пустой группе. Endpoint не реализован (см. §5). Если потребуется — отдельный PATCH `/api/admin/groups/{id}/leader` + flow смены лидерства (revoke_all_for_user old leader, audit `user_role_change`).

## Alternatives considered

### A1. Multi-leader группы (`group_leaders`-таблица many-to-many)
Отвергнуто. Усложняет модель: «кто видит главного лидера», «кто увольняет лидера», «как audit'ить» — без явного запроса от продукта. 1:1 group→leader решает 100% сценариев на старте.

### A2. User в нескольких группах (M:N через `user_groups`)
Отвергнуто. Удваивает SQL-фильтры (через `user_groups` JOIN), усложняет visibility (вычисление union'а). Сценарий «один пользователь в двух командах» не запрашивался; если возникнет — отдельный ADR с пере-моделированием.

### A3. Per-group tags (`tags.group_id` nullable)
Отвергнуто на старт (TD-015). Per-user теги уже работают по ADR-0017; за-deprecate'ить или мигрировать данные требует отдельного решения. Сначала валидируем UX модели групп, потом решаем.

### A4. RBAC через отдельную таблицу `permissions(role, resource, action)`
Отвергнуто. Overengineering. У нас 3 роли, 4–5 ресурсов; switch/case в коде проще, чем DB-driven RBAC.

### A5. ENUM-type Postgres для `role`
Отвергнуто. ENUM нельзя расширить без миграции `ALTER TYPE`; в случае добавления роли (например, `viewer`) проще `TEXT + CHECK`. См. `tag_rules.type` — тот же подход.

### A6. Каскадное удаление лидера (`groups.leader_user_id` ON DELETE CASCADE)
Отвергнуто. Удаление user'а одним кликом тихо распускает группу — invisible side-effect. Super-admin должен явно удалить группу, потом — лидера. `RESTRICT` обеспечивает explicit-flow.

### A7. Дублирование `mail_accounts` для участников группы (каждый участник имеет свою копию account-row для общего email)
Отвергнуто. Нарушает 1NF, утраивает шифрование пароля, конфликтует с UNIQUE `(user_id, email)`, требует sync-cycle на каждой копии. Visibility через JOIN (см. §7.1) решает то же самое за один SQL-фильтр.

### A8. Хранить `role` только в БД (не в session-payload), `VisibilityScope` строить через DB-lookup на каждом запросе
Отвергнуто. Лишний DB round-trip на 100% запросов. Session-payload в Redis хранит role+group_id, обновляется при login; revoke_all_for_user покрывает edge-case изменения роли.

### A9. Хранить `role` в JWT (stateless)
Отвергнуто. Противоречит ADR-0004 (server-side sessions). Stateless JWT не поддерживает мгновенный revoke (нужен blacklist) — против безопасности.

### A10. Лидер видит ВСЁ, участник — только своё
Отвергнуто. Продукт явно сформулировал «общая работа в группе». Если в будущем нужны асимметричные права — добавить capability-list к group_member (новый ADR).

---

## Связанные изменения UI в этом же релизе

Эти изменения формально не относятся к ролевой модели, но вошли в один спринт с ADR-0019 и упоминаются здесь для целостности release-notes. Источник истины каждого — соответствующий раздел `08-frontend.md`.

### UI-1. Восстановление кнопки «Выйти» в topbar

После предыдущего UI-redesign (commit `860b0b8`) кнопка `Log out` была скрыта в Telegram WebApp-режиме CSS-правилом `body.tg-app .topbar nav, body.tg-app .topbar__user { display: none }`, при этом для desktop browser была потеряна видимая кнопка выхода. Возвращена в `<header class="topbar"><nav>` как `<form method="POST" action="/logout">` с `csrf_input()` (см. `08-frontend.md` §2). Тест: на любой авторизованной HTML-странице rendered base включает `action="/logout"` либо в `.topbar`, либо в `.bottom-nav`.

### UI-2. Bottom-navigation для mobile + Telegram WebApp

В `tg-app`-режиме topbar-nav скрывается (Telegram даёт собственный back-button). На mobile (≤640px) topbar тоже плохо помещается. Добавлена фиксированная нижняя панель из 5 пунктов: «Входящие» (`/`), «Почты» (`/accounts`), «Теги» (`/tags`), «Админ» (`/admin` — только для `role='super_admin'`), «Выйти» (`<form method="POST" action="/logout">`). Подробности — `08-frontend.md` §11.

Привязка к ADR-0019: пункт «Админ» **виден только super_admin'у** (не leader/member); это согласовано с разделением Admin API в §10 этого ADR. Для `group_leader` и `group_member` bottom-nav имеет 4 пункта (без «Админ»).
