# ADR-0036 — Backward / newest-first пагинация внешнего PULL-API

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-07-04 |
| Заменяет / отменён | — (не отменяет ADR-0029; **extends ADR-0029** — добавляет обратный/latest-режим тому же `GET /api/external/messages`) |

## Context

ADR-0029 даёт **только forward-keyset** для `GET /api/external/messages`: `since_id` (default `0`, `ge=0`) + `limit` (`1..200`), выборка `WHERE id > since_id ORDER BY id ASC LIMIT limit`, `next_since_id = max(id)`, `has_more = len==limit`. Это движение **старые → новые**: чтобы дойти до самых свежих писем, forward-курсор требует перебрать всю историю от `since_id=0`.

Появился новый потребитель — CRM «Почты» (внешний прокси поверх этого API). Ему нужен **обратный** паттерн — «бесконечная лента» без кнопки:

1. При открытии показать **самые свежие N** писем сразу (newest-first, id DESC), не перебирая всю историю.
2. При скролле вниз подгружать более **старые** письма страницами.

Forward-курсор (ADR-0029) этого не позволяет: нельзя «прыгнуть в конец» (к max id) без полного прохода, и порядок id ASC даёт oldest-first — противоположный ленте.

**Ограничения, которые нельзя нарушать:**
- Read-only модель ADR-0029 (только `GET`, только поля письма, super_admin visibility, canonical-дедуп) — не трогается.
- Reply-endpoint ADR-0035 (`POST …/reply`) — не трогается.
- Auth / rate-limit / CSRF-exempt / redact — те же, что у forward-режима.
- Обратная совместимость: существующий forward-клиент (B2B-партнёр ADR-0029) не должен получить ни изменения дефолтного поведения, ни новых обязательных полей/параметров.
- Threading reply, теги (`ExternalTagDTO`), `mail_account`-DTO — без изменений.

## Decision

Добавить к **тому же** `GET /api/external/messages` явный параметр направления `order` и обратный курсор `before_id`. Forward-режим ADR-0029 сохраняется **как есть** (BC); backward/latest — новый режим. Режимы **взаимоисключающи в одном запросе**.

### 1. Параметры (расширение)

| Параметр | Тип / границы | Default | Режим | Семантика |
| --- | --- | --- | --- | --- |
| `order` | enum `asc` \| `desc` | `asc` | switch | `asc` = forward keyset (ADR-0029, oldest→newest). `desc` = backward/latest (newest→older). |
| `since_id` | `int`, `ge=0` | `0` | только `asc` | `WHERE id > since_id`. Как ADR-0029. В `desc` — **запрещён** (`400`). |
| `before_id` | `int`, `ge=1` | *(отсутствует)* | только `desc` | Присутствует → `WHERE id < before_id`. Отсутствует → latest N (без нижней id-границы). В `asc` — **запрещён** (`400`). |
| `limit` | `int`, `1..200` (hard cap 200) | `50` | оба | Как ADR-0029. Не изменён. |

**Почему `order`, а не «просто presence of `before_id`».** Первая (latest) страница ленты не имеет курсора — потребитель ещё не знает `max(id)`. Одна presence-of-`before_id`-эвристика не может отличить «дай самые свежие» от «продолжай с before_id», а отсутствие любых параметров в ADR-0029 уже означает forward-from-0 (менять этот дефолт — breaking change). Явный `order=desc` решает обе задачи: он и **включает** backward-режим (latest-first при отсутствии `before_id`), и задаёт сортировку выдачи; `before_id` — только курсор продолжения внутри `desc`. `order=asc`/omitted оставляет поведение ADR-0029 байт-в-байт.

### 2. Семантика выборки по режимам

**Forward (`order=asc`, default) — ADR-0029, без изменений:**
```sql
WHERE m.id > :since_id
  AND m.mail_account_id IN (:canonical_ids)
ORDER BY m.id ASC
LIMIT :limit;
```

**Backward — latest (`order=desc`, `before_id` отсутствует):**
```sql
WHERE m.mail_account_id IN (:canonical_ids)
ORDER BY m.id DESC
LIMIT :limit;               -- самые свежие N, newest-first
```

**Backward — older page (`order=desc`, `before_id` задан):**
```sql
WHERE m.id < :before_id
  AND m.mail_account_id IN (:canonical_ids)
ORDER BY m.id DESC
LIMIT :limit;              -- страница старее before_id, newest-first
```

- `:canonical_ids` = `MailAccountsRepo.list_canonical_account_ids()` (`MIN(id)` per `LOWER(email)`) — **тот же** canonical-дедуп дубль-ящиков, что в ADR-0029 §5. Применяется во всех трёх режимах идентично.
- Теги подгружаются тем же batch-запросом `IN (:message_ids)` и возвращаются в **обоих** режимах (см. §4).
- `messages.id BIGSERIAL` монотонен по insert-order → `ORDER BY id DESC` использует reverse-scan по PK; **новых индексов/миграций не требуется** (PK покрывает обе сортировки; `INDEX (mail_account_id, internal_date DESC)` покрывает canonical-фильтр, `03-data-model.md:378`).

### 3. Ответ 200 по режимам

**Forward (`order=asc`) — без изменений (ADR-0029 §2):**
```json
{ "messages": [ /* ExternalMessageDTO, id ASC */ ], "next_since_id": 12345, "has_more": true }
```

**Backward (`order=desc`):**
```json
{ "messages": [ /* ExternalMessageDTO, id DESC (newest-first) */ ], "next_before_id": 12001, "has_more": true }
```

| Поле | Режим | Значение |
| --- | --- | --- |
| `messages[]` | оба | `ExternalMessageDTO` (ADR-0029 §6) — **без изменений**. В `asc` порядок `id ASC`, в `desc` — `id DESC` (newest-first). |
| `next_since_id` | только `asc` | `max(id)` батча; пусто → входной `since_id`. Как ADR-0029. **Отсутствует** в `desc`-ответе. |
| `next_before_id` | только `desc` | `min(id)` батча (= `id` последнего элемента, т.к. DESC) — передать в `before_id` для следующей (более старой) страницы. `null`, если батч пуст (старых больше нет). **Отсутствует** в `asc`-ответе. |
| `has_more` | оба | `len(messages) == limit`. `false` ⇒ страниц больше нет (в `asc` — новее; в `desc` — старее). |

**Итерация ленты (потребитель):**
1. Первый экран: `GET ?order=desc&limit=50` → newest 50 + `next_before_id`.
2. Скролл вниз: `GET ?order=desc&before_id=<next_before_id>&limit=50` → следующая страница старее.
3. Стоп, когда `has_more=false` (или `messages=[]`/`next_before_id=null`).

Пустой `desc`-результат: `{ "messages": [], "next_before_id": null, "has_more": false }` (нет писем вообще, либо нет писем с `id < before_id`).

### 4. Теги в обоих режимах

`ExternalMessageDTO.tags` (`ExternalTagDTO{id,name,color}`, ADR-0029 §6, ADR-0017) возвращается **идентично** в `asc` и `desc`: тот же batch-load по `message_id`, `tags: []` если тегов нет. DTO не меняется — только порядок и набор `messages[]` (страница) и курсорные поля различаются. Подтверждено: backward-режим **не** влияет на состав полей письма.

### 5. Сосуществование режимов и валидация

Режимы взаимоисключающи **в пределах одного запроса**. Выбор — через ошибку, а не молчаливый приоритет (явность > неожиданность):

| Условие | HTTP | code | field |
| --- | --- | --- | --- |
| `order` ∉ {`asc`,`desc`} | 400 | `validation_error` | `order` |
| `before_id` передан при `order=asc` (или `order` не задан) | 400 | `validation_error` | `before_id` (message: «before_id допустим только при order=desc») |
| `since_id` передан при `order=desc` | 400 | `validation_error` | `since_id` (message: «since_id допустим только при order=asc») |
| `since_id` **и** `before_id` переданы одновременно | 400 | `validation_error` | `cursor` (message: «since_id и before_id взаимоисключающи») |
| `before_id < 1` / нечисловой | 400 | `validation_error` | `before_id` |
| `since_id < 0` / нечисловой | 400 | `validation_error` | `since_id` (как ADR-0029) |
| `limit` вне `1..200` | 400 | `validation_error` | `limit` (как ADR-0029) |

Остальные коды — как ADR-0029 §3: `401 not_authenticated` (нет/неверный ключ или фича выключена), `429 rate_limited` (+`Retry-After`). `403`/`404` не используются (нет ресурсной адресации/scope'ов). Все `400` — envelope `{error:{code,message,details:{errors:[...]}}}`.

### 6. Auth / rate-limit / CSRF / визибилити — без изменений

- Auth-флоу тот же (ADR-0029 §4): `consume(LIMIT_EXTERNAL_API, key=client_ip)` **до** работы с ключом → `X-API-Key`/`Bearer` (`compare_digest`) → `external_api_enabled` → валидация query → выборка. `order`/`before_id` валидируются на шаге query-валидации (после auth) — как `since_id`/`limit`.
- Rate-limit — **тот же** `LIMIT_EXTERNAL_API` (`EXTERNAL_API_RATE_LIMIT_PER_MINUTE`, default `120/min` per IP). Backward — тоже read, тот же бюджет; **нового лимита нет**.
- CSRF exempt, redact-list (`EXTERNAL_API_KEY`/`X-API-Key`/`Authorization`), super_admin visibility, canonical-дедуп — идентичны ADR-0029.
- **Новых env / feature-флагов нет.** Backward-режим доступен всегда, когда `external_api_enabled` (read всегда включён при непустом ключе — в отличие от write-gate `EXTERNAL_REPLY_ENABLED` ADR-0035, которого это ADR не касается).

### 7. Совместимость (не ломаем)

- **ADR-0029 forward** — байт-в-байт: `order` default `asc`, `since_id`/`limit` семантика и ответ (`next_since_id`/`has_more`) не изменены. Существующий B2B-клиент, не передающий `order`, работает как раньше.
- **ADR-0035 reply** — не затрагивается (backward — read; write-поверхность и threading `In-Reply-To`/`References` не меняются).
- **Read-only модель ADR-0029** — сохранена: backward — только `GET`, только поля письма, никаких write/CRUD.
- **Версионирование (ADR-0029 §6):** изменение **аддитивно и non-breaking** — новый optional-параметр `order`/`before_id` + новое курсорное поле `next_before_id`, появляющееся **только** в новом `desc`-ответе. Дефолт (`asc`/без параметров) неизменен. Путь `/api/external/` (неявная v1) **не** бампается; нового ADR-пути не требуется.

Это ADR **снимает** зафиксированное в ADR-0029 ограничение «нет newest-first» (Alternatives 2 ADR-0029 отвергал `internal_date DESC`-курсор из-за silent-loss поздних писем — здесь `desc`-курсор по монотонному **`id`**, не по дате, поэтому та проблема не воспроизводится: поздно-пришедшее письмо имеет максимальный `id` и корректно попадает в latest-страницу).

## Consequences

### Positive
- **Newest-first лента без перебора истории** — `order=desc` без `before_id` отдаёт самые свежие N reverse-scan'ом по PK за `O(limit)`, без прохода от id=0.
- **Симметричный keyset по `id`** — backward-курсор по тому же монотонному `messages.id`, что и forward; без пропусков/дублей в пределах страницы. `id`-курсор (а не `internal_date`) сохраняет корректность для поздно-пришедших писем (снятие ограничения ADR-0029 §Alternatives 2).
- **Полная BC** — forward-клиент ADR-0029 не затронут; изменение аддитивно, без бампа версии/пути.
- **Ноль новой инфраструктуры** — без миграций БД, без новых индексов (PK покрывает `id DESC`), без новых env/флагов, без нового rate-limit'а. Тот же auth/redact/CSRF/visibility/canonical-дедуп.
- **Теги и DTO неизменны** — `ExternalMessageDTO`/`ExternalTagDTO` те же; отличается только порядок и курсорные поля.

### Negative / risks
- **Скользящее окно при активном ingest.** Пока потребитель листает `desc` вниз, приходят новые письма (id больше любого before_id) — они не попадают в уже листаемые страницы (ожидаемо для newest-first ленты). Чтобы увидеть новое сверху — потребитель перезапрашивает первую страницу (`order=desc` без `before_id`) или ведёт **вторую**, forward-подписку (`order=asc` с `since_id`=последний виденный max). Курсорной дыры это не создаёт (id-keyset строг). Документируется как контрактное ожидание для CRM-прокси.
- **id-gaps от retention** (ADR-0011, 30д) — как в ADR-0029, безвредны и для `id < before_id ORDER BY id DESC` (отсутствующие id просто пропускаются). Дошёл до конца истории → `has_more=false`/`next_before_id=null`.
- **Два курсорных поля в контракте** (`next_since_id` для asc, `next_before_id` для desc) — потребитель обязан читать поле, соответствующее выбранному `order`. Митигация: поля **не пересекаются** (каждое присутствует только в своём режиме) + явные 400 на смешение параметров.
- **Нагрузка** — `ORDER BY id DESC LIMIT ≤200` reverse-scan по PK + canonical-фильтр (покрыт существующим индексом) + tags-IN; профиль идентичен forward. Rate-limit `120/min` тот же. Фоновой работы на нашей стороне нет.

### Migration plan
1. **Без миграции БД** — новых таблиц/колонок/индексов нет (reverse-scan по существующему `messages.id` PK).
2. **`backend/app/external/router.py`**: добавить query-параметры `order: Literal["asc","desc"] = "asc"` и `before_id: int | None = None` (`ge=1`) к `GET /api/external/messages`.
3. **`backend/app/external/service.py`**: валидация сосуществования (§5) → выбор режима; в `desc` резолв latest vs older по наличию `before_id`; сборка ответа с `next_before_id`/`has_more` (desc) либо `next_since_id`/`has_more` (asc).
4. **`ExternalMessagesRepo`** (`backend/app/repositories/…`): методы для backward — `list_latest(*, mail_account_ids, limit)` и `list_before_id(*, mail_account_ids, before_id, limit)` (`ORDER BY id DESC`), либо параметризация существующего `list_since_id` направлением. Tags-batch переиспользуется.
5. **`backend/app/external/schemas.py`**: две формы страницы (или одна с optional-курсорами) — `ExternalMessagesPage` (asc: `next_since_id`) и `ExternalMessagesPageDesc` (desc: `next_before_id | null`); `ExternalMessageDTO`/`ExternalTagDTO`/`ExternalMailAccountDTO` — **без изменений**.
6. **DevOps / env** — изменений нет (тот же `EXTERNAL_API_KEY` / `EXTERNAL_API_RATE_LIMIT_PER_MINUTE`).

## Alternatives considered

1. **Триггер backward только по наличию `before_id`, без `order`** — отвергнуто. Первая latest-страница не имеет курсора; presence-of-`before_id` не отличает «дай самые свежие» от «продолжи», а «нет параметров» в ADR-0029 уже = forward-from-0 (менять этот дефолт — breaking). `order=desc` явно включает latest-first и задаёт направление.
2. **`internal_date DESC`-курсор для newest-first** — отвергнуто (то же обоснование, что ADR-0029 Alternatives 2): поздно-пришедшее письмо имеет старую дату → выпадает на пройденную страницу → silent loss. Backward-курсор берётся по монотонному **`id`**, а не по дате — проблемы нет.
3. **Отдельный endpoint `GET /api/external/messages/latest`** — отвергнуто. Дублирует auth/rate-limit/visibility/canonical/DTO-сборку ради смены сортировки; `order` на том же endpoint компактнее и версионно-совместимее.
4. **Молчаливый приоритет при передаче обоих курсоров** (напр. `before_id` важнее `since_id`) — отвергнуто в пользу `400 validation_error`: явная ошибка предотвращает тихую подмену намерения потребителя.
5. **`page`/`offset`-пагинация назад** — отвергнуто: offset деградирует на глубине и ломается при вставках/retention-удалениях; keyset по `id` стабилен.

## Open questions

Нет. Контракт закрыт полностью (параметры, режимы, курсоры, ошибки, совместимость определены выше).
