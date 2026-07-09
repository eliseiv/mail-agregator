# ADR-0040 — Глобальные теги (единый админский каталог)

Статус: `accepted` · Дата: 2026-07-09

Extends [ADR-0017](./ADR-0017-tags.md) (теги) / [ADR-0022](./ADR-0022-telegram-sso-and-notifications.md) (уведомления). Парные ADR — `ADR-0039` (external write API, раздел `/api/external/tags`), CRM `ADR-038`. Реализация раздела `/api/external/tags` описана здесь (модель) + в ADR-0039/04-api-contracts (контракт).

## Context

Теги агрегатора сегодня **приватны на пользователя**: `tags.user_id` NOT NULL FK, `uq_tags_user_name (user_id, name)` (`shared/models/tag.py:35,56`); builtin-теги создаются лениво на первом логине каждого пользователя (`TagsService.ensure_builtin_tags`, вызывается из `auth/service.py:209,273`). Для headless-CRM (собственный UI агрегатора отключается, логина в нём не будет) нужен **единый глобальный каталог тегов**, управляемый админом из CRM.

## Decision

### §1. Модель: `tags.user_id` nullable, `NULL` = глобальный

- `tags.user_id` → **nullable** (`Mapped[int | None]`). `NULL` = глобальный тег (виден/применяется ко всем письмам системы). Не-NULL — сохраняется для обратной совместимости персональных тегов (если понадобятся), но headless-каталог использует только глобальные.
- Существующий `uq_tags_user_name (user_id, name)` **остаётся** (для персональных). Т.к. в Postgres `NULL` не равен `NULL` в составном UNIQUE, добавляется **partial-unique** `uq_tags_global_name (name) WHERE user_id IS NULL` — глобальные имена уникальны.
- FK `ON DELETE CASCADE` не мешает глобальным (у них нет владельца → удаление пользователя их не трогает).
- Новая alembic-ревизия в `migrations/versions/` (down_revision = текущий head `20260706_022`): `ALTER COLUMN user_id DROP NOT NULL` + `CREATE UNIQUE INDEX uq_tags_global_name ON tags(name) WHERE user_id IS NULL`. Данные существующих builtin/персональных тегов не удаляются (см. §3 про пере-сидирование builtin).

### §2. SQL применения правил: включить глобальные теги

Правки `backend/app/tags/sql.py` — критично, т.к. текущий SQL молча выронил бы глобальные теги:

- **`APPLY_TAGS_TO_MESSAGE`** сейчас делает `JOIN users u ON u.id = t.user_id` (`sql.py:199`) и `WHERE (u.id = ma.user_id OR user_groups… OR u.role='super_admin')`. Глобальный тег (`t.user_id IS NULL`) при INNER JOIN на `users` **выпал бы** (нет строки user). Фикс: `JOIN` → `LEFT JOIN users u ON u.id = t.user_id`, а видимость-предикат дополняется веткой глобального тега:
  ```sql
  WHERE ( t.user_id IS NULL                      -- глобальный тег: применяется ко ВСЕМ письмам
          OR u.id = ma.user_id
          OR (ma.group_id IS NOT NULL AND EXISTS (
                 SELECT 1 FROM user_groups ug
                 WHERE ug.user_id = u.id AND ug.group_id = ma.group_id))
          OR u.role = 'super_admin' )
  ```
- **`APPLY_TAG_TO_EXISTING`** сейчас гейтит видимость `CAST(:is_super_admin AS BOOLEAN) OR ma.user_id=:user_id OR user_groups(:user_id)` (`sql.py:270-289`). Для глобального тега применение должно достигать **каждого** письма (как super_admin). При apply глобального тега передаётся `is_super_admin=TRUE` (короткое замыкание видимости в TRUE, уже реализовано round-26) — отдельная ветка `t.user_id IS NULL` в SQL не требуется, но `user_id`-bind для глобального тега семантически иррелевантен (см. §5, external endpoint передаёт `is_super_admin=TRUE` для глобального применения).
- **Идемпотентность** `ON CONFLICT (message_id, tag_id) DO NOTHING` — **сохраняется** без изменений.
- **Семантика матчинга ADR-0017** (whole-word, case-sensitive, экранирование паттерна, `norm()`, boundary-классы, `body_contains` по `body_text`+strip_tags(`body_html`), `sender_contains` по адресу+display-name) — **не меняется**. Меняется только предикат видимости (кто «видит» тег), не предикат совпадения правила.

### §3. Builtin-теги — глобальные, сидирование в lifespan

- `backend/app/tags/builtin.py` (`BUILTIN_TAGS`) переводятся в **глобальные** (`user_id = NULL`, `is_builtin = TRUE`). Каталог правил/цветов/`match_mode` не меняется.
- Создание переносится из ленивого `ensure_builtin_tags(user_id)` (per-login) в **идемпотентное сидирование на старте приложения** (lifespan, по образцу `seed_super_admin`): новая функция `seed_builtin_tags(session)`, вызывается в `backend/app/main.py::create_app` lifespan рядом с `seed_super_admin`. Идемпотентность — по `uq_tags_global_name` (`INSERT … ON CONFLICT (name) WHERE user_id IS NULL DO NOTHING`, либо предварительная проверка наличия глобального builtin). Цвета валидируются против `PALETTE_COLORS` (defence-in-depth, как сейчас).
- Ленивый `ensure_builtin_tags` per-login **больше не вызывается** (UI-логина в агрегаторе не будет — `ADR-0041`). Оставшийся код `ensure_builtin_tags` может быть удалён/оставлен неиспользуемым (решение backend S1; поведенчески он мёртв). Существующие персональные builtin-строки (созданные до миграции) остаются в БД как персональные и на глобальный каталог не влияют; при желании отдельная миграция может их вычистить (не обязательно для корректности — глобальный каталог самодостаточен).

### §4. Раздел `/api/external/tags` — CRUD (write, ADR-0039 §1 gate)

Все — от имени «глобального» владельца (`user_id IS NULL`). Переиспользуют `backend/app/tags/service.py` (нужен глобально-скоупленный вариант `get_owned`: для внешних тегов «owned» = `user_id IS NULL`). Под `EXTERNAL_WRITE_ENABLED` + `LIMIT_EXTERNAL_WRITE` (ADR-0039 §1). Контракт — [04-api-contracts.md](../04-api-contracts.md#external-write-tags):

- `GET /api/external/tags` — список глобальных тегов с правилами и `color` (`{tags: [ExternalTagFullDTO{id, name, color, match_mode, is_builtin, rules:[{id, type, pattern, created_at}], created_at, updated_at}]}`). Под `EXTERNAL_API_KEY` (read; но т.к. в одном разделе с write — практично гейтить чтение каталога тем же ключом; write-действия дополнительно под `EXTERNAL_WRITE_ENABLED`).
- `POST /api/external/tags` — `{name, color, match_mode?}` (`match_mode` default `any`) → `ExternalTagFullDTO`. `409` на дубль имени (`uq_tags_global_name`). Цвет валидируется `PALETTE_COLORS` / CHECK `^#[0-9A-Fa-f]{6}$`.
- `PATCH /api/external/tags/{id}` — `{name?, color?, match_mode?}` → `ExternalTagFullDTO`. `404` для не-глобального/несуществующего id.
- `DELETE /api/external/tags/{id}` → `204`. Builtin-тег (`is_builtin=TRUE`) удалять нельзя → **`409 conflict`** (`ConflictError` из `TagsService.delete_global`) — переименование/правка правил разрешены. Это **отдельный** код от внутреннего UI-шного `DELETE /api/tags/{id}` (`400 cannot_delete_builtin_tag`/`CannotDeleteBuiltinTagError`, ADR-0017): external-контур сознательно отдаёт `409 conflict` (согласовано с нормативной таблицей `04-api-contracts.md` §4f-tags).
- `POST /api/external/tags/{id}/rules` — `{type, pattern}` (`type ∈ {subject_contains, body_contains, sender_contains, sender_exact}`) → `{id, type, pattern, created_at}`.
- `DELETE /api/external/tags/{id}/rules/{rule_id}` → `204`.
- `POST /api/external/tags/{id}/apply-to-existing` — применяет правила глобального тега ко всем существующим письмам (лимит `APPLY_TO_EXISTING_LIMIT=100_000`, уже есть) → `{applied_count}`. Внутри — `_apply_tag_to_existing(user_id=<любой>, tag_id, is_super_admin=True)` (глобальный охват через short-circuit видимости).

### §5. Анализ регрессии доставки (ОБЯЗАТЕЛЬНО — по требованию ADR)

**Вопрос: не сломает ли смена модели тегов Telegram-доставку / webhooks / forwarding?** Проведён аудит триггеров каждого канала. Вывод по Telegram зависит от режима `TG_NOTIFY_ALL_MESSAGES` — разобраны **обе** ветки.

- **Telegram-уведомления — доставка получателям не ломается; громкость зависит от режима.** Enqueue-гейт: `worker/app/sync_cycle.py:330` — `if settings.TG_NOTIFY_ALL_MESSAGES or applied > 0: notified_message_ids.append(...)`. Получатели в обоих режимах резолвятся по группе (`list_recipients_for_message`, ADR-0039 §Q-0039-1), не по тегам; теги влияют лишь на чипы `tag_names` в тексте (`push_notify_dispatch.py:145-155`).
  - **`TG_NOTIFY_ALL_MESSAGES = true` (default, прод):** `or` коротко замыкается — уведомляется КАЖДОЕ входящее письмо независимо от `applied`. Громкость от модели тегов **не зависит** → регрессии нет. Чипы: сегодня per-user builtin даёт строку `message_tags` на каждого видящего юзера, глобальный — одну; набор имён после collapse-by-name (`push_notify_dispatch.py:148-155`) эквивалентен.
  - **`TG_NOTIFY_ALL_MESSAGES = false` (опция, поддержана в `config.py`, покрыта тестами):** гейт = `applied > 0` (уведомляем только письма, получившие ≥1 тег). Здесь есть **осознанное изменение поведения**, а не «нет регрессии»: раньше builtin-теги были per-user и создавались лениво при логине (`ensure_builtin_tags` per-login) — для команды, где **никто не логинился в агрегатор**, builtin-тегов не существовало → `applied = 0` → уведомления **не было** даже на письмо с триггер-фразой. После перевода builtin в глобальные (§3) они матчат письма **независимо от логина** → `applied > 0` наступает чаще → в false-режиме **растёт число уведомлений** (для команд без логина в агрегатор — с нуля до «по числу тегированных писем»).

    **Решение — принять изменение как корректное, без митигации.** Обоснование: (1) семантика false-режима — «уведомлять о письмах, соответствующих каталогу классификации»; раньше она молча зависела от постороннего факта «логинился ли кто-то в отключаемый UI агрегатора» (`ADR-0041`) — письмо с триггер-фразой у «незалогиненной» команды не уведомлялось вопреки намерению; это был скрытый дефект, а не фича. Глобальный каталог делает поведение детерминированным и совпадающим с ожиданием оператора. (2) В headless-режиме UI-логина в агрегатор не будет вовсе (`ADR-0041`), поэтому старое «per-user lazy» в false-режиме деградировало бы до «уведомлений нет никогда» — что хуже. (3) Прод работает в **default `true`** (громкость независима от тегов), поэтому на текущем деплое фактического роста нет; изменение проявляется только если оператор осознанно включит false. **Оператору, не желающему роста:** оставить default `true` (громкость независима от тегов) либо сузить каталог правил. **S4 (qa):** тесты false-режима, ожидавшие «нет уведомления при отсутствии per-user builtin», **устаревают** — переписать под глобальный каталог (`applied>0` для письма, матчащего глобальный тег); это правка тест-файлов (зона qa), не дефект.
- **Webhooks — НЕ регрессируют и изолированы от тегов.** `sql.py:190` («The webhook channel stays isolated from these tags (see ADR-0023 §3.2)») + `sync_cycle` enqueue webhooks по тем же `message_ids` (все новые письма команды), резолв получателя по `webhooks.group_id`. Теги в пайплайн webhooks не входят.
- **Forwarding — НЕ регрессирует.** `forward_dispatch` триггерится на всех новых входящих письмах команды (`group_id IS NOT NULL`), резолв по `group_forwarding.group_id`; тегами не гейтится (ADR-0034).

**Единственная точка, которую МОЖНО сломать сменой модели, помимо громкости false-режима выше** — сам SQL применения тегов (`JOIN users` выронил бы глобальные теги). Устранено в §2 (`LEFT JOIN` + ветка `t.user_id IS NULL`). Вывод: доставка получателям в обоих режимах не ломается; в false-режиме — осознанный рост громкости (принят, см. выше); риск SQL — покрывается тестами qa (S4).

**Производительность.** Глобальные теги применяются ко всем письмам → `APPLY_TAG_TO_EXISTING` для глобального тега сканирует весь корпус (как super_admin-apply сегодня). Лимит `APPLY_TO_EXISTING_LIMIT=100_000` уже гейтит синхронный путь (`422` при превышении). План `APPLY_TAG_TO_EXISTING` проверить на реальном объёме (qa/perf, отслеживается как остаточный риск в CRM `ADR-038`).

## Consequences

- Единый админский каталог тегов, управляемый из CRM; builtin-теги глобальны и сидируются на старте (детерминированно, без зависимости от логина).
- Доставка получателям (TG/webhooks/forwarding) не ломается — доказано аудитом триггеров. Исключение — **громкость** TG в режиме `TG_NOTIFY_ALL_MESSAGES=false`: осознанно растёт (глобальные builtin матчат письма независимо от логина); принято как исправление скрытого дефекта, прод работает в default `true` (§5).
- Персональные теги технически возможны (`user_id` не NULL), но headless-каталог их не использует; существующие персональные builtin-строки становятся «осиротевшими» персональными (безвредны).
- Миграция аддитивна (drop NOT NULL + partial unique index); откат — восстановить NOT NULL после переноса глобальных в персональные (маловероятно).

## Alternatives considered

- **Оставить per-user теги, дублировать каталог во все команды.** Отклонён: N копий тега на письмо (шум `message_tags`), рассинхрон при правке, нет «единого админского каталога».
- **Новая таблица `global_tags` вместо nullable `user_id`.** Отклонён: дублирование всей логики матчинга/SQL/сидирования; nullable `user_id` минимально инвазивен и переиспользует `TagsService`/`sql.py`.
- **Гейтить чтение `/api/external/tags` отдельным флагом.** Отклонён: чтение каталога безвредно и нужно CRM всегда под `EXTERNAL_API_KEY`; отдельный флаг — лишняя ручка.
