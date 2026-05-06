# ADR-0015: No-JS fallback — HTTP method override + form-encoded acceptance + content negotiation

- **Статус:** accepted
- **Дата:** 2026-05-05

## Context

`docs/08-frontend.md` секция 8 предписывает обязательную поддержку базовых сценариев при отключённом JavaScript:

- login, set-password, logout;
- inbox listing;
- открытие письма, скачивание вложений;
- compose + send;
- accounts CRUD (add / edit / delete, без "Test connection");
- admin user CRUD (create / reset / delete).

Браузер из HTML-формы умеет отправлять только два метода — `GET` и `POST` — и только два типа кодирования тела — `application/x-www-form-urlencoded` или `multipart/form-data`. Прямые `DELETE`/`PATCH` и `application/json` из чистого `<form>` без JavaScript недоступны.

В то же время `docs/04-api-contracts.md` (на момент round 4 review) описывал API строго в RESTful-стиле:

- `DELETE /api/mail-accounts/{id}` (и `DELETE /api/admin/users/{id}`);
- `PATCH /api/mail-accounts/{id}`;
- `POST` создания/отправки — только с `Content-Type: application/json`.

Frontend-агент реализовал шаблоны в соответствии с no-JS требованием — формы шлют `POST /api/.../delete` с скрытым полем `_method=DELETE`, аналогично для PATCH, а тела create/edit идут как `application/x-www-form-urlencoded`. Backend-агент реализовал API строго по контракту → no-JS fallback не работает, frontend-reviewer зафиксировал 6 major-issues.

Решение, согласованное с владельцем продукта: оставить ТЗ no-JS (сценарии в `08-frontend.md` sec 8 — обязательные), но формализовать механизмы поддержки в API-контракте.

## Decision

Принимаем стандартный набор паттернов, давно отработанный во фреймворках Rails / Symfony / Django:

### 1. HTTP method override через скрытое поле `_method`

ASGI middleware `MethodOverrideMiddleware`:

- Расположение в стеке: **после парсинга тела** (Starlette body-reader), **до CSRF middleware**. Это критично — CSRF-проверка должна работать против итогового (override'нутого) метода.
- Срабатывает только на `POST`-запросах с `Content-Type: application/x-www-form-urlencoded` (multipart игнорируется — не используется в whitelist'е).
- Читает поле `_method` из form-body. Если значение — одно из `DELETE`, `PATCH`, `PUT` (whitelist методов) — переписывает `request.method`. Любое другое значение игнорируется (NoOp), `request.method` остаётся `POST`.
- Применяется **только** к роутам из whitelist (см. ниже). Запросы вне whitelist — middleware не трогает (NoOp).
- Edge case: `_method` присутствует, но route НЕ в whitelist → возвращаем `400 Bad Request` (`code=method_override_not_allowed`). Это намеренно — отлавливает накладки, не даёт случайно "распропагировать" override на чувствительные роуты.
- Логирование: при каждом успешном override — debug-сообщение с original method (`POST`), effective method (`DELETE`/`PATCH`/`PUT`), путём, request_id. Облегчает traceability при разборе инцидентов.

### 2. Form-encoded acceptance (двойное content-type)

Endpoints из whitelist принимают **оба** content-type'а: `application/json` (как раньше) **И** `application/x-www-form-urlencoded` (для no-JS форм). Маппинг полей одинаков; различие только в кодировании.

Whitelist endpoints (полный список):

| Endpoint | Канонический метод | Form-fallback (`POST` + `_method`) |
| --- | --- | --- |
| `POST /api/messages/send` | POST | (override не нужен — уже POST) |
| `POST /api/mail-accounts` | POST | (override не нужен — уже POST) |
| `PATCH /api/mail-accounts/{id}` | PATCH | `POST /api/mail-accounts/{id}` + `_method=PATCH` |
| `DELETE /api/mail-accounts/{id}` | DELETE | `POST /api/mail-accounts/{id}/delete` + `_method=DELETE` |
| `POST /api/mail-accounts/{id}/sync-now` | POST | (override не нужен — уже POST) |
| `POST /api/admin/users` | POST | (override не нужен — уже POST) |
| `POST /api/admin/users/{id}/reset` | POST | (override не нужен — уже POST) |
| `DELETE /api/admin/users/{id}` | DELETE | `POST /api/admin/users/{id}/delete` + `_method=DELETE` |

Обоснование sibling-пути для DELETE (`.../delete`): в whitelist'е `MethodOverrideMiddleware` нужно различать "POST с целью PATCH/DELETE" от "POST на канонический POST-роут". Sibling-путь снимает неоднозначность — `POST /api/mail-accounts/{id}/delete` с `_method=DELETE` явно указывает на удаление и не пересекается с `POST /api/mail-accounts/{id}` (который не существует). Аналогично для admin users delete. Для PATCH такой sibling не нужен — `POST /api/mail-accounts/{id}` сам по себе не существует, поэтому `_method=PATCH` поверх него безопасен.

### 3. Content negotiation на response side

Сервер определяет, как клиент ожидает ответ:

- **Form-клиент** (браузер из `<form>`): `Content-Type` запроса — `application/x-www-form-urlencoded`, `Accept` НЕ содержит `application/json` (типично — `text/html, ...`).
- **JSON-клиент** (fetch / curl): `Content-Type: application/json`, либо `Accept: application/json`.

Поведение:

| Сценарий | JSON-клиент | Form-клиент |
| --- | --- | --- |
| Success (create / update / delete / send) | `200`/`201`/`204` + JSON body как раньше | `303 See Other` + `Location: <redirect>` + flash через session |
| Validation error (4xx) | `400`/`422` + JSON `{error: {...}}` как раньше | Re-render формы (HTTP 200 с HTML, статус 422 для Inbox-стиля семантики допустим — frontend оба обрабатывает) + error-context (значения полей сохраняются, выводится `error.message` рядом с проблемным полем) |
| Auth/CSRF/permission error | `401`/`403` + JSON | Аналогично канонический сценарий: 302 → `/login` для auth; для CSRF/forbidden — re-render с flash об ошибке, либо 403 HTML-страница в зависимости от роута (см. existing flow в `auth/router.py`) |
| External error (502 SMTP/IMAP) | `502` + JSON | Re-render формы compose с flash "Не удалось отправить: ..." |

### 4. Redirect targets для form-success

| Endpoint | Redirect URL | Flash |
| --- | --- | --- |
| `POST /api/mail-accounts` (create, success) | `GET /accounts` | "Email-аккаунт добавлен" |
| `PATCH /api/mail-accounts/{id}` (edit, success) | `GET /accounts` | "Изменения сохранены" |
| `DELETE /api/mail-accounts/{id}` (delete, success) | `GET /accounts` | "Аккаунт удалён" |
| `POST /api/mail-accounts/{id}/sync-now` (success) | `GET /accounts` | "Синхронизация запущена" |
| `POST /api/admin/users` (create, success) | `GET /admin` | "Пользователь создан" |
| `POST /api/admin/users/{id}/reset` (success) | `GET /admin` | "Пароль сброшен" |
| `DELETE /api/admin/users/{id}` (success) | `GET /admin` | "Пользователь удалён" |
| `POST /api/messages/send` (success) | `GET /` | "Письмо отправлено" |

Все redirect-цели — server-resolved (backend хардкодит, не доверяет `Referer` / hidden-полям клиента).

### 5. Flash mechanism

Хранение:
- Ключ: `flash:{session_id}` в Redis (см. модуль `redis` в `05-modules.md`).
- Значение: JSON-список `[{category: "success" | "error" | "info", text: str}]`.
- TTL: 60 секунд (достаточно, чтобы пережить redirect + render следующей страницы).
- Семантика "read-and-clear": при следующем GET HTML-страницы Jinja2-context-builder извлекает (`GET`) и удаляет (`DEL`) ключ, передаёт список в template-context как `flashes`.

Запись flash при form-success / form-error выполняется backend перед формированием redirect'а / re-render'а.

### 6. Multi-value поля в form-encoded

Поля `to`, `cc`, `bcc` в `POST /api/messages/send`:

- **JSON**: список строк `["a@b.com", "c@d.com"]` (как раньше).
- **Form-encoded**: одна строка `to=a@b.com,c@d.com` (или semicolon `;` как разделитель). Backend нормализует:
  1. Split по `,` или `;`.
  2. `strip()` каждого элемента.
  3. Отбрасывание пустых.
  4. Дальше — стандартная валидация RFC 5322 каждого адреса.

## Consequences

### Backend

- Добавить `MethodOverrideMiddleware` (см. модуль `csrf` / middleware-stack в `05-modules.md`).
- Расширить роутеры из whitelist: для каждого endpoint добавить ветку form-encoded acceptance (через `fastapi.Form(...)` deps или Pydantic `from_form` адаптер), content-negotiation на ответе (помощник `respond(request, success_payload, redirect_to, flash_text)` инкапсулирует выбор JSON vs 303+flash).
- Реализовать flash-механизм (`redis.SessionStore` или отдельный helper `FlashStore` — на усмотрение реализующего, главное — чтобы был read-and-clear через Redis-key, привязанный к session_id).
- Sibling-роуты `POST /api/mail-accounts/{id}/delete` и `POST /api/admin/users/{id}/delete` — добавить.

### Безопасность

- `_method` override применяется **только** к whitelist endpoints — не появляется новая поверхность атаки на остальных роутах.
- CSRF-проверка обязательна для override-запросов (всё равно `POST` с form-encoded; CSRF middleware отрабатывает после override и видит итоговый метод `DELETE`/`PATCH` — но проверка идентичная).
- CSRF-токен — в скрытом поле `csrf_token` формы (тот же механизм, что во всех остальных формах; см. ADR-0010).
- `Origin`/`Referer`-проверка `SameSite=Lax` на cookie остаётся в силе.
- Никаких bypass'ов CSRF для override.

### Frontend

- Уже использует этот паттерн (фактически продиктовал необходимость ADR).
- Изменений не требует, кроме согласования путей `.../delete` (frontend-агент уже использовал именно эту форму).

### Тестирование (qa-задача)

Контрактные тесты для каждого whitelist endpoint:

1. **JSON path**: как раньше — JSON-request → JSON-response с правильным статусом.
2. **Form path success**: form-encoded request → 303 + Location + flash в Redis.
3. **Form path validation error**: form-encoded request с битыми данными → re-render формы с error-context.
4. **Method override**: `POST .../delete` + `_method=DELETE` + form-encoded → реальное удаление (тот же effect, что и `DELETE`).
5. **Method override negative**: `POST /api/messages` (не в whitelist) + `_method=DELETE` → 400 `method_override_not_allowed`.
6. **CSRF против override**: `POST .../delete` + `_method=DELETE` без CSRF-токена → 403 `csrf_failed`.
7. **Multi-value form**: `to=a@b,c@d` (CSV) + corresponding semicolon variant — оба парсятся идентично.
8. **Flash lifecycle**: после form-success — flash есть в Redis, после следующего GET — flash прочитан и удалён.

### Документация

- `04-api-contracts.md` — расширен секцией Form-encoded fallback и подзаголовками в каждом задействованном endpoint.
- `05-modules.md` — модуль `csrf` дополнен под-секцией про `MethodOverrideMiddleware`; модули `accounts` и `admin` дополнены под-секцией про content negotiation; модуль `redis` дополнен ключом `flash:{session_id}`.
- `06-security.md` — секция CSRF дополнена упоминанием override.
- `08-frontend.md` — секция 8 ссылается на ADR-0015.

## Alternatives considered

### A. JS-only delete/edit/admin (отказ от no-JS fallback)

Нарушает требование `08-frontend.md` sec 8, согласованное с владельцем продукта. Отклонено.

### B. Отдельные `POST` siblings без method-override

Например, `POST /api/mail-accounts/{id}/delete` напрямую как DELETE-семантика, `POST /api/mail-accounts/{id}/edit` как PATCH-семантика — без `_method`-механизма.

Минусы:
- Удваивает endpoint-площадь (`POST .../edit` + `PATCH .../`); каждое добавление новой операции требует пары.
- Размывает RESTful семантику (теряется смысл method'ов в OpenAPI).
- Всё равно требует form-encoded acceptance для create/edit (т.е. меньше work это не делает).

Отклонено в пользу method-override как более общего и стандартного решения.

### C. Только form-encoded, без method-override

Дублирование endpoint'ов `POST /api/mail-accounts/{id}/edit` отдельно от `PATCH /api/mail-accounts/{id}`. То же что (B), но ещё хуже для поддержки.

Отклонено.

### D. Custom non-standard scheme (например, header `X-HTTP-Method`)

Не работает для form-POST из чистого HTML — браузер не выставляет custom-headers. Подходит только для AJAX → не решает оригинальную проблему. Отклонено.

## Revisions

- **2026-05-05 (rev. 1):** initial.
