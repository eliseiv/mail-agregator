# ADR-0017: Теги для писем — rule-based авто-классификация и пользовательские правила

- **Статус:** accepted
- **Дата:** 2026-05-07

## Context

Пользователи (включая супер-админа) хотят быстро визуально классифицировать входящие письма. Текущая модель имеет только бинарный признак `is_read` — недостаточно для разделения писем по бизнес-категориям (например, диспуты от Apple, уведомления о подписках, продление сертификатов).

Запрос продукта (TZ Sprint feature "tags"):

1. Четыре **встроенных** тега, срабатывающих по детерминированным правилам при синхронизации:
   - `DPLA.PLA` — `subject` ИЛИ `body_text` содержит `"DPLA"` или `"PLA"`.
   - `Диспут` — `subject` содержит `"Apple Inc"` ИЛИ `from_addr = "AppStoreNotices@apple.com"`.
   - `Отменить подписку` — `body_text` содержит `"cancel"` или `"subscription"`.
   - `Продление аккаунта` — `body_text` содержит `"Your Distribution Certificate will no longer be valid in 30 days"`.
2. UI для **пользовательских** тегов — кнопка `+ Добавить тег`, форма с именем тега и набором условий (keyword-в-subject / keyword-в-body / sender substring / sender exact). Чекбокс "Применить к существующим письмам" при создании.

Ключевые ограничения, под которые мы выбираем дизайн:
- Объёмы малые (≤ 500 mail-аккаунтов × 30 дней ретенции × ~50 писем/день ≈ 750k писем max в БД); см. `03-data-model.md` секция "Объёмные оценки".
- Стек уже зафиксирован — Postgres 16 + Redis + APScheduler-worker (см. ADR-0001, ADR-0003).
- Плагинная сложная фильтрация / regex-движок не нужны; правила — простые substring-матчи.
- Sync — `apply_tags` должен встраиваться в существующий цикл (`worker.sync_cycle.save_message`), не блокируя пакетную обработку (см. ADR-0008, ADR-0013).
- Безопасность — пользователь A не может прицепить свой тег к письму пользователя B, и не может видеть теги пользователя B.

## Decision

### 1. Per-user изоляция тегов

Все теги — **per-user**. Тег `DPLA.PLA` создаётся **отдельно для каждого пользователя**; admin тоже видит свои четыре builtin-тега и может создавать собственные. У двух разных пользователей могут быть теги с одинаковыми именами — это два разных объекта в БД (разные `user_id`).

Обоснование:
- Изоляция «из коробки» через FK на `users(id)` + JOIN при чтении.
- Нет глобального namespace — нет конфликтов имён между пользователями.
- Builtin-теги создаются автоматически при первом login пользователя (post-login hook в `auth.AuthService.login`); не data-миграция, чтобы не плодить мёртвых записей для пользователей, которые никогда не логинились.

### 2. Схема БД

Три новые таблицы (DDL — см. `03-data-model.md`):

- `tags` — `(id, user_id, name, color, is_builtin, created_at, updated_at)`. UNIQUE `(user_id, name)`.
- `tag_rules` — `(id, tag_id, type, pattern, created_at)`. Поле `type` — enum-string: `subject_contains | body_contains | sender_contains | sender_exact`. Несколько rules для одного тега соединяются логическим **OR** (см. ниже).
- `message_tags` — many-to-many link `(message_id, tag_id, created_at)`. PK = `(message_id, tag_id)`.

Каскадные удаления:
- `tags.user_id` → `users(id)` `ON DELETE CASCADE`. При удалении пользователя его теги исчезают; `message_tags` каскадятся через `tag_id`.
- `tag_rules.tag_id` → `tags(id)` `ON DELETE CASCADE`.
- `message_tags.message_id` → `messages(id)` `ON DELETE CASCADE` (retention cleanup автоматически чистит links при удалении messages).
- `message_tags.tag_id` → `tags(id)` `ON DELETE CASCADE`.

Индексы:
- `tags`: PK `(id)`, UNIQUE `(user_id, name)`, INDEX `(user_id)` (для list).
- `tag_rules`: PK `(id)`, INDEX `(tag_id)` (для load-all-rules-for-tag).
- `message_tags`: PK `(message_id, tag_id)`, дополнительный INDEX `(tag_id, message_id)` (для list-messages-with-tag, см. inbox filter `tag_id`).

### 3. Логика между rules — OR

В рамках одного тега несколько rules объединяются по **OR**. То есть тег прикладывается к письму, если **хотя бы одно** правило сработало.

Обоснование:
- Соответствует mental model пользователя: «прицепи этот тег если письмо про X **или** про Y».
- AND-логика выражается множеством отдельных тегов (если очень нужна — не блокер, но и не ясный пользовательский запрос).
- Не плодит дополнительных полей (group / boolean) в `tag_rules`; UI остаётся простым (плоский список).

### 4. Сравнение — substring case-insensitive (ILIKE), без regex

Все 4 типа правил выполняются как Postgres `ILIKE '%' || pattern || '%'` (case-insensitive substring). Для `sender_exact` — `LOWER(from_addr) = LOWER(pattern)`.

Обоснование:
- Простота. Пользователь вводит «Apple Inc» — он ожидает, что найдётся в любом регистре.
- Безопасность. Regex даёт ReDoS (catastrophic backtracking); ILIKE — линейная сложность.
- Patterns параметризованы — SQL-инъекций нет.
- `%`/`_` в pattern — никакой специальной обработки; пользователь может ввести `%` намеренно как wildcard. Документируем это как побочный эффект (примечание в UI: "Поддерживаются `%` для произвольной подстроки и `_` для одного символа"). Альтернативно — escape; решение «оставить как есть» проще и работает.

### 5. Apply tags при синхронизации (worker)

В `worker.sync_one_account.save_message`, **в той же транзакции**, что и `INSERT INTO messages ... ON CONFLICT (mail_account_id, uidvalidity, uid) DO NOTHING RETURNING id`, выполняется:

```sql
INSERT INTO message_tags (message_id, tag_id)
SELECT :message_id, t.id
FROM tags t
JOIN mail_accounts ma ON ma.user_id = t.user_id
JOIN messages m ON m.id = :message_id AND m.mail_account_id = ma.id
WHERE EXISTS (
    SELECT 1 FROM tag_rules r WHERE r.tag_id = t.id AND (
        (r.type = 'subject_contains' AND m.subject ILIKE '%' || r.pattern || '%') OR
        (r.type = 'body_contains'    AND m.body_text ILIKE '%' || r.pattern || '%') OR
        (r.type = 'sender_contains'  AND m.from_addr ILIKE '%' || r.pattern || '%') OR
        (r.type = 'sender_exact'     AND LOWER(m.from_addr) = LOWER(r.pattern))
    )
)
ON CONFLICT (message_id, tag_id) DO NOTHING;
```

Один SQL-запрос на одно письмо — все теги пользователя проверяются за один round-trip. Транзакционность: если apply упал — message тоже откатывается (избегаем orphan messages без тегов). При `INSERT messages ... ON CONFLICT DO NOTHING` без RETURNING (когда письмо уже было — не добавилось) `apply_tags` пропускается (нет нового `message_id`).

Стоимость: для 10 тегов × 3 правил пользователя — Postgres делает 1 indexed scan по `tag_rules` per tag (тривиально, объёмы ничтожны). Для 50 писем в пакете — 50 таких запросов. Управляемо при текущих объёмах.

`worker.save_message` НЕ должен fall-back'аться на «message без тегов» при ошибке apply-tags — иначе invariant ломается. Если что-то падает — let it crash (worker retry per next sync cycle).

### 6. Builtin-теги — post-login hook

Builtin-теги создаются один раз для каждого пользователя — при его **первом успешном login**. Реализуется как часть `auth.AuthService.login` (после успешного `argon2.verify`, перед возвратом session): вызов `TagsService.ensure_builtin_tags(user_id)`.

`ensure_builtin_tags`:
- Проверяет `SELECT id FROM tags WHERE user_id=:uid AND is_builtin=true LIMIT 1`. Если есть — return (идемпотент).
- Иначе — INSERT 4 builtin tags + tag_rules в одной транзакции. Список тегов и правил фиксирован в коде (`backend/app/tags/builtin.py`).
- Также вызывается из `auth.AuthService.complete_set_password` (set-password flow завершает первый «нормальный» login).

Альтернатива — data-миграция / on-create-user — отвергнута: лишние записи для never-logged-in пользователей (создаются админом, могут не залогиниться), плюс data-миграции противоречат принципу «миграции = только schema» (см. `03-data-model.md`). Post-login hook — простой, идемпотентный, всегда работает для активных пользователей.

Список builtin-тегов (формирование — детерминированное; реализация в `backend/app/tags/builtin.py`):

| Имя | Цвет | Rules |
| --- | --- | --- |
| `DPLA.PLA` | `#2563eb` (blue) | `subject_contains: DPLA`, `subject_contains: PLA`, `body_contains: DPLA`, `body_contains: PLA` |
| `Диспут` | `#dc2626` (red) | `subject_contains: Apple Inc`, `sender_exact: AppStoreNotices@apple.com` |
| `Отменить подписку` | `#f59e0b` (amber) | `body_contains: cancel`, `body_contains: subscription` |
| `Продление аккаунта` | `#16a34a` (green) | `body_contains: Your Distribution Certificate will no longer be valid in 30 days` |

`is_builtin=true` — пользователь не может удалить такой тег (см. API `DELETE /api/tags/{id}` — 400 на builtin). Но может **переименовать**, **изменить цвет** и **добавлять/удалять rules** к нему (согласовано: builtin — это только защита от удаления). Это упрощает развитие — пользователь может расширить покрытие правил без потери ID-стабильности.

### 7. "Apply to existing" — синхронно в API endpoint create_tag

При создании тега с `apply_to_existing=true` API endpoint `POST /api/tags` после INSERT тега и rules выполняет один SQL-запрос:

```sql
INSERT INTO message_tags (message_id, tag_id)
SELECT m.id, :tag_id
FROM messages m
JOIN mail_accounts ma ON ma.id = m.mail_account_id
WHERE ma.user_id = :user_id AND EXISTS (
    SELECT 1 FROM tag_rules r WHERE r.tag_id = :tag_id AND (
        (r.type = 'subject_contains' AND m.subject ILIKE '%' || r.pattern || '%') OR
        (r.type = 'body_contains'    AND m.body_text ILIKE '%' || r.pattern || '%') OR
        (r.type = 'sender_contains'  AND m.from_addr ILIKE '%' || r.pattern || '%') OR
        (r.type = 'sender_exact'     AND LOWER(m.from_addr) = LOWER(r.pattern))
    )
)
ON CONFLICT (message_id, tag_id) DO NOTHING;
```

Стоимость на максимуме: ~150k messages per user (5 пользователей × ~30k мессаджей с учётом 30-day retention; см. `03-data-model.md` — суммарно 750k delà'd на 5 человек) × 1 indexed seq scan по `tag_rules`. Postgres выполняет это за **доли секунды на тёплом кэше; верхняя граница ~5 секунд** на современном дешёвом VM. Synchronous вызов в API — приемлемо.

Защитный лимит для синхронного path:
- Перед INSERT'ом — `SELECT count(*) FROM messages m JOIN mail_accounts ma ... WHERE ma.user_id=:uid` с уровнем `count > 100000` → возвращаем ошибку `tag_apply_too_many` (429 / 422; см. `04-api-contracts.md`) и предлагаем создать тег без `apply_to_existing`. Worker подхватит applying на новых письмах, а на старых — пользователь подождёт следующего ретенционного cleanup или вручную пересоздаст тег.
- HTTP timeout endpoint'а `POST /api/tags` — 30 секунд (общий backend default). Запрос свыше падает 504/500.

Альтернатива — фоновое применение через worker / Redis queue — рассматривалась (см. Alternatives), но отвергнута для первой итерации: synchronous простота на нашем масштабе достаточна, plus отдельный async-флоу = новый failure mode и UI status polling (overengineering под `~5 пользователей × ~100 ящиков`).

### 8. Inbox filter by tag

`GET /` и `GET /api/messages` принимают опциональный query-параметр `tag_id` (BIGINT). Backend дополнительно проверяет ownership (`tags.user_id == current_user.id`) и присоединяет JOIN `message_tags mt ON mt.message_id = m.id AND mt.tag_id = :tag_id`. Фильтр совмещаем с уже существующими `account_id`, `unread`, `cursor`.

Tag-фильтр НЕ ломает keyset-pagination (cursor по `(internal_date DESC, id DESC)` остаётся стабильным).

### 9. Ownership / Authorization

Все endpoints `/api/tags/...` и `/api/tags/{id}/rules/...` обязательно проверяют:
- `tag.user_id == request.state.session.user_id`. Чужой `tag_id` → 404 (не 403, чтобы не утечкой существование чужого).
- При `tag_id` в filter inbox — то же. Невалидный/чужой → 404.

`is_builtin=true` → запрет на DELETE (`400 cannot_delete_builtin_tag`). Изменение name/color/rules — разрешено.

## Consequences

### Положительные
- **Маленькая поверхность изменений.** Добавляется один service-модуль, три таблицы, ~7 endpoints. Существующая worker pipeline получает один SQL hook.
- **Без новых внешних зависимостей.** Работает на уже выбранном Postgres + FastAPI + APScheduler.
- **Производительность приемлемая.** При наших объёмах (≤ 750k писем максимум, ≤ 5 пользователей) ILIKE-сканы по `messages` и tag-checks при sync укладываются в текущий sync-cycle window (5 минут).
- **Полная транзакционность.** `INSERT message + apply_tags` — атомарно; нет orphan-записей без тегов. Retention cleanup чистит `message_tags` через CASCADE — нет orphan tag-links.
- **Backward-compatible.** Существующие endpoints `/`, `/api/messages`, `/messages/{id}` дополняются опциональными полями (`tags: [...]`); отсутствие `tag_id` в query не меняет поведение.
- **Безопасно.** ILIKE без regex → нет ReDoS. Pattern параметризован → нет SQL-инъекции. Per-user изоляция через FK + JOIN.

### Отрицательные / компромиссы
- **`apply_to_existing` синхронен.** На объёмах сильно выше текущих (>100k messages per user) endpoint начнёт упираться в таймаут. Защищены лимитом `100000` (TD-011, ниже).
- **`%` и `_` в pattern — wildcards.** Пользователь может случайно ввести `%` и получить unexpected match. Документируем в UI, не escape'им. Если станет проблемой — escape `LIKE` через `r.pattern || '%' ESCAPE '\'` + `replace(pattern, '%', '\%')`.
- **Нет AND / NOT / приоритетов между rules одного tag.** Только OR. Покрывает 95% запросов; для оставшихся — несколько отдельных тегов.
- **DB-storage растёт линейно.** ~50 байт на link `message_tags`. Для 750k messages × среднем 3 теги/message = 2.25M строк ≈ 110 MB. Приемлемо.
- **Нет UI для bulk-tagging вручную.** Tag прикладывается только через rules. Мануальный «tag this message» — отдельная функция, не в этом scope (если потребуется — отдельный ADR).
- **Builtin-теги создаются на login, а не на user_create.** Админ создаёт пользователя → тегов нет, пока тот не залогинится. Безопасно, но если admin захочет посмотреть «какие у user'а будут теги» через `/admin` — увидит пустой список. Документируем в UX.

### Tech debt items, привнесённые этим решением
- **TD-011** (новый): `apply_to_existing` синхронен, лимит 100k messages. Если масштаб вырастет — переделать на background worker job + UI status polling. См. `100-known-tech-debt.md`.
- **TD-012** (новый): `%`/`_` в pattern — невэскейплены. Если пользователи начнут жаловаться — добавить escape в `TagsService.add_rule`. См. `100-known-tech-debt.md`.

## Alternatives considered

### A1. Глобальные теги (shared across users)
Отвергнуто. Создаёт coupling: один пользователь меняет правило → влияет на других. Сложнее authz: нужны permissions «кому видно/кому управлять». Per-user проще и достаточно.

### A2. Regex-matching вместо ILIKE
Отвергнуто. ReDoS — реальная угроза (см. ATT&CK CWE-1333). Pattern вводит пользователь → нет способа гарантировать линейность. ILIKE покрывает 95% бизнес-сценариев и линеен.

### A3. AND/OR/NOT логика между rules + group_id в tag_rules
Отвергнуто на старт. UI становится «query-builder» — большой scope. Текущий запрос продукта решается множеством отдельных тегов (тег = одна категория). Если придёт явный фидбэк — новый ADR.

### A4. Async фоновое применение `apply_to_existing` через worker
Отвергнуто на старт. На текущем масштабе синхронное выполнение (≤5 секунд на верхней границе) приемлемо. Async добавляет:
- Redis queue или DB-table «pending tag-apply jobs».
- Worker handler для job'ов.
- UI с прогресс-баром или polling-эндпоинтом.
- Race conditions (что если пользователь удалит тег пока job в очереди).

При N=5 пользователей × ≤ 30k мессаджей это overengineering. См. TD-011 — пере-оценить при росте масштабов.

### A5. Data-миграция для builtin-тегов (создаются один раз для всех существующих + при INSERT user)
Отвергнуто. Миграции — schema-only по правилу `03-data-model.md`. Плюс плодит мёртвые записи: admin создал user → builtin-теги сразу есть → user никогда не залогинился → теги мёртвый груз. Post-login hook идемпотентен и elegant.

### A6. Tag-color picker свободного RGB
Отвергнуто как overengineering. Используем фиксированный набор из 8 цветов (chips/swatches в UI) — palette в `08-frontend.md` секция 5.1. Сужает выбор, но 8 цветов покрывают все типовые семантики (важное / срочное / спам / etc). Колонка `tags.color` хранит hex — backend дополнительно валидирует, что hex входит в whitelist из 8 значений палитры (см. `08-frontend.md` сек. 5.1). Это исключает inline-style в HTML и сохраняет CSP `style-src 'self'` без ослаблений.

### A7. Хранить tag-applied-flag в `messages` (boolean column)
Отвергнуто. Не масштабируется на N тегов; нарушает 1NF. Many-to-many через `message_tags` — каноническое решение.

### A8. Применять теги ТОЛЬКО на новых письмах (без `apply_to_existing`)
Отвергнуто. Прямой запрос продукта — чекбокс есть. Лишает пользователя возможности «нашёл паттерн → пометить весь существующий inbox».

### A9. Builtin-теги создаются `seed_super_admin` (как и сам admin)
Отвергнуто. `seed_super_admin` касается только super-admin'а. Builtin-теги нужны всем пользователям, и time-of-creation у обычных пользователей — `auth.complete_set_password` (первый login), а не seed.
