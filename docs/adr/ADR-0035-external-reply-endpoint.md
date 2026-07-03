# ADR-0035 — External reply-endpoint (единственный scoped write во внешнем API)

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-07-03 |
| Заменяет / отменён | — (extends [ADR-0029](./ADR-0029-external-pull-api.md); read-контракт ADR-0029 **не отменяется** и остаётся источником истины для pull) |

## Context

[ADR-0029](./ADR-0029-external-pull-api.md) ввёл **READ-ONLY** внешний API: единственный `GET /api/external/messages` (инкрементальный keyset-pull всех писем системы по `X-API-Key`). Явно зафиксировано (ADR-0029 §6 «Версионирование», Consequences): любое расширение поверхности — через **новый ADR**.

Появилось новое требование: доверенный B2B-партнёр (CRM), который уже забирает письма pull-каналом, должен уметь **ответить на конкретное входящее письмо** программно — без человека в UI, тем же `X-API-Key`. CRM проксирует запрос: получает `message.id` из pull-фида, формирует ответ и шлёт его нам; мы отправляем реальное письмо через SMTP того ящика, на который письмо пришло.

Отправка сегодня существует **только** как внутренний сессионный `POST /api/messages/send` (`SendService.send`, `backend/app/send/`): произвольный `from_account_id`, произвольные `to/cc/bcc`, threading по `in_reply_to_message_id`, visibility-проверка по сессии пользователя (`VisibilityScope`). Внешнему каналу этот endpoint недоступен (нет cookie-сессии) и **не должен** быть доступен as-is — он даёт произвольную отправку.

**Ключевой конфликт с ADR-0029.** ADR-0029 намеренно read-only: компрометация ключа = чтение всех писем. Добавление write расширяет blast-radius (компрометация ключа = ещё и отправка писем от имени ящиков системы). Задача ADR — добавить write **минимально возможной поверхностью**, чтобы модель ADR-0029 не разрушилась.

Существующая инфраструктура, переиспользуемая as-is (без дублирования):
- Auth-флоу внешнего API (`backend/app/external/router.py`): rate-limit-first → извлечение ключа (`X-API-Key` приоритет, иначе `Authorization: Bearer`) → `external_api_enabled` → constant-time `_api_key_matches` (`secrets.compare_digest`). CSRF-exempt.
- Canonical-дедуп scope (`MailAccountsRepo.list_canonical_account_ids`, `MIN(id)` per `LOWER(email)`) — тот же scope, что видит pull (ADR-0029 §5).
- `MessagesRepo.get_for_user_ids(message_id, mail_account_ids)` — visibility-aware выборка письма по id в наборе аккаунтов; `None` при отсутствии/вне scope.
- SMTP/MIME-ядро отправки: `SendService`, `smtp_send_message` (password + XOAUTH2, `assert_public_host`, fail-fast timeout, ADR-0034 §5), `build_mime`/`generate_message_id` (`backend/app/send/mime.py`), threading-резолвинг `message_id_header`/`refs_header`, best-effort IMAP-append в Sent. **Ничего из этого не дублируется** — reply-endpoint переиспользует то же ядро.
- Rate-limit-фреймворк (`backend/app/rate_limit.py`, `Limit`/`consume`, override capacity из settings — паттерн `LIMIT_EXTERNAL_API`/`LIMIT_WEBHOOK_TEST`).
- Унифицированный формат ошибок `{error:{code,message,field,details}}` (`04-api-contracts.md` §«Унифицированный формат ошибок»).

---

## Decision

Добавить **ровно один** write-endpoint во внешний API — ответ на **существующее** письмо:

```
POST /api/external/messages/{id}/reply
```

Поверхность узкая по трём осям (это и есть обоснование «не рушит ADR-0029»):
1. **Только ответ на существующее письмо.** Нет создания «с нуля», нет CRUD, нет произвольной отправки. Тело запроса не содержит `from_account_id`.
2. **Отправитель не выбирается.** `from` жёстко = `mail_account` **того самого** письма `{id}` (ящик, на который письмо пришло). Партнёр не может отправить от произвольного ящика системы.
3. **Тот же scope, что и чтение.** Письмо `{id}` резолвится в **том же** canonical-scope, что отдаёт pull (ADR-0029 §5). Ответить можно только на письмо, которое партнёр в принципе мог получить pull-каналом; всё остальное → `404`.

### 1. Feature-флаги — write **opt-in**, отдельно от read

| Флаг | Тип | Default | Смысл |
| --- | --- | --- | --- |
| `EXTERNAL_API_KEY` | `str` | `""` | Как в ADR-0029. Пусто ⇒ весь внешний API (read **и** reply) выключен. |
| `EXTERNAL_REPLY_ENABLED` | `bool` | `false` | **Отдельный** гейт записи. `false` (default) ⇒ reply-endpoint отвечает `403 forbidden` даже при валидном ключе. |

**Почему отдельный флаг (обязателен, не gold-plating).** Существующие read-only деплои ADR-0029 **уже** имеют непустой `EXTERNAL_API_KEY`. Если бы reply гейтился только этим ключом, апгрейд кода **молча** дал бы им write-способность (отправка писем) — недопустимое неявное расширение доверия. `EXTERNAL_REPLY_ENABLED=false` по умолчанию сохраняет read-only-постуру ADR-0029 как дефолт; запись — явный opt-in оператора.

### 2. Endpoint-контракт

| | |
| --- | --- |
| Метод / путь | `POST /api/external/messages/{id}/reply` (`{id}` — `int ≥ 1`, path-параметр; `messages.id` оригинала). |
| Auth | static `EXTERNAL_API_KEY`, заголовок `X-API-Key: <key>` **или** `Authorization: Bearer <key>` (`X-API-Key` приоритет), constant-time compare. Без cookie-сессии. |
| CSRF | **exempt** (нет cookie-auth; API-key канал). Тот же allowlist, что `GET /api/external/messages`. |
| Rate-limit | **отдельный** `LIMIT_EXTERNAL_REPLY` (env `EXTERNAL_REPLY_RATE_LIMIT_PER_MINUTE`, `int`, default `30`, `ge=1`; запросов в минуту на IP). Consume **первым**, до работы с ключом. Обоснование — §4. |
| Content-Type | `application/json`. Form-fallback (ADR-0015) **не** предоставляется (внешний машинный канал, не браузерная форма). |

**Тело запроса** — новая схема `ExternalReplyRequest` (`backend/app/external/schemas.py`):

| Поле | Тип | Обяз. | Валидация / default |
| --- | --- | --- | --- |
| `to` | `list[str] \| null` | нет | Если не передан/`null`/пустой ⇒ default `[<оригинал.from_addr>]` (ответ отправителю). Каждый элемент — e-mail-паттерн `send/schemas.py:_EMAIL_RE`. `max_length=100`. |
| `cc` | `list[str] \| null` | нет | default `null`. Тот же e-mail-паттерн. `max_length=100`. |
| `subject` | `str \| null` | нет | Если не передан/`null` ⇒ default `"Re: " + (<оригинал.subject> or "")`. `max_length=998` (RFC 5322 line). |
| `body` | `str` | **да** | Непустой: после `str.strip()` длина `≥ 1` (иначе `400 validation_error`). `max_length=1_048_576` (1 MiB, как `send`). Отправляется как `text/plain` (paritет с `SendService` — plain-only, ADR-0012). |

- **Нет** `from_account_id` (отправитель определяется сервером). **Нет** `bcc` (сужение поверхности — скрытые получатели во внешнем контракте не нужны; при необходимости — аддитивно новым ADR). **Нет** `in_reply_to_message_id` (threading задаётся сервером из `{id}`).
- `to`/`cc` — server-derived default `to` (`[оригинал.from_addr]`) **не** проходит через request-валидатор (это не пользовательский ввод); MIME/SMTP отвечают за его корректность (см. Edge cases).

### 3. Семантика (порядок строгий)

```
1. consume(LIMIT_EXTERNAL_REPLY, key="ip:"+client_ip)   # ПЕРВЫМ — anti-flood/anti-abuse. 429 (+Retry-After).
2. key = X-API-Key | Bearer(Authorization)
3. if not settings.external_api_enabled:  raise NotAuthenticatedError   # 401 — ключ/фича off, неперечислимо
4. if key is None or not _api_key_matches(key, EXTERNAL_API_KEY):  raise NotAuthenticatedError  # 401
5. if not settings.EXTERNAL_REPLY_ENABLED:  raise ForbiddenError        # 403 — запись выключена (ключ валиден)
6. FastAPI валидирует body (ExternalReplyRequest) → 400 validation_error при нарушении
7. res = SendService(db).send_external_reply(message_id={id}, to=..., cc=..., subject=..., body=...)
       # внутри: canonical scope → original по {id} (404 если нет/вне scope) →
       #         from = original.mail_account_id → reuse send-ядра (MIME/SMTP/append/persist), threading по {id}
8. 200 {sent_id: res.sent_id, smtp_message_id: res.smtp_message_id}
```

Шаги 1–4 **идентичны** `GET /api/external/messages` (ADR-0029 §4) — тот же auth-флоу; backend выносит его в общий хелпер (§Migration). Шаг 5 — новый write-гейт. Шаги 7-8 — делегирование в send-ядро.

**Резолвинг письма (внутри `send_external_reply`):**
```
canonical_ids = MailAccountsRepo.list_canonical_account_ids()        # тот же scope, что pull (ADR-0029 §5)
original       = MessagesRepo.get_for_user_ids(message_id={id}, mail_account_ids=canonical_ids)
if original is None:  raise NotFoundError            # 404 — письма нет ИЛИ оно вне canonical scope
from_account   = MailAccountsRepo.get_for_user_ids(canonical_ids, original.mail_account_id)   # ящик оригинала
```
- `from` = `original.mail_account_id` — ящик, **на который** пришло письмо. `to` = переданный `to` или `[original.from_addr]`. Threading: `in_reply_to_message_id = {id}` → send-ядро берёт `original.message_id_header`/`refs_header` и ставит MIME `In-Reply-To`/`References` (существующая логика `SendService.send` шаги 2/4).
- Non-canonical дубль-ящик: письмо, чей `mail_account_id` — не канонический (второй ящик того же email у другой команды), в pull **не отдаётся**, поэтому его `id` партнёр никогда не видел; reply на такой id → `404` (get_for_user_ids вернёт None). Консистентно с read.

### 4. Rate-limit — **отдельный** лимит (обоснование)

Reply получает **собственный** `LIMIT_EXTERNAL_REPLY` (30/мин на IP), а **не** переиспользует `LIMIT_EXTERNAL_API` (120/мин, read):

- **Разная стоимость и abuse-профиль.** Read — дешёвый keyset-SELECT. Reply **отправляет реальное письмо** по SMTP от ящика системы: стоимость (SMTP-коннект/AUTH/DATA + best-effort IMAP-append), spam/abuse-риск и репутация домена. Write-бюджет обязан быть **строго меньше** и **независим**.
- **Изоляция бюджетов.** Общий лимит означал бы: флуд reply «съедает» бюджет pull (партнёр перестаёт забирать письма), а активный polling «съедает» бюджет reply. Разделение исключает взаимное вытеснение.
- Окно 60s — как у read (единый per-minute-семантик). Default 30 < 120 (каждый вызов = один SMTP-send). Override capacity из settings на consume-time (паттерн `LIMIT_EXTERNAL_API`/`LIMIT_WEBHOOK_TEST`). Ключ — `ip:<client_ip>` (как read).

### 5. Ответ 200 — новая схема `ExternalReplyResponse`

```json
{ "sent_id": 987, "smtp_message_id": "<generated-msgid@postapp.store>" }
```

| Поле | Тип | Смысл |
| --- | --- | --- |
| `sent_id` | `int` | `sent_messages.id` персистнутого исходящего (`SendMessageResponse.sent_id`). |
| `smtp_message_id` | `str` | `Message-ID` отправленного письма (`SendMessageResponse.smtp_message_id`). |

**Подмножество** `SendMessageResponse{sent_id, smtp_message_id, appended_to_sent}`. `appended_to_sent` **намеренно НЕ** во внешнем контракте: best-effort IMAP-append в Sent — внутренняя деталь (может «не удаться» без влияния на факт отправки, ADR-0034 §5); партнёру не нужен. Внутренний `SendService` append по-прежнему делает; external DTO поле опускает. (Аддитивно вернуть можно новым ADR, если потребуется.)

### 6. Ошибки (envelope `{error:{code,message,...}}`)

| HTTP | code | Когда |
| --- | --- | --- |
| 200 | — | Письмо отправлено (SMTP успех). Тело — `ExternalReplyResponse`. |
| 400 | `validation_error` | Пустой/whitespace-only `body`; `body` > 1 MiB; невалидный e-mail в `to`/`cc`; `subject` > 998; `> 100` адресов. `details.errors[]` (обработчик `RequestValidationError`, `04-api-contracts.md`). |
| 401 | `not_authenticated` | Нет/неверный ключ **или** `EXTERNAL_API_KEY` пуст (фича off). **Неперечислимо** — как ADR-0029 §3. |
| 403 | `forbidden` | Ключ валиден, но `EXTERNAL_REPLY_ENABLED=false` (запись выключена). |
| 404 | `not_found` | Письма `{id}` нет **или** оно вне canonical scope (в т.ч. non-canonical дубль). Неотличимо — не раскрываем существование вне scope. |
| 409 | `oauth_reconsent_required` | Ящик оригинала — `oauth_outlook` с истёкшим consent (`oauth_needs_consent`); нужен reconnect оператором (проброс `OAuthReconsentRequiredError` из send-ядра). |
| 429 | `rate_limited` | Превышен `LIMIT_EXTERNAL_REPLY`. Заголовок `Retry-After` присутствует. |
| 502 | `smtp_failed` | SMTP-отправка не удалась (`SMTPSendFailedError` из send-ядра: коннект/AUTH/DATA/timeout). |

- **Логирование без ключа/тела письма.** structlog-событие `external_reply` пишет `client_ip`, `message_id`, `sent_id`, `smtp_message_id` — **никогда** значение ключа/заголовка (redact-list ADR-0029 §Security) и **не** тело письма. `mail_account_id` в событие **не** включается: он однозначно восстановим по `message_id` (`from = original.mail_account_id`, §3) и не добавляет ценности аудиту сверх `message_id` — набор оставлен минимальным.

### 7. Атрибуция `sent_messages`

Внешний контекст не имеет пользователя-автора. Строка `sent_messages` создаётся с `user_id = from_account.user_id` (**владелец ящика**) — FK-валидно и семантически корректно (ответ ушёл от имени ящика владельца). Это единственное отличие персиста от сессионного `send` (там `user_id` = автор-сессия, ADR-0019 §7.3).

---

## Consequences

### Positive
- **Минимальное расширение поверхности.** Один write-endpoint, только ответ на существующее письмо, отправитель не выбирается (= ящик оригинала), scope = read-scope. Модель ADR-0029 (узкий доверенный канал по одному ключу) сохранена: нет CRUD, нет произвольной отправки, нет выбора `from`.
- **Read-only остаётся дефолтом.** `EXTERNAL_REPLY_ENABLED=false` по умолчанию — существующие ADR-0029-деплои write **не** получают при апгрейде (нет неявного расширения доверия).
- **Zero-дублирование send-логики.** MIME (`build_mime`), SMTP (`smtp_send_message`, password+XOAUTH2, SSRF-recheck, timeout), threading, IMAP-append, персист — переиспользуются через общее ядро `SendService`. Новый код — тонкий scope-резолвинг + маппинг DTO.
- **Threading корректен.** `In-Reply-To`/`References` из оригинала — ответ склеивается в тред у получателя (та же логика, что UI-reply).
- **Изолированные бюджеты.** Отдельный write-rate-limit не даёт reply и pull вытеснять друг друга; write-бюджет строго меньше read.
- **Опциональность.** Оба флага off по умолчанию ⇒ нулевая write-поверхность, если не включено.

### Negative / risks
- **Расширение blast-radius ключа.** Компрометация `EXTERNAL_API_KEY` при `EXTERNAL_REPLY_ENABLED=true` = отправка писем от ящиков системы (не только чтение). Митигации: (а) write выключена по умолчанию; (б) отправитель ограничен ящиком оригинала — нельзя слать от произвольного ящика; (в) отдельный жёсткий rate-limit; (г) ротация ключа как ADR-0029. Per-client-ключей/scopes нет в MVP (один доверенный партнёр) — при появлении нескольких партнёров или потребности отзыва отдельных прав → отдельный ADR (таблица ключей с per-key capability).
- **Отправка от чужого имени внутри доверия.** Партнёр может ответить на любое видимое письмо от имени соответствующего ящика (super_admin scope, все команды). Это в рамках доверия B2B-партнёра (как и полный read в ADR-0029). Ответственность за ключ — на партнёре.
- **Best-effort Sent-append не виден партнёру** (`appended_to_sent` опущен). Если письмо не попало в IMAP «Sent» ящика — партнёр этого из ответа не узнает (send всё равно состоялся). Приемлемо: факт отправки = 200 + `smtp_message_id`.
- **Нет идемпотентности запроса.** Повтор `POST .../reply` (ретрай партнёра после таймаута) отправит письмо повторно. В MVP не решается (как внутренний `send`). Партнёр отвечает за дедуп на своей стороне. При необходимости exactly-once — отдельный ADR (idempotency-key).
- **Retention-гонка.** Оригинал мог быть удалён retention (ADR-0011, 30д) между pull и reply → `404`. Ожидаемо (как id-gaps в ADR-0029): партнёр отвечает своевременно.

### Migration plan
1. **Без миграции БД.** Новых таблиц/колонок/индексов нет (переиспользуются `messages`/`mail_accounts`/`sent_messages`).
2. **`shared/config.py`**: `EXTERNAL_REPLY_ENABLED: bool = False`; `EXTERNAL_REPLY_RATE_LIMIT_PER_MINUTE: int = 30` (`ge=1`). `EXTERNAL_API_KEY`/redact-list — уже есть (ADR-0029).
3. **`backend/app/rate_limit.py`**: `LIMIT_EXTERNAL_REPLY = Limit(name="external_reply", capacity=30, window_seconds=60)`; capacity override на consume-time из `settings.EXTERNAL_REPLY_RATE_LIMIT_PER_MINUTE` (паттерн `LIMIT_EXTERNAL_API`).
4. **`backend/app/external/router.py`**: вынести общий auth-хелпер (`_authenticate(request, ip)` — rate-limit-first опускается на сторону вызова, извлечение ключа + `external_api_enabled` + `_api_key_matches` → `NotAuthenticatedError`), переиспользовать в GET и в новом `POST /messages/{id}/reply`. Reply: `consume(LIMIT_EXTERNAL_REPLY)` → auth → `if not settings.EXTERNAL_REPLY_ENABLED: raise ForbiddenError` → делегировать в `SendService.send_external_reply` → маппинг в `ExternalReplyResponse`.
5. **`backend/app/external/schemas.py`**: `ExternalReplyRequest` (`to`/`cc`/`subject`/`body` — §2, e-mail-валидатор reuse из `send/schemas.py`), `ExternalReplyResponse{sent_id:int, smtp_message_id:str}`.
6. **`backend/app/send/service.py`**: новый `SendService.send_external_reply(*, message_id: int, to: list[str] | None, cc: list[str] | None, subject: str | None, body: str) -> SendMessageResponse`. Резолвит canonical scope + оригинал (404), `from = original.mail_account_id`, `to = to or [original.from_addr]`, `subject = subject if not None else "Re: "+(original.subject or "")`, `author user_id = from_account.user_id`, `in_reply_to_message_id = message_id`. **Выделить** post-visibility часть `send()` (шаги 2–7: threading-резолв → MIME → SMTP → IMAP-append → персист) в общий приватный хелпер и вызвать из обоих (`send` и `send_external_reply`), чтобы **не** дублировать MIME/SMTP. `send()` (сессионный, §7 user-check) не меняет поведение.
7. **CSRF-exempt — доп. кода не требуется.** CSRF-exempt allowlist живёт в `backend/app/csrf.py` (`EXEMPT_PATH_PREFIXES`), а не в `main.py`. Reply-путь **уже** покрыт существующим префиксом `/api/external/` (тем же, что `GET /api/external/messages`, ADR-0029 §1) — отдельного изменения не нужно, только обновить поясняющий комментарий к префиксу (упомянуть, что он покрывает и reply-write ADR-0035).
8. **DevOps**: env `EXTERNAL_REPLY_ENABLED` (default `false`), `EXTERNAL_REPLY_RATE_LIMIT_PER_MINUTE` (default `30`) — только в `api`-контейнер (worker не использует).

---

## Alternatives considered

1. **Переиспользовать `POST /api/messages/send` для внешнего канала** — отвергнуто. Даёт произвольную отправку (любой `from_account_id`, любые `to/cc/bcc`) и завязан на cookie-сессию/`VisibilityScope`. Открыть его ключом = широкая write-поверхность вопреки узкой модели ADR-0029.

2. **Гейтить reply только `EXTERNAL_API_KEY` (без `EXTERNAL_REPLY_ENABLED`)** — отвергнуто. Существующие read-only-деплои, уже имеющие ключ, получили бы write **молча** при апгрейде — неявное расширение доверия. Отдельный opt-in-флаг обязателен.

3. **Переиспользовать `LIMIT_EXTERNAL_API` (общий лимит read+reply)** — отвергнуто. Write дороже и abuse-опаснее read; общий бюджет допускает взаимное вытеснение (флуд reply глушит pull и наоборот). Отдельный `LIMIT_EXTERNAL_REPLY` (строго меньше, независим) — §4.

4. **Разрешить произвольный `from_account_id` в теле reply** — отвергнуто. Партнёр смог бы слать от любого ящика системы (широкий blast-radius). `from` = ящик оригинала жёстко — сужение поверхности и естественная reply-семантика.

5. **Идемпотентность через idempotency-key в MVP** — отложено. Внутренний `send` тоже не идемпотентен; добавление ключа/таблицы claim — оверинжиниринг для одного партнёра. Партнёр дедуплицирует на своей стороне. Exactly-once — отдельный ADR при явной потребности.

6. **Возвращать `appended_to_sent` во внешнем ответе** — отвергнуто. Best-effort IMAP-append — внутренняя деталь, не влияет на факт отправки; партнёру не нужна. Аддитивно новым ADR при запросе.

---

## Security

- **Write opt-in.** `EXTERNAL_REPLY_ENABLED=false` по умолчанию — write-поверхность нулевая, пока оператор явно не включит. Read-only-постура ADR-0029 сохраняется как дефолт.
- **Тот же ключ/constant-time.** `secrets.compare_digest(key, EXTERNAL_API_KEY)`; `X-API-Key`/`Bearer`; redact-list (`EXTERNAL_API_KEY`/`X-API-Key`/`Authorization`) — как ADR-0029. Ключ никогда не в логах.
- **Порядок проверок.** Rate-limit **до** работы с ключом (anti-bruteforce/anti-flood); затем ключ; затем write-гейт; затем валидация тела; затем отправка. `401 not_authenticated` неперечислим (нет ключа / неверный / фича off — одинаковый ответ).
- **Сужение отправителя.** `from` = ящик оригинала (не выбирается партнёром) — компрометация ключа не даёт слать от произвольного ящика.
- **Scope = read-scope.** Ответить можно только на письмо в canonical scope pull; вне scope → `404` (не раскрываем существование).
- **SSRF-recheck при отправке.** `assert_public_host(smtp_host)`/`(imap_host)` в send-ядре (как внутренний send) — external reply не обходит проверку.
- **Отдельный жёсткий rate-limit** write (30/мин, строго < read 120/мин) — ограничивает spam/abuse и стоимость SMTP.
- **CSRF-exempt** обоснован (нет cookie-auth; API-key канал) — как ADR-0029.
- **TLS** — доступ только через nginx :443 (как весь API).

## Edge cases

| Случай | Поведение |
| --- | --- |
| `{id}` не существует | `404 not_found`. |
| `{id}` вне canonical scope (non-canonical дубль-ящик) | `404 not_found` — партнёр этот id в pull не видел; существование не раскрывается. |
| `to` не передан/`null`/пустой | default `[<оригинал.from_addr>]` (ответ отправителю). |
| `subject` не передан/`null` | default `"Re: " + (<оригинал.subject> or "")` (если у оригинала `subject=null` → `"Re: "`). |
| `body` пустой/whitespace-only | `400 validation_error` (`field=body`). |
| `body` > 1 MiB / невалидный e-mail в `to`/`cc` / `subject` > 998 | `400 validation_error` (`details.errors[]`). |
| `оригинал.from_addr` невалиден как e-mail (server-derived default `to`) | Не проходит request-валидатор (не пользовательский ввод); MIME/SMTP отбивают → `502 smtp_failed`. |
| Ящик оригинала `oauth_outlook`, consent истёк | `409 oauth_reconsent_required` — reconnect оператором. |
| SMTP-отправка не удалась | `502 smtp_failed` (send **не** состоялся; `sent_messages` не создан). |
| IMAP-append в Sent не удался | Отправка **состоялась**: `200` c `sent_id`/`smtp_message_id` (best-effort append — внутренняя деталь, не в ответе). |
| `EXTERNAL_REPLY_ENABLED=false`, ключ валиден | `403 forbidden`. |
| `EXTERNAL_API_KEY` пуст (фича off) | `401 not_authenticated` (неперечислимо). |
| Повторный `POST .../reply` (ретрай) | Письмо уходит повторно (нет идемпотентности в MVP; партнёр дедуплицирует). |
| Оригинал удалён retention между pull и reply | `404 not_found` (как id-gaps ADR-0029; партнёр отвечает своевременно). |

## Open questions

| ID | Вопрос | Решение |
| --- | --- | --- |
| Q-0035-1 | Включать `bcc` во внешний reply-контракт? | **closed = no.** Скрытые получатели во внешнем машинном канале не нужны; сужение поверхности. Аддитивно новым ADR при явном запросе. |
| Q-0035-2 | Возвращать `appended_to_sent`? | **closed = no.** Best-effort IMAP-append — внутренняя деталь; не влияет на факт отправки. Аддитивно при запросе. |
| Q-0035-3 | Идемпотентность (exactly-once reply)? | **closed = defer.** Как внутренний `send` — не идемпотентен в MVP; дедуп на стороне партнёра. Отдельный ADR при потребности. |
| Q-0035-4 | Per-client-ключи / отзыв отдельных прав write? | **closed = defer.** Один доверенный партнёр в MVP (один ключ). При нескольких партнёрах / потребности отзыва — отдельный ADR (таблица ключей с per-key capability). |
