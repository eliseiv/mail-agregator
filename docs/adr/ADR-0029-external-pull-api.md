# ADR-0029 — External PULL-API для передачи писем стороннему сервису

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-06-11 |
| Заменяет / отменён | — (не отменяет ADR-0023 outbound webhooks; это **отдельный** канал — pull, а не push) |

## Context

ADR-0023 даёт **push**-канал (мы инициируем POST в receiver по письмам с тегами одной команды). Появился новый класс интеграции: **B2B-партнёр** хочет инкрементально **сам забирать ВСЕ письма системы** (а не только tagged, и не per-team), на своей стороне, в своём темпе, без необходимости поднимать вечно-доступный HTTPS-receiver и без нашего push-rate-limit'а.

Пользователь явно зафиксировал требования (НЕ подлежат изменению в этом ADR):

1. **PULL-модель.** Внешний сервис периодически опрашивает наш `GET /api/external/messages`. Мы ничего не push'им. Внешний сервис хранит у себя `last_id` (курсор) и передаёт его в `since_id`.
2. **Объём — ВСЕ письма системы** (super_admin visibility: все ящики всех команд). Не per-team, не tagged-only.
3. **Тело — ПОЛНОЕ и СЫРОЕ**: `body_text` + `body_html` как хранятся в БД (`messages.body_text`/`body_html`), **БЕЗ** render-нормализации `collapse_blank_lines_*` (ADR-0022 §2.10). Контракт — стабильный raw stored, без UI-логики.
4. **Вложения НЕ передаются** (Q-0029-1, closed=no).
5. **`to_addrs`/`cc_addrs` ВКЛЮЧЕНЫ** (Q-0029-2, closed=included).
6. **Auth — статический `EXTERNAL_API_KEY`** (constant-time `secrets.compare_digest`), заголовок `X-API-Key` **или** `Authorization: Bearer <key>`.

Существующая инфраструктура, переиспользуемая as-is:
- `secrets.compare_digest` — тот же constant-time паттерн, что у Telegram webhook-secret (ADR-0027 §1 §10, `06-security.md` §1.12).
- `slowapi` rate-limit (ADR-0009, `backend/app/rate_limit.py`) — добавляется именованный лимит `LIMIT_EXTERNAL_API`.
- structlog redact-list (ADR-0014, `06-security.md`) — `EXTERNAL_API_KEY` / `X-API-Key` / `Authorization` добавляются в redact.
- CSRF middleware (ADR-0010) — endpoint **csrf-exempt** (нет cookie-сессии; это API-key канал, GET-only).
- `messages.id BIGSERIAL` (ADR-0008, `03-data-model.md` строка 354) — монотонный insert-order, **готовый keyset-курсор** без новых колонок/индексов (PK уже индекс).
- `MailAccountsRepo.list_canonical_account_ids` (round-18, `backend/app/repositories/mail_accounts.py`) — `MIN(id) per LOWER(email)`; **используется в read-path** для дедупликации дубль-ящиков (см. §5 Visibility — почему). Тот же метод, что схлопывает дубли в super_admin inbox (`backend/app/messages/service.py`, round-18).

---

## Decision

### 1. Endpoint

```
GET /api/external/messages?since_id=<int≥0,default 0>&limit=<int 1..200,default 50>
```

| | |
| --- | --- |
| Auth | static `EXTERNAL_API_KEY`, заголовок `X-API-Key: <key>` **или** `Authorization: Bearer <key>`. Без cookie-сессии. |
| CSRF | exempt (нет cookie-auth; GET-only, read-only). |
| Rate-limit | `LIMIT_EXTERNAL_API` (env `EXTERNAL_API_RATE_LIMIT_PER_MINUTE`, `int`, default `120`, `ge=1`; запросов в минуту на IP). |
| Метод | только `GET`. |
| Query `since_id` | `int ≥ 0`, default `0`. Семантика: `WHERE id > since_id`. |
| Query `limit` | `int`, `1..200`, default `50`. Cap 200 (hard). `limit>200` → `400 validation_error`; `limit<1` → `400`. |

**Семантика выборки (keyset по `messages.id`):**

```sql
SELECT m.id, m.subject, m.internal_date, m.from_addr, m.from_name,
       m.to_addrs, m.cc_addrs, m.body_text, m.body_html,
       m.body_present, m.body_truncated,
       ma.id AS mail_account_id, ma.email AS mail_account_email,
       ma.display_name AS mail_account_display_name
FROM messages m
JOIN mail_accounts ma ON ma.id = m.mail_account_id
WHERE m.id > :since_id
  AND m.mail_account_id IN (:canonical_account_ids)   -- canonical-дедуп дубль-ящиков (round-18)
ORDER BY m.id ASC
LIMIT :limit;
-- :canonical_account_ids := MailAccountsRepo.list_canonical_account_ids() — MIN(id) per LOWER(email).
-- tags подгружаются вторым запросом IN (:message_ids) и группируются в Python
-- (или LEFT JOIN LATERAL json_agg — на усмотрение backend; контракт — массив tags на письмо).
```

`messages.id BIGSERIAL` — строго монотонный insert-order: keyset `id > since_id ORDER BY id ASC` гарантирует **отсутствие пропусков и дублей курсора** между последовательными страницами. Внешний сервис хранит `last_id` и на каждой итерации шлёт `since_id=last_id`, обновляя его на `next_since_id` из ответа.

**Дедуп дубль-ящиков.** Фильтр `mail_account_id IN (canonical_account_ids)` отсеивает дублирующиеся подключения одного email двумя командами (два `mail_accounts`-ряда на один `LOWER(email)` — реальный кейс прод-инцидента, `muratdikenci042@outlook.com`). Каждый `mail_account` синкает один и тот же ящик независимо → получаются письма с **разными** `messages.id`, но одинаковым контентом. Без canonical-фильтра external pull отдал бы внешнему сервису **обе** копии. `list_canonical_account_ids()` оставляет ровно один (`MIN(id)`) аккаунт на email — внешний сервис получает **одну** копию, консистентно с тем, как super_admin inbox схлопывает дубли (round-18). `internal_date` / keyset-семантика не нарушены: фильтр применяется к `mail_account_id`, курсор по-прежнему монотонен по `id`.

### 2. Ответ 200

```json
{
  "messages": [
    {
      "id": 12345,
      "subject": "Тема письма",
      "internal_date": "2026-06-11T09:30:00Z",
      "from_addr": "sender@example.com",
      "from_name": "Sender Name",
      "to_addrs": "a@example.com, b@example.com",
      "cc_addrs": "c@example.com",
      "mail_account": { "id": 7, "email": "support@corp.example", "display_name": "Support" },
      "body_text": "<сырое stored body_text, без collapse-нормализации>",
      "body_html": "<сырое stored body_html или null>",
      "body_present": true,
      "body_truncated": false,
      "tags": [ { "id": 7, "name": "Urgent", "color": "#dc2626" } ]
    }
  ],
  "next_since_id": 12345,
  "has_more": true
}
```

- `next_since_id` = `id` **последнего** элемента `messages[]` (= `max(id)`, т.к. ORDER BY id ASC). Внешний сервис сохраняет его как новый `last_id`.
- `has_more` = `len(messages) == limit` (эвристика «возможно есть ещё»; следующий запрос с `next_since_id` подтвердит). При `len < limit` → `false`.
- **Пустой результат** (нет писем с `id > since_id`): `{ "messages": [], "next_since_id": <входной since_id>, "has_more": false }`. Курсор не двигается.
- `to_addrs` — всегда строка (БД `NOT NULL DEFAULT ''`); `cc_addrs` — `string | null` (БД nullable); `from_name`, `subject`, `body_html`, `mail_account.display_name` — nullable (как в БД).
- `body_present=false` ⇒ `body_text=""` и `body_html=null` (письмо без text/plain и text/html part — ADR-0012); поля всё равно присутствуют в JSON.
- **Поля письма и только они.** Никаких паролей/токенов/secret'ов/`encrypted_password`/`oauth_*`/IMAP-UID/internal owner-структур. `mail_account` отдаёт **только** `id`/`email`/`display_name`.

### 3. Ошибки (envelope `{error:{code,message}}` — `04-api-contracts.md` §«Унифицированный формат ошибок»)

| HTTP | code | Когда |
| --- | --- | --- |
| 401 | `not_authenticated` | Нет ключа / неверный ключ / фича выключена (`EXTERNAL_API_KEY` пуст). **Неперечислимо**: «выключено» неотличимо от «неверный ключ» — конфиг не раскрывается. |
| 429 | `rate_limited` | Превышен `LIMIT_EXTERNAL_API`. Заголовок `Retry-After` присутствует. |
| 400 | `validation_error` | `since_id<0` / нечисловой / `limit` вне `1..200`. `details.errors[]`. |

> 404/403 не используются: канал не имеет ресурсной адресации и пользовательских scope'ов — только аутентификация ключа и валидация query.

### 4. Auth-флоу (порядок строгий)

```
1. consume(LIMIT_EXTERNAL_API, key=client_ip)   # ПЕРВЫМ — anti-flood ДО любой работы с ключом.
                                                  #   429 rate_limited (+Retry-After) при исчерпании.
2. key = request.headers.get("X-API-Key") or _bearer(request.headers.get("Authorization"))
   # _bearer: "Bearer <token>" → token; иначе None. X-API-Key имеет приоритет.
3. if not settings.external_api_enabled:          # external_api_enabled := bool(EXTERNAL_API_KEY) (непустой)
       raise NotAuthenticatedError                # 401 — фича выключена, НЕ раскрываем это (как «неверный ключ»)
4. if key is None or not secrets.compare_digest(key, settings.EXTERNAL_API_KEY):
       raise NotAuthenticatedError                # 401 not_authenticated. compare_digest — constant-time.
5. validate query (since_id, limit) → 400 при нарушении
6. canonical_ids = MailAccountsRepo.list_canonical_account_ids()   # MIN(id) per LOWER(email) — дедуп дубль-ящиков
   rows = ExternalMessagesRepo.list_since_id(mail_account_ids=canonical_ids, since_id=since_id, limit=limit)
7. 200 {messages, next_since_id, has_more}
```

- **Логирование БЕЗ ключа.** structlog-событие пишет `client_ip`, `since_id`, `limit`, `returned_count` — **никогда** значение ключа/заголовка. `EXTERNAL_API_KEY`/`X-API-Key`/`Authorization` — в redact-list.
- `compare_digest` сравнивает только при наличии ключа; при `key is None` — сразу 401 (без сравнения), но это не открывает timing-enumeration (длина ключа фиксирована env, не вводится атакующим).

### 5. Visibility — super_admin scope (все ящики, canonical-дедуп), без сессии

Endpoint **не** имеет пользовательской сессии и `VisibilityScope`: API-key = доверенный сервис, видит **все** письма всех команд (эквивалент super_admin). Фильтрации по `group_id` **нет** (намеренно: «все письма системы»). **Единственный** фильтр на read-path — canonical-дедуп дубль-ящиков:

```
WHERE m.id > :since_id
  AND m.mail_account_id IN (:canonical_account_ids)   -- list_canonical_account_ids() = MIN(id) per LOWER(email)
ORDER BY m.id ASC
LIMIT :limit
```

**Почему canonical, а не «все mail_account_id».** Один и тот же email может быть подключён **двумя командами** независимо (два `mail_accounts`-ряда, один `LOWER(email)`; прод-инцидент `muratdikenci042@outlook.com`). Каждый `mail_account` синкает один ящик отдельно → одинаковый контент письма попадает в БД **дважды** под разными `messages.id`. Без фильтра external pull отдал бы внешнему сервису **обе** копии (контентные дубли, разные id — keyset их не схлопывает, т.к. дедуп идёт по `id`, а id разные). `MailAccountsRepo.list_canonical_account_ids()` оставляет **один** канонический аккаунт (`MIN(id)`) на каждый `LOWER(email)` → внешний сервис получает **ровно одну** копию каждого письма-дубля.

**Консистентность.** Это тот же механизм, что схлопывает дубли в **super_admin inbox UI** (round-18, `backend/app/messages/service.py` → `list_canonical_account_ids()`). External pull и super_admin inbox показывают **одинаковый** дедуплицированный набор писем — единая семантика «все письма системы без дублей дубль-ящиков».

**Курсорная семантика сохранена.** Фильтр применяется к `mail_account_id`; keyset по-прежнему монотонен по `messages.id` (id ASC). Письма из не-канонических аккаунтов просто отсутствуют в выборке — это не дыры курсора (курсор движется по `id` всех выданных писем), а консистентное исключение дублей. Поздно-пришедшее письмо канонического аккаунта по-прежнему попадает в хвост по `id` (Alternatives 2 в силе).

**Индексы достаточны** (подтверждено по `03-data-model.md`):
- `messages` PK `id` (BIGSERIAL) — keyset `id > since_id ORDER BY id ASC`.
- `INDEX (mail_account_id, internal_date DESC)` (`03-data-model.md:378`) — составной с ведущим `mail_account_id`, покрывает `mail_account_id IN (canonical_ids)`.
- Объём `canonical_ids` — `≤ сотни` (число уникальных email-ящиков системы; `≤ 500` ящиков по оценке `03-data-model.md:720`). `IN (...)` с сотнями элементов + keyset по PK — приемлемо. Планировщик использует либо `id`-PK scan с post-filter по `mail_account_id`, либо `mail_account_id`-индекс + сортировку по `id`; при `LIMIT ≤ 200` стоимость ограничена. Дополнительных индексов **не требуется**.

### 6. Схема — отдельный `ExternalMessageDTO`

Вводится **новый** `ExternalMessageDTO` (+ вложенные `ExternalMailAccountDTO`, `ExternalTagDTO`, `ExternalMessagesPage`) в `backend/app/external/schemas.py`, **отдельный** от UI-`MessageDetail`/`MessageService.get` (модуль 10). Причины:
- UI-DTO применяет render-нормализацию `collapse_blank_lines_*` (модуль 10, `05-modules.md:948-951`) — внешний контракт обязан отдавать **сырое** тело. Связывание сломало бы либо UI, либо контракт.
- Внешний контракт — **стабильный, версионируемый**, эволюционирует независимо от внутренних UI-полей.

**Версионирование:**
- Текущий путь — `/api/external/` (неявная v1). Поля добавляются **аддитивно** (новое optional-поле — без breaking change, без ADR).
- Любое **breaking**-изменение (удаление/переименование/смена типа поля, смена курсорной семантики) → новый путь `/api/external/v1/` (явная версия) + новый ADR (`ADR-0029.1+`). Старый путь поддерживается на период миграции.

### 7. Raw body без collapse

`body_text`/`body_html` отдаются **как хранятся** в `messages` (то, что записал worker при ingest, ADR-0012). `collapse_blank_lines_text`/`collapse_blank_lines_html`/`collapse_blank_lines_tg` (ADR-0022 §2.10) — это **render-time** UI-нормализация; в external-API **не вызывается**. `body_truncated=true` означает, что worker усёк тело при ingest до 1 MiB (ADR-0012) — внешний сервис получает усечённое тело и знает об этом по флагу.

---

## Consequences

### Positive
- **Инкрементальный pull без пропусков/дублей курсора** — keyset по монотонному `messages.id BIGSERIAL`; внешний сервис держит один `last_id`. Без новых колонок/индексов (PK + существующий `(mail_account_id, internal_date DESC)` достаточны).
- **Дубли дубль-ящиков схлопнуты** — фильтр `mail_account_id IN (list_canonical_account_ids())` исключает контентные дубли, возникающие когда один email подключён двумя командами (разные `messages.id`, одинаковый контент). Внешний сервис получает **одну** копию каждого письма. **Консистентно с super_admin inbox** (тот же `list_canonical_account_ids()`, round-18) — единая семантика «все письма системы без дублей».
- **Минимальная поверхность** — один GET-endpoint, read-only, статический ключ. Нет CRUD, нет push-инфраструктуры (очередей/диспатчеров/recovery), нет состояния на нашей стороне (курсор у клиента).
- **Изоляция контракта** — отдельный `ExternalMessageDTO`, версионируемый, не связан с UI-рендером.
- **Простая интеграция** — `X-API-Key`/`Bearer`, без OAuth/JWT/подписей; подходит для no-code и self-hosted клиентов.
- **Опциональность** — пустой `EXTERNAL_API_KEY` ⇒ фича выключена, endpoint отдаёт 401 неперечислимо (нулевой attack-surface, если не используется).

### Negative / risks
- **Single-factor static key** — компрометация ключа = доступ ко **всем** письмам системы (super_admin visibility). Митигация: ключ хранится в env (`chmod 600 .env`), в redact-list (не в логах/коде), ротация — смена `EXTERNAL_API_KEY` + рестарт `api` (`docker compose up -d --force-recreate api`); внешний сервис обновляет ключ синхронно. Нет per-client-ключей в MVP (один ключ = один партнёр). Если потребуется несколько партнёров / отзыв отдельных ключей — отдельный ADR (таблица `external_api_keys` с hash). Нет replay-resistance/HMAC — accepted risk (TLS защищает транзит; ключ статичен — см. Alternatives 3).
- **Доверенный сервис видит ВСЕ письма** (все команды). Это явное требование (super_admin visibility). Партнёр — доверенная сторона; ответственность за защиту ключа и данных на его стороне.
- **id-gaps от retention** — retention-cleanup (ADR-0011, 30 дней) физически удаляет старые `messages` → в последовательности `id` появляются **дыры**. Это **безвредно** для keyset `id > since_id` (пропуск отсутствующих id, не пропуск новых). Требование к внешнему сервису: **поллить чаще окна ретенции** (≪ 30 дней; практически — минуты/часы), иначе письма успеют удалиться до забора. Зафиксировано как контрактное ожидание (см. Edge cases + Q-0029 не открывается — это документируемое ограничение, не вопрос).
- **Поздно-пришедшее письмо** (старая `internal_date`, новый `id`) корректно отдаётся keyset'ом по `id` (новый id > курсор) — **это и есть причина выбора `since_id` вместо cursor'а по `internal_date`** (см. Alternatives 2).
- **Вложения не передаются** (Q-0029-1). Внешнему сервису доступны только метаданные письма + тело. Если потребуется — отдельный ADR (аддитивное поле `attachments[]` с метаданными или отдельный endpoint).
- **Нагрузка** — read-only keyset по PK `id` + фильтр `mail_account_id IN (canonical_ids)` (покрыт `INDEX (mail_account_id, internal_date DESC)`, `03-data-model.md:378`) + tags-IN; существующих индексов достаточно, новых не нужно. `canonical_ids` — `≤ сотни` (число уникальных email-ящиков). `LIMIT≤200` + rate-limit `120/min` ограничивают пик. На нашей стороне нет фоновой работы.

### Migration plan
1. **Без миграции БД** — новых таблиц/колонок/индексов нет (keyset по существующему `messages.id` PK).
2. **`shared/config.py`**: добавить `EXTERNAL_API_KEY: str = ""` + derived `external_api_enabled := bool(EXTERNAL_API_KEY)` + `EXTERNAL_API_RATE_LIMIT_PER_MINUTE: int = 120` (`ge=1`). Добавить `EXTERNAL_API_KEY`/`X-API-Key`/`Authorization` в structlog redact-list (`shared/logging.py`).
3. **`backend/app/external/`**: `router.py` (`GET /api/external/messages`), `service.py` (auth-флоу + `MailAccountsRepo.list_canonical_account_ids()` для canonical-дедупа + сборка page), `schemas.py` (`ExternalMessageDTO` и пр.).
4. **`backend/app/repositories/messages.py`** (или новый `ExternalMessagesRepo`): `list_since_id(*, mail_account_ids: list[int], since_id: int, limit: int)` — keyset `WHERE m.id > since_id AND m.mail_account_id IN (mail_account_ids) ORDER BY m.id ASC LIMIT limit` + tags-batch. `mail_account_ids` приходит из `MailAccountsRepo.list_canonical_account_ids()` (canonical-дедуп; вызывается в service-слое до repo). Empty `mail_account_ids` (нет ящиков вообще) ⇒ пустой результат без запроса.
5. **`backend/app/rate_limit.py`**: `LIMIT_EXTERNAL_API` (cap = `settings.EXTERNAL_API_RATE_LIMIT_PER_MINUTE`, `int`, default `120`, override на consume-time, паттерн `TG_SEND_PER_CHAT_PER_MINUTE`).
6. **`backend/app/main.py`**: `include_router(external.router)`; добавить путь в CSRF-exempt allowlist (как Telegram webhooks).
7. **DevOps**: env `EXTERNAL_API_KEY` опциональна (пусто = выключено); генерация `openssl rand -hex 32`. Передаётся **только** в `api` (worker не использует).

---

## Alternatives considered

1. **Reuse push-webhook (ADR-0023)** — отвергнуто. ADR-0023 — per-team + tagged-only + push. Требование: ВСЕ письма + pull. Семантика принципиально иная; натягивание сломало бы оба канала.

2. **Cursor base64 по inbox-порядку `(internal_date DESC, id DESC)`** — отвергнуто (ключевое обоснование).
   - Inbox UI листает по `internal_date DESC`. Но поздно-пришедшее письмо имеет **старую** `internal_date` и **новый** `id`: при курсоре по убыванию даты оно встанет **на уже пройденную** страницу (между старыми датами) → внешний сервис, ушедший вперёд по дате-курсору, его **пропустит** (silent data loss).
   - **Решение:** keyset `id ASC` — каждое новое письмо имеет максимальный `id` > любого выданного курсора, попадает в хвост → гарантия отсутствия пропусков. Retention-удаления дают безвредные id-gaps (см. Consequences).

3. **OAuth2 / JWT вместо статического ключа** — отвергнуто на MVP.
   - Pro: отзыв, scoping, истечение, audience.
   - Contra: сложнее интеграция (token endpoint, refresh, ротация) — избыточно для одного доверенного B2B-партнёра. Пользователь явно выбрал static key.
   - **Решение:** static `EXTERNAL_API_KEY` + `compare_digest`. Multi-key/отзыв — отдельный ADR при появлении нескольких партнёров.

4. **Reuse UI `MessageDetail`/`MessageService.get`** — отвергнуто.
   - UI-DTO применяет `collapse_blank_lines_*` (render-нормализация) и заточен под web/TG-рендер. Контракт требует **сырое** тело и независимую эволюцию полей.
   - **Решение:** отдельный `ExternalMessageDTO`.

5. **Push (мы инициируем доставку) вместо pull** — отвергнуто.
   - Требует от партнёра вечно-доступного HTTPS-receiver + перекладывает retry/delivery-tracking на нас (как ADR-0023). Партнёр хочет забирать сам в своём темпе.
   - **Решение:** pull. Внешний сервис держит курсор и поллит.

---

## Security

- **Хранение ключа:** `EXTERNAL_API_KEY` — в env (`.env`, `chmod 600`), генерация `openssl rand -hex 32` (256 бит). **Опциональна**: пусто ⇒ `external_api_enabled=false` ⇒ endpoint отдаёт 401 (фича выключена). Передаётся только в `api`-контейнер.
- **Redact:** `EXTERNAL_API_KEY`, `X-API-Key`, `Authorization` — в structlog redact-list (рядом с `MAIL_ENCRYPTION_KEY`/`TELEGRAM_BOT_TOKEN`). Логи external-API не содержат значение ключа ни в каком виде.
- **Constant-time compare:** `secrets.compare_digest(key, EXTERNAL_API_KEY)` — защита от timing-атак на сравнение.
- **401 unenumerable:** «фича выключена» (`EXTERNAL_API_KEY` пуст) и «неверный ключ» возвращают **одинаковый** `401 not_authenticated` — атакующий не может по ответу определить, включена ли фича / какой длины ключ. Конфиг не раскрывается.
- **Read-only scope:** endpoint только `GET`, отдаёт **только** поля письма (`id`/`subject`/`internal_date`/`from_*`/`to_addrs`/`cc_addrs`/`mail_account.{id,email,display_name}`/`body_*`/`tags`). Никаких паролей, OAuth-токенов, IMAP-UID, `encrypted_password`, secret'ов, owner-структур.
- **Anti-flood:** `LIMIT_EXTERNAL_API` consume **до** работы с ключом — защита от brute-force ключа и DoS.
- **Ротация:** смена `EXTERNAL_API_KEY` в env → `docker compose up -d --force-recreate api`; партнёр обновляет ключ синхронно. Старый ключ немедленно недействителен (нет grace-периода в MVP).
- **TLS:** канал доступен только через nginx :443 (как остальной API); транзит зашифрован.

## Edge cases

| Случай | Поведение |
| --- | --- |
| `messages[]` пуст (нет `id > since_id`) | `{messages:[], next_since_id:<входной since_id>, has_more:false}`. Курсор не двигается. |
| `since_id` «в будущем» (> max(id)) | Пустой результат (как выше). Не ошибка — keyset просто ничего не находит. |
| `since_id=0` (default) | Отдаёт с самого начала (все письма, начиная с минимального существующего id). |
| `limit` cap | `limit>200` → `400 validation_error`; `limit<1` → `400`. `limit` не передан → `50`. |
| Удалённый ящик (`mail_accounts` удалён) | Письма каскадно удалены (`messages.mail_account_id` FK ON DELETE CASCADE) → их `id` исчезают → безвредные id-gaps. JOIN на `mail_accounts` всегда находит аккаунт для существующего письма. |
| **Дубль-ящик** (один email подключён двумя командами → два `mail_accounts`, один `LOWER(email)`) | Canonical-фильтр `mail_account_id IN (list_canonical_account_ids())` оставляет письма **только** канонического (`MIN(id)`) аккаунта → внешний сервис получает **одну** копию каждого письма, не обе. Консистентно с super_admin inbox (round-18). |
| Письмо без тегов | `tags: []` (пустой массив). |
| `body_present=false` | `body_text=""`, `body_html=null`, поля присутствуют. |
| `cc_addrs` отсутствует в письме | `cc_addrs: null` (БД nullable). `to_addrs` — всегда строка (БД `NOT NULL DEFAULT ''`). |
| id-gap от retention | `id > since_id ORDER BY id ASC` пропускает отсутствующие id без ошибки. Контракт: внешний сервис поллит чаще 30-дн окна ретенции. |
| Невалидный `since_id` (отрицательный/нечисловой) | `400 validation_error`. |

## Open questions

| ID | Вопрос | Решение |
| --- | --- | --- |
| Q-0029-1 | Передавать ли вложения? | **closed = no.** Вложения не передаются (только метаданные письма + тело). Расширение — отдельный ADR при явном запросе. |
| Q-0029-2 | Включать ли `to_addrs`/`cc_addrs`? | **closed = included.** Оба поля в DTO (`to_addrs` всегда строка, `cc_addrs` nullable). |
