# 04. API Contracts

> **⚠️ ДЕМОНТАЖ ВЫПОЛНЕН (2026-07-15) — этот документ описывает ДО-демонтажную поверхность.** По [ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md) (Фаза A3) агрегатор headless: **действующие эндпоинты — только** `/healthz`, `/readyz` и `/api/external/*` (см. баннер §8 «Сводная таблица» — там актуальный перечень). Всё остальное (Public auth, HTML-страницы, `/api/mail-accounts*`, `/api/messages*`, `/api/tags*`, `/api/admin/*`, `/api/telegram/*`, `/api/webhooks/*`, `/api/forwarding/*`, `/api/oauth/outlook/*`, `/api/external/teams`, `/api/external/tags*`, `POST /api/external/messages/{id}/reply`) **СНЯТО и отдаёт `404`**. Аутентификация — только машинная (`EXTERNAL_API_KEY` через `X-API-Key`/`Bearer`), сессий/CSRF/form-fallback нет. Разделы помечены посекционно (`TD-050`(в)).

Все эндпоинты сервиса. Делятся на:
- **Public auth** — без сессии (login, set-password).
- **User API** — требует обычной user-сессии.
- **Admin API** — требует admin-сессии (`is_admin=true`).
- **HTML pages** — server-rendered (Jinja2), для UI; описаны кратко (детали — в `08-frontend.md`).

Все JSON-эндпоинты префикс `/api`. HTML-страницы — без префикса. CSRF — обязателен для всех state-changing методов под cookie-сессией (см. ADR-0010).

---

## Общие положения

### Аутентификация

- Cookie `mas_session` (HttpOnly, Secure в prod, SameSite=Lax). Содержит opaque token; backend ищет в Redis.
- Дополнительно cookie `mas_csrf` (не HttpOnly) для double-submit. JS-клиенты читают и шлют в `X-CSRF-Token`. HTML-формы вставляют hidden `csrf_token` (Jinja2 macro `csrf_input()`).

### Заголовки запроса

- `Content-Type: application/json` для JSON-эндпоинтов.
- `Content-Type: application/x-www-form-urlencoded` или `multipart/form-data` для HTML-форм.
- `X-CSRF-Token` — для AJAX state-changing запросов.

### Заголовки ответа

- Каждый ответ: `X-Request-ID: <uuid>` — для корреляции логов.
- HTML-страницы:
  - `Cache-Control: no-store`
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: same-origin`
  - `Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'`
  - `Strict-Transport-Security: max-age=31536000; includeSubDomains` (только в prod, поверх HTTPS).

### Унифицированный формат ошибок (JSON)

```json
{
  "error": {
    "code": "snake_case_code",
    "message": "human-readable",
    "field": "optional_field_name",
    "details": { /* optional */ }
  }
}
```

Коды ошибок (общие):

| HTTP | code | Когда |
| --- | --- | --- |
| 400 | `validation_error` | Pydantic-валидация не прошла (детали в `details.errors[]`). |
| 401 | `not_authenticated` | Нет сессии или просрочена. |
| 401 | `invalid_credentials` | login fail. |
| 403 | `forbidden` | Сессия есть, но прав нет. |
| 403 | `csrf_failed` | CSRF проверка не прошла. |
| 404 | `not_found` | |
| 409 | `conflict` | Например, username уже занят. |
| 422 | `imap_login_failed`, `smtp_login_failed`, `invalid_host` | Сбой IMAP/SMTP-логина/коннекта при тесте креденшелов (`accounts/testers.py`) + отказ SSRF-guard (`assert_public_host`). |
| 423 | `account_locked` | login lockout (`Retry-After` присутствует). |
| 429 | `rate_limited` | (`Retry-After` присутствует). |
| 500 | `internal_error` | Непредвиденная ошибка (тело не утечка детали). |
| 502 | `upstream_error`, `smtp_failed` | Upstream-сбой вне auth; либо фактическая SMTP-**отправка** не удалась (`SMTPSendFailedError` из send-ядра: коннект/AUTH/DATA/timeout — ADR-0035). Проверка соединения (`test`/create/update) сюда НЕ относится — она даёт `422`. |
| 503 | `dependency_unavailable` | Postgres/Redis/MinIO недоступны (от healthcheck). |
| 400 | `method_override_not_allowed` | Запрос `POST` с полем `_method` пришёл на роут, не входящий в whitelist form-fallback (см. ниже). |
| 400 | `cannot_delete_builtin_tag` | DELETE на тег с `is_builtin=true`. |
| 422 | `tag_apply_too_many` | `apply_to_existing=true` при числе писем у пользователя > 100 000 (см. ADR-0017 §7). |
| 400 | `group_id_must_be_null_for_new_leader` | `POST /api/admin/users` с `role=group_leader` И заданным `group_id` (новый лидер всегда auto-create'ит группу; см. ADR-0019 §5). |
| 400 | `group_has_members` | `DELETE /api/admin/groups/{id}` пока в группе остались участники или лидер; super-admin сначала переводит/удаляет их. |
| 400 | `group_leader_consistency_violation` | Инвариант лидерства нарушен (raw из триггера; обычно ловится backend'ом перед SQL). |
| 400 | `cannot_delete_group_with_super_admin_target` | Внутренняя защита от ошибочного удаления группы, ссылающейся на super_admin как лидера (по инварианту невозможно, но defensive). |
| 404 | `group_not_found` | Запрос про группу, которой нет (или у запрашивающего нет прав её видеть). |
| 403 | `user_not_in_group_scope` | Лидер пытается выполнить действие на пользователя/аккаунт вне своей группы; либо целевая `group_id` при create/transfer mail-аккаунта вне scope инициатора (ADR-0031). |
| 403 | `forbidden` (mail-account transfer) | `group_member` пытается сменить команду существующего mail-аккаунта через `PATCH /api/mail-accounts/{id}` с `group_id`. Перенос для `group_member` запрещён (ADR-0031 §4). |
| 404 | `group_not_found` (mail-account) | `POST /api/mail-accounts` или `PATCH /api/mail-accounts/{id}` с несуществующей целевой `group_id` (ADR-0031). |
| 400 | `cannot_add_super_admin_to_group` | `POST /api/admin/users/{id}/groups`: цель — `super_admin` (он видит всё, членства запрещены). См. [ADR-0030](./adr/ADR-0030-multi-group-membership.md). |
| 409 | `membership_already_exists` | `POST /api/admin/users/{id}/groups`: пользователь уже состоит в этой команде (UNIQUE `user_groups(user_id, group_id)`). Идемпотентность. См. ADR-0030. |
| 400 | `cannot_remove_home_membership` | `DELETE /api/admin/users/{id}/groups/{group_id}`: попытка удалить **домашнее** членство (`= users.group_id`). Для смены домашней команды используйте «Переместить» (`PATCH /api/admin/users/{id}`). См. ADR-0030. |
| 404 | `membership_not_found` | `DELETE /api/admin/users/{id}/groups/{group_id}`: у пользователя нет такого (дополнительного) членства. См. ADR-0030. |
| 409 | `cannot_move_group_leader` | `PATCH /api/admin/users/{id}` со сменой `group_id` для `role='group_leader'`: перенос лидера запрещён (нарушил бы инвариант лидера). Для лидера доступно только «Добавить в команду». См. ADR-0030. |
| 401 | `invalid_init_data` | `POST /api/telegram/auth`: HMAC-подпись Telegram `init_data` некорректна. См. [ADR-0022](./adr/ADR-0022-telegram-sso-and-notifications.md) §1.2. |
| 401 | `init_data_expired` | `POST /api/telegram/auth`: `auth_date` в `init_data` старше 5 минут. |
| 400 | `webhook_url_private_ip` | `POST/PATCH /api/webhooks/me`: URL резолвится в приватный CIDR / localhost. SSRF-защита. См. [ADR-0023](./adr/ADR-0023-outbound-webhooks.md) §4.3. |
| 409 | `webhook_already_exists` | `POST /api/webhooks/me`: у группы уже есть webhook (`UNIQUE(group_id)`). Используется `PATCH` для update или `DELETE` + `POST` для пересоздания. |
| 400 | `validation_error` (`field=forward_to`) | `PUT /api/forwarding/me`: `forward_to` отсутствует или не проходит e-mail-паттерн (`accounts/schemas.py`: один `@`, домен с точкой, без `..`, длина 3..254). См. [ADR-0034](./adr/ADR-0034-leader-mail-forwarding.md) §2. |
| 400 | `validation_error` (`field=group_id`) | `GET/PUT/DELETE /api/forwarding/me`: `super_admin` не передал обязательный `?group_id=`, либо `group_leader` передал запрещённый `?group_id=`. Симметрично webhooks (ADR-0023 §2). |
| 401 | `not_authenticated` (external) | `GET /api/external/messages`: нет/неверный `X-API-Key`/`Bearer`, **или** фича выключена (`EXTERNAL_API_KEY` пуст). Неперечислимо — «выключено» неотличимо от «неверный ключ». См. [ADR-0029](./adr/ADR-0029-external-pull-api.md) §3/§4. |
| 404 | `password_not_set` | `GET /api/admin/users/{id}/password`: у пользователя нет обратимой копии пароля (`users.password_encrypted IS NULL` — пароль предшествует ADR-0038 и не менялся, либо задан в self-set-режиме). Колонка «Пароль» показывает «—». См. [ADR-0038](./adr/ADR-0038-reversible-login-password-storage.md). |

---

## Form-encoded fallback (no-JS support)

> **⚠️ ИСТОРИЧЕСКИЙ РАЗДЕЛ — form-fallback вместе с Jinja-UI (ADR-0015/0041, Фаза A3) СНЯТО демонтажём ([ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md), выполнено на проде 2026-07-15).** Описанное ниже в коде/проде агрегатора НЕ существует; текст сохранён как record исходного решения. НЕ реализовывать и не принимать за действующий контракт. **Актуально:** поверхность агрегатора — только JSON machine-API; счётчики `_OVERRIDE_*` и `_method`-override сняты.

Источник истины — [ADR-0015](./adr/ADR-0015-no-js-fallback.md). Требование вытекает из `08-frontend.md` секция 8 (обязательный no-JS режим для базовых сценариев).

### Whitelist endpoints, принимающих form-encoded

Перечисленные ниже endpoints принимают **оба** content-type'а — `application/json` (для AJAX-клиентов) **И** `application/x-www-form-urlencoded` (для HTML-форм без JS). Маппинг полей идентичен.

| Endpoint (canonical) | Также доступен через form-fallback |
| --- | --- |
| `POST /api/messages/send` | (тот же путь и метод) |
| `POST /api/mail-accounts` (create) | (тот же путь и метод) |
| `PATCH /api/mail-accounts/{id}` (edit + перенос команды ADR-0031) | `POST /api/mail-accounts/{id}` + form-поле `_method=PATCH` |
| `DELETE /api/mail-accounts/{id}` | `POST /api/mail-accounts/{id}/delete` + form-поле `_method=DELETE` |
| `POST /api/mail-accounts/{id}/sync-now` | (тот же путь и метод) |
| `POST /api/admin/users` (create) | (тот же путь и метод) |
| `POST /api/admin/users/{id}/reset` | (тот же путь и метод) |
| `DELETE /api/admin/users/{id}` | `POST /api/admin/users/{id}/delete` + form-поле `_method=DELETE` |
| `POST /api/tags` (create) | (тот же путь и метод; multi-row rules — см. ниже) |
| `PATCH /api/tags/{id}` (edit name/color) | `POST /api/tags/{id}` + form-поле `_method=PATCH` |
| `DELETE /api/tags/{id}` | `POST /api/tags/{id}/delete` + form-поле `_method=DELETE` |
| `POST /api/tags/{id}/rules` (add rule) | (тот же путь и метод) |
| `DELETE /api/tags/{id}/rules/{rule_id}` | `POST /api/tags/{id}/rules/{rule_id}/delete` + form-поле `_method=DELETE` |
| `POST /api/tags/{id}/apply-to-existing` | (тот же путь и метод) |
| `POST /api/admin/groups` (create) | (тот же путь и метод) |
| `PATCH /api/admin/groups/{id}` (rename) | `POST /api/admin/groups/{id}` + form-поле `_method=PATCH` |
| `DELETE /api/admin/groups/{id}` | `POST /api/admin/groups/{id}/delete` + form-поле `_method=DELETE` |
| `PATCH /api/admin/users/{id}` (role/group/display_name) | `POST /api/admin/users/{id}` + form-поле `_method=PATCH` |
| `POST /api/admin/users/{id}/groups` (add membership, ADR-0030) | (тот же путь и метод) |
| `DELETE /api/admin/users/{id}/groups/{group_id}` (remove membership, ADR-0030) | `POST /api/admin/users/{id}/groups/{group_id}/delete` + form-поле `_method=DELETE` |
| `POST /api/webhooks/me` (create) | (тот же путь и метод) |
| `PATCH /api/webhooks/me` (edit) | `POST /api/webhooks/me` + form-поле `_method=PATCH` |
| `DELETE /api/webhooks/me` | `POST /api/webhooks/me/delete` + form-поле `_method=DELETE` |
| `POST /api/webhooks/me/rotate-secret` | (тот же путь и метод) |
| `POST /api/webhooks/me/test` | (тот же путь и метод) |
| `PUT /api/forwarding/me` (upsert, ADR-0034) | `POST /api/forwarding/me` + form-поле `_method=PUT` |
| `DELETE /api/forwarding/me` (ADR-0034) | `POST /api/forwarding/me/delete` + form-поле `_method=DELETE` |

Любые остальные роуты не принимают `_method` — `POST` с этим полем на не-whitelist-роуте даёт `400 method_override_not_allowed`.

> **ADR-0031.** Перенос команды ящика идёт через **существующий** `PATCH /api/mail-accounts/{id}` (строка выше) — **новый** form-fallback regex-путь не вводится. Поэтому `_OVERRIDE_REGEX_PATHS` в `backend/app/middlewares.py` остаётся **16** записей, а хардкод `_OVERRIDE_REGEX_PATHS = 16` в `tests/unit/test_method_override.py` (`TestRegexCount.test_regex_paths_present`) **менять не нужно**.

> **ADR-0034.** Оба form-fallback-пути переадресации (`/api/forwarding/me` для PUT, `/api/forwarding/me/delete` для DELETE) — **exact** (без `\d+`-параметра), поэтому добавляются в `_OVERRIDE_EXACT_PATHS` в `backend/app/middlewares.py` (было **5** → станет **7**), а регекс-список `_OVERRIDE_REGEX_PATHS` (16) **не меняется**. Backend-агент обновляет хардкод в `tests/unit/test_method_override.py` (`TestRegexCount.test_exact_paths_present`: `assert len(_OVERRIDE_EXACT_PATHS) == 5` → `== 7`). Регекс-счётчик (16) **не трогать**.

> **ADR-0038.** Показ пароля — `GET /api/admin/users/{id}/password` — это **read-only** (не мутация), method-override для него **не применяется**, новый override-путь **не вводится**. Поле `password` в create (`POST /api/admin/users`) и reset (`POST /api/admin/users/{id}/reset`), а также `additional_group_ids` в create — используют **существующие** whitelisted-пути (`/api/admin/users` exact; `^/api/admin/users/\d+/reset$` regex), новых путей нет. **Итог: `_OVERRIDE_EXACT_PATHS` остаётся 7, `_OVERRIDE_REGEX_PATHS` остаётся 16** — счётчики в `tests/unit/test_method_override.py` (`TestRegexCount`: exact == 7, regex == 16) **менять не нужно**.

### Метод override

ASGI-middleware `MethodOverrideMiddleware` читает поле `_method` из body POST-запросов с `Content-Type: application/x-www-form-urlencoded`; если значение ∈ {`DELETE`, `PATCH`, `PUT`} — переписывает `request.method`. Применяется только к whitelist-роутам. См. ADR-0015 и `05-modules.md` модуль `csrf`/middleware-stack.

CSRF-проверка для override-запросов **обязательна** — токен передаётся в скрытом поле формы `csrf_token` (стандартный механизм; см. ADR-0010).

### Content negotiation

Сервер различает клиента по заголовкам:
- **JSON-клиент** (как раньше): `Content-Type: application/json` или `Accept: application/json`.
- **Form-клиент**: `Content-Type: application/x-www-form-urlencoded`, `Accept` НЕ содержит `application/json`.

| Сценарий | JSON-клиент | Form-клиент |
| --- | --- | --- |
| Success | `200`/`201`/`204` + JSON body | `303 See Other` + `Location: <server-resolved URL>` + flash в session |
| Validation error | `400`/`422` + `{error: {...}}` | Re-render формы с error-context (значения полей сохранены, ошибка показана рядом с проблемным полем) |
| External error (502 SMTP/IMAP) | `502` + JSON | Re-render формы с flash об ошибке |

### Redirect targets для form-success (server-resolved)

| Endpoint | Redirect URL | Flash text |
| --- | --- | --- |
| `POST /api/mail-accounts` | `/accounts` | "Email-аккаунт добавлен" |
| `PATCH /api/mail-accounts/{id}` | `/accounts` | "Изменения сохранены" |
| `DELETE /api/mail-accounts/{id}` | `/accounts` | "Аккаунт удалён" |
| `POST /api/mail-accounts/{id}/sync-now` | `/accounts` | "Синхронизация запущена" |
| `POST /api/admin/users` | `/admin` | "Пользователь создан" |
| `POST /api/admin/users/{id}/reset` | `/admin` | "Пароль сброшен" |
| `DELETE /api/admin/users/{id}` | `/admin` | "Пользователь удалён" |
| `POST /api/messages/send` | `/` | "Письмо отправлено" |
| `POST /api/tags` | `/tags` | "Тег создан" (если `apply_to_existing=true` — "Тег создан, применён к {N} письмам") |
| `PATCH /api/tags/{id}` | `/tags` | "Тег обновлён" |
| `DELETE /api/tags/{id}` | `/tags` | "Тег удалён" |
| `POST /api/tags/{id}/rules` | `/tags/{id}/edit` | "Правило добавлено" |
| `DELETE /api/tags/{id}/rules/{rule_id}` | `/tags/{id}/edit` | "Правило удалено" |
| `POST /api/tags/{id}/apply-to-existing` | `/tags` | "Применено к {N} письмам" |
| `POST /api/admin/groups` | `/admin/groups` | "Группа создана" |
| `PATCH /api/admin/groups/{id}` | `/admin/groups` | "Группа переименована" |
| `DELETE /api/admin/groups/{id}` | `/admin/groups` | "Группа удалена" |
| `PATCH /api/admin/users/{id}` | `/admin` | "Пользователь обновлён" (для move — "Пользователь перемещён в другую команду") |
| `POST /api/admin/users/{id}/groups` | `/admin` | "Пользователь добавлен в команду" |
| `DELETE /api/admin/users/{id}/groups/{group_id}` | `/admin` | "Членство в команде удалено" |
| `POST /api/webhooks/me` | `/my/integrations` | "Webhook создан" + one-shot flash `[secret_reveal]` с plaintext |
| `PATCH /api/webhooks/me` | `/my/integrations` | "Webhook обновлён" |
| `DELETE /api/webhooks/me` | `/my/integrations` | "Webhook удалён" |
| `POST /api/webhooks/me/rotate-secret` | `/my/integrations` | one-shot flash `[secret_reveal]` с новым plaintext |
| `POST /api/webhooks/me/test` | `/my/integrations` | "Тест выполнен: HTTP {code}, {duration_ms} мс" |
| `PUT /api/forwarding/me` (via `POST` + `_method=PUT`) | `/my/integrations` | "Переадресация сохранена" (ADR-0034) |
| `DELETE /api/forwarding/me` (via `POST /api/forwarding/me/delete` + `_method=DELETE`) | `/my/integrations` | "Переадресация удалена" (ADR-0034) |

### Multi-value поля (form-encoded)

Поля `to`, `cc`, `bcc` в `POST /api/messages/send`:
- **JSON**: список строк `["a@b.com", "c@d.com"]`.
- **Form-encoded**: одна строка с разделителем `,` или `;` (поддерживаются оба): `to=a@b.com, c@d.com;e@f.com`.
- Backend нормализует: split → `strip()` → отбросить пустые → RFC 5322-валидация каждого.

### Flash mechanism

- Хранение: Redis-ключ `flash:{session_id}`, JSON-список `[{category, text}]`. TTL 60 сек.
- Lifecycle: write при form-success / form-error → read-and-clear при следующем GET HTML-страницы → передаётся в template-context как `flashes`.
- См. модуль `redis` (`05-modules.md`) — добавлен этот ключ.

---

## 1. Public Auth

> **⚠️ ИСТОРИЧЕСКИЙ РАЗДЕЛ — публичный логин/сессии/set-password вместе с Jinja-UI (Фаза A3) СНЯТО демонтажём ([ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md), выполнено на проде 2026-07-15).** Описанное ниже в коде/проде агрегатора НЕ существует; текст сохранён как record исходного решения. НЕ реализовывать и не принимать за действующий контракт. **Актуально:** входа в агрегатор нет; аутентификация только машинная — `EXTERNAL_API_KEY` (`X-API-Key`/`Bearer`).

### `GET /login`

Render HTML form. Если пользователь уже залогинен — `302 Location: /`.

### `POST /login` (step-1 of two-step login — ADR-0016)

| | |
| --- | --- |
| Запрос | `application/x-www-form-urlencoded` или `application/json` |
| Поля | `username` (str, 1..64). Без `password` — он вводится на следующем шаге. |
| Rate-limit | 30 / 15 минут per IP (легкий, не дает enumeration brute-force). |
| 200 | (только для JSON) `{"kind": "set_password_required", "redirect": "/set-password"}` или `{"kind": "needs_password", "redirect": "/login/password"}`. |
| 303 | (для form) Set-Cookie `mas_login` (HttpOnly, 15 мин) + `Location: /login/password`; либо Set-Cookie `mas_setup` + `Location: /set-password`. |
| 400 | `validation_error` (пустое/слишком длинное username). |

Семантика:
- Если user найден и `password_reset_required=true` — backend создаёт временную **setup-session** (Redis ключ `setup_session:{token}`, TTL 15 минут), ставит cookie `mas_setup`, редирект на `/set-password`.
- Если user найден и password установлен — backend ставит cookie `mas_login` (значение = lower-case username) и редирект на `/login/password`.
- Если user НЕ найден — те же действия, что для "найден и password установлен" (cookie + redirect на `/login/password`). На step-2 будет возвращён generic `invalid_credentials`. Это устраняет user-enumeration через timing/redirect.

### `GET /login/password` (step-2 form)

| | |
| --- | --- |
| Доступ | требует cookie `mas_login`. Без неё — 303 на `/login`. |
| 200 | HTML form: read-only username (из cookie) + password input + csrf_token (пустой, см. CSRF exempt). |

### `POST /login/password` (step-2 — verify password, create session)

| | |
| --- | --- |
| Запрос | `application/x-www-form-urlencoded` или `application/json`. |
| Поля | `password` (str, 1..128). Username извлекается из cookie `mas_login` — submit поле `username` игнорируется. |
| Rate-limit | 5 / 15 минут per `username|IP` (см. ADR-0009). |
| 200 | (только для JSON) `{"kind": "session_created", "redirect": "/"}` или `{"kind": "set_password_required", "redirect": "/set-password"}`. |
| 303 | (для form) Set-Cookie `mas_session`, `mas_csrf` + clear `mas_login`; `Location: /` либо `/set-password`. |
| 401 | `invalid_credentials` (общая формулировка, не раскрываем существование username) либо `not_authenticated` если cookie `mas_login` отсутствует. |
| 423 | `account_locked` + `Retry-After`. |
| 429 | `rate_limited` + `Retry-After`. |

CSRF: оба endpoint'а (`POST /login` и `POST /login/password`) **exempt** — у пользователя ещё нет session. Защита: rate-limit + lockout + короткий TTL cookie `mas_login`.

### `GET /set-password`

| | |
| --- | --- |
| Доступ | требует cookie `mas_setup` (валидная setup-session). |
| 200 | HTML form: `password`, `password_confirm`, `csrf_token`. |
| 302 | Если `mas_setup` отсутствует — `Location: /login`. |

### `POST /set-password`

| | |
| --- | --- |
| Запрос | form: `password`, `password_confirm`, `csrf_token`. |
| Валидация | `password` length 12..128; должен содержать min 1 цифру и 1 букву; `password == password_confirm`. |
| Rate-limit | 5 / 15 минут per setup-session-token (cookie `mas_setup`); fallback на IP только если cookie отсутствует/невалиден. |
| 302 | На успехе: Set-Cookie `mas_session`, удаляем `mas_setup`, `Location: /`. |
| 400 | `validation_error` (mismatch/слабый пароль). |
| 401 | `not_authenticated` (нет/истекла setup-session). |
| 429 | `rate_limited`. |

### `POST /logout`

| | |
| --- | --- |
| Запрос | пустое тело + CSRF. |
| 302 | удалить session-key из Redis, очистить cookies, `Location: /login`. |

---

## 2. User HTML pages

> **⚠️ ИСТОРИЧЕСКИЙ РАЗДЕЛ — все HTML-страницы (Фаза A3) СНЯТО демонтажём ([ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md), выполнено на проде 2026-07-15).** Описанное ниже в коде/проде агрегатора НЕ существует; текст сохранён как record исходного решения. НЕ реализовывать и не принимать за действующий контракт. **Актуально:** любой HTML-URL → `404`.

| Метод | Путь | Описание |
| --- | --- | --- |
| GET | `/` | Inbox (объединённый список писем со всех аккаунтов). Параметры query: `account_id` (фильтр), `tag_id` (фильтр по тегу; ownership проверяется), `cursor` (keyset, тот же формат что у `GET /api/messages`), `unread` (bool), `limit` (default 50, max 200). Page-based pagination не поддерживается — только cursor. <br>**Примечание:** фильтр «по почте» в UI — searchable typeahead-combobox (см. `08-frontend.md` §4.3); это UX-слой, серверный контракт (единственный параметр `account_id` + его scope-авторизация) **не изменён**. |
| GET | `/messages/{id}` | Просмотр одного письма (plain text) + список вложений + теги. |
| GET | `/compose` | Форма нового письма (выбор from-аккаунта). |
| GET | `/compose?reply_to={message_id}` | Форма ответа (subject prefilled "Re: ...", body цитата). |
| GET | `/accounts` | Список mail-аккаунтов пользователя. **Query `status?`** (ADR-0038-сопутствующее / фикс): `all\|active\|inactive`, default `all` — серверный фильтр по `MailAccountDTO.is_active` (модель не меняется). `active` → только `is_active=true`; `inactive` → только `is_active=false`; `all`/отсутствует → без фильтра. Значение прокидывается в контекст шаблона как `status_filter` для подсветки текущего сегмента тулбара. Применяется **после** `VisibilityScope` (фильтр — по уже видимому пользователю набору; работает для super_admin и leader/member). Некорректное значение → трактуется как `all` (не 400). |
| GET | `/accounts/new` | Форма добавления mail-аккаунта (с auto-suggest). |
| GET | `/accounts/{id}/edit` | Форма редактирования (без отображения пароля; новый — опционально). |
| GET | `/tags` | Список пользовательских тегов с кнопкой "+ Добавить тег". |
| GET | `/tags/new` | Форма создания тега (имя + цвет + список conditions + checkbox `apply_to_existing`). |
| GET | `/tags/{id}/edit` | Форма редактирования тега (имя + цвет + add/remove rules). |

Все требуют user-сессии; иначе 302 → `/login`.

---

## 3. User JSON API

> **⚠️ ИСТОРИЧЕСКИЙ РАЗДЕЛ — cookie-сессионный пользовательский JSON API (`/api/mail-accounts*`, `/api/messages*`, `/api/tags*`, `/api/my/*`, session-`send`) СНЯТО демонтажём ([ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md), выполнено на проде 2026-07-15).** Описанное ниже в коде/проде агрегатора НЕ существует; текст сохранён как record исходного решения. НЕ реализовывать и не принимать за действующий контракт. **Актуально:** эквивалент mailbox-операций — в машинном разделе §4f (`/api/external/mailboxes*`) под `EXTERNAL_API_KEY` + `EXTERNAL_WRITE_ENABLED`.

Префикс `/api`. Все требуют user-сессии. Все state-changing — CSRF.

### Mail accounts

#### `GET /api/mail-accounts`
| Query | `group_id?` (только для super_admin — фильтр по группе; для остальных игнорируется), `user_id?` (фильтр по конкретному владельцу — для super_admin/лидера в рамках своей группы) |
| 200 | `[{id, user_id, owner: {id, username, display_name}, email, display_name, imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_ssl, smtp_starttls, smtp_username, is_active, last_synced_at, last_sync_error, consecutive_failures, created_at}]` |
| Visibility | Применяется `VisibilityScope` (см. ADR-0019 §7): super_admin видит все, лидер/участник — все аккаунты участников своей группы. Поле `owner` показывает, кто владелец аккаунта в группе (для UI «чей это ящик»). |

#### `POST /api/mail-accounts`
| Запрос | `{email, password, display_name?: str\|null, imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_ssl, smtp_starttls, smtp_username?, smtp_password?, target_user_id?: int, group_id?: int\|null}` |
| Валидация | email — RFC 5322; ports 1..65535; `smtp_ssl XOR smtp_starttls` (не оба true); `password` 1..256; `display_name` 1..100 (после trim'а пустая → null, см. ADR-0020). |
| Поведение | Перед сохранением — IMAP login + SMTP login (тест). При успехе — шифруем (AES-GCM), вставляем строку, возвращаем. <br>**`target_user_id` логика** (ADR-0019 §8): <br>— `super_admin`: `target_user_id` опционален (default = own id; если указан — backend проверяет существование). <br>— `group_leader`: `target_user_id` опционален (default = own id; если указан — backend проверяет, что target в той же группе, иначе `403 user_not_in_group_scope`). <br>— `group_member`: `target_user_id` запрещён или должен `== own id`; иначе `400 validation_error`. <br>**`group_id` логика** (ADR-0031 §2/§4 — команда, в которую кладётся ящик): <br>— **Не передан** → берётся **домашняя** группа владельца (`users.group_id`); для `super_admin`, создающего на себя, домашней группы нет → `NULL` (персональный ящик). Полная обратная совместимость. <br>— **Передан** → валидируется по роли создателя: `group_member` → `group_id ∈ его user_groups` (`scope.group_ids`), только на себя; `group_leader` себе → `group_id ∈ его user_groups`, участнику своей команды → `group_id = команда лидера` (`scope.group_id`); `super_admin` → любая существующая группа ИЛИ `null`. Несуществующая → `404 group_not_found`; вне scope → `403 user_not_in_group_scope`. **Никогда `500`.** |
| Rate-limit | 10 / час per user. |
| 201 | `{id, user_id, owner: {...}, email, display_name, group_id, ...}` (без паролей). |
| 422 | `imap_login_failed` или `smtp_login_failed` + `details.detail` (текст ошибки от провайдера, без сензитива). |
| 409 | `conflict` (`field=email`) — этот email уже есть у этого `user_id` (т.е. `target_user_id`). UNIQUE по `(user_id, email)` — два разных user'а одной группы могут добавить одинаковый email; это намеренно, хотя на практике в группе делают один общий ящик. |
| 403 | `user_not_in_group_scope` (target не в группе лидера, либо переданный `group_id` вне scope создателя). |
| 404 | `group_not_found` (переданный `group_id` не существует). |

##### Form-encoded request (no-JS)
```
POST /api/mail-accounts HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

email=user%40gmail.com&password=secret&imap_host=imap.gmail.com&imap_port=993&imap_ssl=on&smtp_host=smtp.gmail.com&smtp_port=587&smtp_starttls=on&group_id=3&csrf_token=...
```
> Пример использует **587/STARTTLS** (ADR-0032 follow-up: прод-сервер блокирует TCP 465). Провайдер-пресеты (`05-modules.md` §9) авто-подставляют `smtp_port=587`, `smtp_ssl` off, `smtp_starttls` on для password-провайдеров; `smtp_ssl` и `smtp_starttls` взаимоисключаемы (CHECK).

Чекбоксы (`imap_ssl`, `smtp_ssl`, `smtp_starttls`): значение `on`/`true`/`1` → true; отсутствие поля → false. Опциональные поля (`smtp_username`, `smtp_password`) — допускают пустую строку, backend интерпретирует как «не задано». Поле `group_id` (ADR-0031): отсутствие/пустая строка → не передано (домашняя группа владельца); значение → выбранная команда (валидируется по роли). Для `super_admin` пустое значение в селекторе «Без команды» означает `NULL` — backend интерпретирует явный выбор «Без команды» как `group_id=null` (form-маркер — см. `08-frontend.md` §4.7).

##### Form-encoded response
- Success: `303 See Other`, `Location: /accounts`, flash="Email-аккаунт добавлен".
- Validation/external error: re-render `accounts/form.html` с error-context.

#### `POST /api/mail-accounts/test`
| Запрос | те же поля что POST mail-accounts |
| Назначение | Сухой прогон IMAP+SMTP без сохранения. |
| Rate-limit | 10 / час per user. |
| 200 | `{imap_ok: true, smtp_ok: true}`. |
| 422 | первый fail возвращает соответствующий код. |
| **Верхняя граница (ADR-0047)** | Весь тест (host-assert + IMAP + SMTP) выполняется под hard-deadline `MAILBOX_TEST_DEADLINE_SECONDS` (45 с). Исчерпание → **`422`** `imap_login_failed` / `smtp_login_failed` с `details.detail="timeout"` (стадия — в `details.stage`). Новых кодов ошибок нет; `504` / зависание на этом пути — дефект. Тот же дедлайн наследуют `POST /api/mail-accounts` и `PATCH /api/mail-accounts/{id}` при смене кредов/хостов (оба идут через `MailAccountService.test()`). См. `05-modules.md` §9.2. |

#### `GET /api/mail-accounts/{id}`
| 200 | объект (как в list). |
| 404 | если не принадлежит пользователю. |

#### `PATCH /api/mail-accounts/{id}`
| Запрос | любое подмножество полей, включая `display_name?: str\|null` (см. ADR-0020) и **`group_id?: int\|null`** (ADR-0031 — перенос ящика в другую команду). Если `password` присутствует — пере-шифровываем. Если меняются хосты/порты/auth — backend обязан повторить тест IMAP/SMTP перед сохранением. Перенос команды (`group_id`) сам по себе **повторного теста не требует** (credentials/хосты не трогаются). |
| Visibility | Можно редактировать любой аккаунт в области видимости текущего пользователя (super_admin — все; лидер/участник — все аккаунты своих команд). См. ADR-0019 §8 + ADR-0030. Сам ящик должен быть виден инициатору, иначе `404`. |
| **`group_id` — перенос (ADR-0031 §3/§4)** | Присутствие ключа `group_id` (JSON) / form-поля `group_id` (no-JS) ⇒ сменить команду (отличается от «не передано» через sentinel). Авторизация: <br>— **`super_admin`** — в любую существующую группу ИЛИ `null` (персональный). <br>— **`group_leader`** — ящики в его управлении (его команды); целевая команда — как при create (себе → его `user_groups`; ящику участника своей команды → его команда). <br>— **`group_member`** — менять команду существующего ящика **НЕЛЬЗЯ** → `403 forbidden` (даже на свой ящик; см. ADR-0031 §4). <br>Целевая `group_id`: несуществующая → `404 group_not_found`; вне scope → `403 user_not_in_group_scope`. **Никогда `500`.** Repo-метод `MailAccountsRepo.update_group(account_id, group_id)`. Пишется audit `mail_account_group_change` (`details={mail_account_id, from_group_id, to_group_id}`). |
| 200 | объект (включая `display_name`, `group_id`). |
| 422 / 409 | как при POST. |
| 403 | `forbidden` (`group_member` пытается сменить команду) / `user_not_in_group_scope` (целевая команда вне scope). |
| 404 | `group_not_found` (целевая `group_id` не существует) / `not_found` (ящик вне scope). |

##### Form-encoded request (no-JS)
Через method override (**существующий** whitelist-путь `^/api/mail-accounts/\d+$`; перенос не добавляет нового form-fallback-роута — счётчик `_OVERRIDE_REGEX_PATHS` в `tests/unit/test_method_override.py` остаётся **16**):
```
POST /api/mail-accounts/42 HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

_method=PATCH&group_id=5&csrf_token=...
```
Пустые поля (`password=`) интерпретируются как "не менять"; чтобы реально очистить опциональное поле — backend поддерживает (для edit-формы это не применяется; пароль НЕ может быть очищен). Поле `group_id`: **присутствие** поля ⇒ сменить команду (пустое значение ⇒ `NULL`, допустимо только для `super_admin`); **отсутствие** поля ⇒ команду не менять. Используется отдельная форма «Сменить команду» (см. `08-frontend.md` §4.6) — она шлёт только `_method`, `group_id`, `csrf_token`, не затрагивая остальные поля ящика.

##### Form-encoded response
- Success: `303 See Other`, `Location: /accounts`, flash="Изменения сохранены" (для переноса — "Ящик перенесён в другую команду").
- Validation/external error: re-render `accounts/form.html` (edit-вариант) с error-context.

#### `DELETE /api/mail-accounts/{id}`
| Поведение | CASCADE-удаление в БД + cleanup MinIO по префиксу. |
| 204 | success. |

##### Form-encoded request (no-JS)
Через method override на sibling-роуте:
```
POST /api/mail-accounts/42/delete HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

_method=DELETE&csrf_token=...
```

##### Form-encoded response
- Success: `303 See Other`, `Location: /accounts`, flash="Аккаунт удалён".

#### `POST /api/mail-accounts/{id}/sync-now` (опциональный — рекомендован)
| Назначение | Принудительно запустить sync конкретного аккаунта вне расписания. |
| Реализация | Записывает marker в Redis (`force_sync:{account_id}`) с TTL 60s; worker при следующем тике в первую очередь обрабатывает помеченные. Если worker не подхватит за 60s (например, лежит) — клиент видит timeout-flash в UI. |
| Rate-limit | 5 / час per account. |
| 202 | `{queued: true}`. |

##### Form-encoded request (no-JS)
```
POST /api/mail-accounts/42/sync-now HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

csrf_token=...
```

##### Form-encoded response
- Success: `303 See Other`, `Location: /accounts`, flash="Синхронизация запущена".

### Messages

#### `GET /api/messages`
| Query | `account_id?` (filter — должен быть в области видимости), `group_id?` (filter — **только super_admin**; ограничивает выдачу одной группой), `tag_id?` (filter; ownership tag.user_id=current_user; 404 на чужой), `unread?` (bool), `cursor?` (id для keyset pagination), `limit?` (default 50, max 200) |
| Visibility | Применяется `VisibilityScope` (см. ADR-0019 §7): super_admin видит messages всех; лидер/участник — messages всех участников своей группы. SQL добавляет JOIN `messages → mail_accounts → users` и WHERE по `users.group_id`. |
| Pagination | Keyset по `(internal_date DESC, id DESC)`. Курсор — base64(`{internal_date_iso}:{id}`). |
| 200 | `{items: [{id, mail_account_id, mail_account_email, mail_account_display_name, owner: {id, username, display_name}, from_addr, from_name, subject, internal_date, is_read, has_attachments, tags: [{id, name, color}]}], next_cursor: str\|null}` |
| Note | Поле `mail_account_display_name` — никнейм ящика по ADR-0020 (nullable). UI помощник `effective_account_label = display_name \|\| email`. Поле `owner` — кто владелец ящика (для group-видимости — чтобы лидер понимал, чей это ящик). |

#### `GET /api/messages/{id}`
| 200 | `{id, mail_account_id, mail_account_email, mail_account_display_name, owner: {id, username, display_name}, from_addr, from_name, to_addrs, cc_addrs, subject, internal_date, body_text, body_truncated, body_present, in_reply_to, is_read, attachments: [{id, filename, content_type, size_bytes, skipped_too_large}], tags: [{id, name, color}]}` |
| 404 | если письмо вне области видимости текущего пользователя (через `VisibilityScope` — см. ADR-0019 §7.2). |
| Note | **round-37 (ADR-0022 §2.10):** `body_text` / `body_html` нормализованы для отображения — прогоны из 3+ пустых строк / 3+ `<br>` / пустых `<p>`/`<div>` схлопнуты до одного разделителя абзаца (фикс бага «множество пустых строк» у Apple/маркетинговых писем). Хранимое в БД тело не меняется; та же нормализация применяется на HTML-странице `GET /messages/{id}` (вкл. residual `?embed=tg`). Логика — в `MessageService.get`, общая для JSON и HTML. **TG «Посмотреть сообщение»** (callback `msg:{id}`, Bug-fix #5) рендерится **отдельным** путём `callback_handler._format_message_body` — round-37 её не покрывает; её нормализацию добавляет round-39 (`collapse_blank_lines_tg`). |

#### `POST /api/messages/{id}/mark-read`
| Запрос | `{is_read: bool}` |
| 204 | success |

#### `GET /api/messages/{id}/attachments/{aid}`
| Поведение | Stream бинарного содержимого из MinIO. |
| Headers | `Content-Type` из БД; `Content-Disposition: attachment; filename="<sanitized>"`; `Content-Length`. |
| 200 | binary stream |
| 404 | если не найдено или не принадлежит пользователю или `skipped_too_large=true`. |

### Send

#### `POST /api/messages/send`
| Запрос | `{from_account_id, to: [str], cc?: [str], bcc?: [str], subject?, body: str, in_reply_to_message_id?: int}` |
| Валидация | каждый адрес — RFC 5322; `body` 0..1 MiB; subject 0..998 chars (RFC limit). Если `in_reply_to_message_id` указан — он должен принадлежать пользователю; backend заполняет In-Reply-To/References из этого сообщения. |
| Поведение | См. F3 в `01-architecture.md`. SMTP send -> sent_messages insert -> background IMAP append. |
| Rate-limit | 30 / час per user. |
| 200 | `{sent_id, smtp_message_id, appended_to_sent: bool}` |
| 502 | `smtp_failed` + `details.detail`. |

##### Form-encoded request (no-JS)
```
POST /api/messages/send HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

from_account_id=42&to=alice%40example.com%2C+bob%40example.com&cc=&bcc=&subject=Hello&body=Test+body&csrf_token=...
```
Поля `to`, `cc`, `bcc` — одна строка с разделителями `,`/`;`; парсер делает split → strip → отбрасывание пустых → RFC 5322-валидация. Пустое поле трактуется как пустой список. `in_reply_to_message_id` — целое число или пустая строка (отсутствует).

##### Form-encoded response
- Success: `303 See Other`, `Location: /`, flash="Письмо отправлено".
- Validation/SMTP error: re-render `compose.html` с error-context (значения полей формы возвращаются для повторной правки).

### Tags

Источник истины — [ADR-0017](./adr/ADR-0017-tags.md). Все эндпоинты требуют user-сессии. Все state-changing — CSRF. Tags изолированы per-user через `tags.user_id`; чужой `tag_id` всегда возвращает 404 (не 403, чтобы не утечкой существование чужого).

#### `GET /api/tags`
| 200 | `[{id, name, color, is_builtin, rules: [{id, type, pattern}], created_at, updated_at}]` |
| Описание | Список всех тегов текущего пользователя (включая 4 builtin). Возвращается с rules inline — UI всегда нужно показывать вместе. |

#### `POST /api/tags`
| Запрос (JSON) | `{name: str (1..64), color: str (#RRGGBB), rules: [{type, pattern}], apply_to_existing: bool=false}` |
| Валидация | `name` 1..64; `color` regex `^#[0-9A-Fa-f]{6}$`; `rules` — массив 0..32 элементов; каждый rule.type ∈ enum; `pattern` 1..256. |
| Поведение | Создаёт `tags`-запись с `is_builtin=false`, плюс все rules, всё в одной транзакции. Если `apply_to_existing=true` — после insert выполняет bulk INSERT в `message_tags` (см. ADR-0017 §7). |
| Rate-limit | 30 / час per user. |
| 201 | объект как в `GET /api/tags` (с `applied_count: int` если `apply_to_existing=true`, иначе 0). |
| 409 | `conflict` (`field=name`) — у пользователя уже есть тег с таким именем. |
| 422 | `tag_apply_too_many` — у пользователя >100 000 messages, а apply_to_existing=true (см. ADR-0017 §7). |

##### Form-encoded request (no-JS)
```
POST /api/tags HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

name=My+Tag&color=%232563eb&rule_type[]=subject_contains&rule_pattern[]=hello&rule_type[]=body_contains&rule_pattern[]=world&apply_to_existing=on&csrf_token=...
```
- Multi-value rules: парные массивы `rule_type[]` и `rule_pattern[]`. Элементы соответствуют по индексу. Парсер: `zip(form.getlist('rule_type'), form.getlist('rule_pattern'))` → отбросить пустые pairs (оба поля пустые) → остальные validate.
- Если число `rule_type[]` не совпадает с `rule_pattern[]` → `validation_error` с пояснением.
- Чекбокс `apply_to_existing`: значение `on`/`true`/`1` → true; отсутствие поля → false.

##### Form-encoded response
- Success: `303 See Other`, `Location: /tags`, flash="Тег создан" (или "Тег создан, применён к {N} письмам" если `apply_to_existing` сработал).
- Validation/conflict error: re-render `tags/form.html` (create-вариант) с error-context (значения сохранены, ошибки рядом с полями).
- `tag_apply_too_many`: re-render с flash "У вас слишком много писем (>100k). Создайте тег без применения к существующим — он сработает на новые".

#### `GET /api/tags/{id}`
| 200 | объект как в list (один). |
| 404 | если tag не принадлежит пользователю. |

#### `PATCH /api/tags/{id}`
| Запрос | `{name?: str, color?: str}` — любое подмножество. Rules — отдельные endpoints. |
| Поведение | Запрещено менять `is_builtin`. Для builtin тегов name/color редактируемы. |
| 200 | объект. |
| 404 | если tag не принадлежит пользователю. |
| 409 | `conflict` (`field=name`) если переименование пересекается с другим тегом этого пользователя. |

##### Form-encoded request (no-JS)
Через method override:
```
POST /api/tags/42 HTTP/1.1
Content-Type: application/x-www-form-urlencoded

_method=PATCH&name=New+Name&color=%23dc2626&csrf_token=...
```

##### Form-encoded response
- Success: `303`, `Location: /tags`, flash="Тег обновлён".
- Validation/conflict: re-render `tags/form.html` (edit-вариант).

#### `DELETE /api/tags/{id}`
| Поведение | CASCADE-удаление в БД (`tag_rules`, `message_tags`). |
| 204 | success. |
| 400 | `cannot_delete_builtin_tag` если `is_builtin=true`. |
| 404 | если не принадлежит пользователю. |

##### Form-encoded request (no-JS)
```
POST /api/tags/42/delete HTTP/1.1
Content-Type: application/x-www-form-urlencoded

_method=DELETE&csrf_token=...
```

##### Form-encoded response
- Success: `303`, `Location: /tags`, flash="Тег удалён".
- `cannot_delete_builtin_tag`: re-render `tags/list.html` с flash-error.

#### `GET /api/tags/{id}/rules`
| 200 | `[{id, type, pattern, created_at}]` |
| 404 | если tag не принадлежит пользователю. |

#### `POST /api/tags/{id}/rules`
| Запрос (JSON) | `{type: str, pattern: str}` |
| Валидация | `type` ∈ enum; `pattern` 1..256. |
| 201 | `{id, type, pattern, created_at}` |
| 404 | если tag не принадлежит пользователю. |
| Note | После добавления rule нового apply к существующим письмам автоматически НЕ происходит. Чтобы применить — пользователь жмёт "Применить к существующим" на странице тега (см. `POST /api/tags/{id}/apply-to-existing`). На новые письма rule сработает в следующем sync. |

##### Form-encoded request (no-JS)
```
POST /api/tags/42/rules HTTP/1.1
Content-Type: application/x-www-form-urlencoded

type=subject_contains&pattern=Hello&csrf_token=...
```

##### Form-encoded response
- Success: `303`, `Location: /tags/42/edit`, flash="Правило добавлено".

#### `DELETE /api/tags/{id}/rules/{rule_id}`
| Поведение | DELETE rule. Существующие `message_tags` (которые могли быть прикреплены этим rule) **остаются** — backend не пытается «откатить» прошлые применения, потому что не отслеживает, какой rule сработал. UI документирует это: "Удаление правила не снимает уже прикреплённые теги; чтобы пересобрать — удалите тег и создайте заново". |
| 204 | success. |
| 404 | если rule не принадлежит этому tag'у или tag не пользователю. |

##### Form-encoded request (no-JS)
```
POST /api/tags/42/rules/7/delete HTTP/1.1
Content-Type: application/x-www-form-urlencoded

_method=DELETE&csrf_token=...
```

##### Form-encoded response
- Success: `303`, `Location: /tags/42/edit`, flash="Правило удалено".

#### `POST /api/tags/{id}/apply-to-existing`
| Поведение | Synchronously запускает bulk INSERT в `message_tags` для всех messages пользователя, попадающих под текущие rules тега (см. ADR-0017 §7). |
| Rate-limit | 50 / час per user (защита от abuse частыми запросами; тяжёлые сканы дополнительно отсекаются runaway-guard `tag_apply_too_many` при >100 000 messages, см. ADR-0017 §7). |
| 200 | `{applied_count: int}` — число новых линков (т.е. без учёта тех, что уже были, ON CONFLICT DO NOTHING). |
| 404 | если tag не принадлежит пользователю. |
| 422 | `tag_apply_too_many` если у пользователя > 100 000 messages. |

##### Form-encoded request (no-JS)
```
POST /api/tags/42/apply-to-existing HTTP/1.1
Content-Type: application/x-www-form-urlencoded

csrf_token=...
```

##### Form-encoded response
- Success: `303`, `Location: /tags`, flash="Применено к {N} письмам".
- `tag_apply_too_many`: re-render `tags/list.html` с flash-error.

---

### Self

#### `GET /api/me`
| 200 | `{id, username, display_name, role, group: {id, name}\|null, last_login_at, mail_accounts_count, tg_notifications_enabled: bool, telegram_linked: bool, telegram_links_count: int}` |
| Note | Поле `is_admin` **удалено** в пользу `role` (см. ADR-0019). Frontend использует `role === 'super_admin'` для проверки админских прав. Для UX-помощи возвращается inline-объект `group` (id+name) — чтобы избежать второго запроса. Поле `tg_notifications_enabled` (ADR-0022 §2.7) — `COALESCE(users_settings.tg_notifications_enabled, true)`, default `true` если в `users_settings` нет строки. Поле `telegram_linked` — `EXISTS(SELECT 1 FROM telegram_links WHERE user_id=me AND dead_at IS NULL)`. **ADR-0024:** `telegram_links_count` — число живых привязок (`COUNT WHERE dead_at IS NULL`); список — отдельным `GET /api/telegram/links`. |

#### `PATCH /api/me/settings` (ADR-0022 §2.7)
| Запрос | `{tg_notifications_enabled?: bool}` — любое подмножество. На текущей итерации поддерживается только `tg_notifications_enabled`; в будущем добавятся другие preferences. |
| Поведение | Upsert в `users_settings` (`INSERT … ON CONFLICT (user_id) DO UPDATE SET tg_notifications_enabled=EXCLUDED.tg_notifications_enabled, updated_at=now()`). |
| Область opt-out | `tg_notifications_enabled=false` подавляет **и** push-уведомления о письмах (ADR-0022 §2), **и** алерты о нерабочей почте (ADR-0033) — оба канала используют один и тот же предикат получателей с `COALESCE(us.tg_notifications_enabled, true)=true`. Отдельного тумблера для mailbox-down-алертов нет. |
| Доступ | user-сессия (любая роль). |
| CSRF | yes. |
| 200 | `{tg_notifications_enabled: bool}` — итоговое значение. |
| 400 | `validation_error` если поле не bool. |

##### Form-encoded request (no-JS) — не требуется на MVP

UI toggle отложен (см. ADR-0022 Open question Q-002-1). API endpoint реализуется в этом спринте; form-fallback добавится в следующем sprint вместе с UI.

#### `GET /api/my/groups` (ADR-0031 — источник списка команд для селектора)

Лёгкий read-only endpoint: список команд, доступных текущему пользователю как **целевые** для селектора команды mail-аккаунта (форма добавления / действие «Сменить команду»).

| | |
| --- | --- |
| Доступ | user-сессия (любая роль). |
| Поведение | `group_member` / `group_leader` — команды пользователя из `scope.group_ids` (домашняя + дополнительные членства, ADR-0030). `super_admin` — **все** существующие группы (flat-метод `GroupsRepo.list_all_groups()`; paginated `list_paginated` для admin-списка не подходит). Переиспользует `GroupsRepo` (`05-modules.md` §6), без дублирования групп-логики и без нового сервиса. |
| 200 | `{groups: [{id, name}], home_group_id: int\|null}` — `groups` отсортирован по `name`; `home_group_id` = `users.group_id` текущего пользователя (для предвыбора default-опции в селекторе; `null` для super_admin). |
| Note | `super_admin`/лидер уже имеют admin-only `GET /api/admin/groups` (тяжёлый: `members_count`, leader). `GET /api/my/groups` — user-scope, доступен всем ролям, отдаёт минимум. Для UX фронт у `super_admin` дополнительно показывает опцию «Без команды» (`group_id=null`); этот пункт не приходит из API — он чисто фронтовый. |

CSRF не требуется (GET). Rate-limit не требуется (лёгкий read, объёмы ≤ 5 групп).

---

## 4. Admin API

> **⚠️ ИСТОРИЧЕСКИЙ РАЗДЕЛ — весь `/api/admin/*` вместе с админкой и таблицами `groups`/`user_groups`/`admin_audit` СНЯТО демонтажём ([ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md), выполнено на проде 2026-07-15).** Описанное ниже в коде/проде агрегатора НЕ существует; текст сохранён как record исходного решения. НЕ реализовывать и не принимать за действующий контракт. **Актуально:** админ-роутер не смонтирован (`backend/app/main.py:99-100`), пользователей/ролей/групп нет — единственный ряд `users` = `crm-service`.

Префикс `/admin` (HTML) и `/api/admin` (JSON). Требует session.is_admin=true.

### Pages

| Метод | Путь | Описание |
| --- | --- | --- |
| GET | `/admin` | Дашборд: список пользователей с колонками `username`, `display_name`, `role`, `group`, `last_login`, mail_accounts. |
| GET | `/admin/audit` | Audit log (paginated, default 50/page). |
| GET | `/admin/groups` | Список групп: `name`, `leader (display_name\|username)`, `members_count`. Кнопки `[+ Создать группу]`, на каждой строке — `[Изменить]`, `[Удалить]`. |
| GET | `/admin/groups/new` | Форма создания группы: `name`, `leader_user_id` (select из существующих group_member пользователей). |
| GET | `/admin/groups/{id}/edit` | Форма редактирования группы: `name` (rename) + список участников (read-only). Удалить → отдельная кнопка с confirm. |

### JSON

#### `GET /api/admin/users`
| Query | `q?` (search by username substring), `page?`, `limit?` (default 50, max 200) |
| 200 | `{items: [{id, username, email, is_admin, password_reset_required, has_password, lockout_until, last_login_at, created_at, mail_accounts: [{id, email, is_active, last_synced_at, last_sync_error}]}], total, page, limit}` |
| Note | **ADR-0038:** `has_password: bool` = `users.password_encrypted IS NOT NULL` — управляет отображением колонки «Пароль» (`true` → маска + reveal-кнопка; `false` → «—»). **Сам пароль в листинге НЕ отдаётся** — только по требованию через `GET /api/admin/users/{id}/password`. |

#### `POST /api/admin/users`
| Запрос | `{username: str (3..64, [A-Za-z0-9_.-]), email?: str, display_name?: str (1..100), role: 'group_leader'\|'group_member' (DEFAULT 'group_member'), group_id?: int, password?: str\|null (ADR-0038), additional_group_ids?: list[int] (ADR-0038/ADR-0030)}` |
| Поведение | Создаёт пользователя. Логика по ролям (см. ADR-0019 §5): <br>— `role='group_leader'`: `group_id` **должен быть пуст** (иначе `400 group_id_must_be_null_for_new_leader`); backend в одной транзакции (1) INSERT users без group_id, (2) INSERT groups с `name='Группа {display_name\|username}'` и `leader_user_id=user.id`, (3) UPDATE users.group_id. Audit: `create_user` + `group_create`. <br>— `role='group_member'`: `group_id` **обязателен** (existing group). Backend проверяет существование. Audit: `create_user`. <br>— `role` не передан — default `group_member`; `group_id` обязателен. <br>— Создание `super_admin` через API **запрещено**: super-admin создаётся только через seed (ADR-0019 §1). <br>**`password` (ADR-0038 §3):** опционально. <br>— **Задан** (12..128, ≥1 буква + ≥1 цифра) → backend пишет `password_hash` (argon2id) **И** `password_encrypted` (AES-GCM, AAD `user_pw:{new_id}`), `password_reset_required=false`. Дополнительный audit `user_password_set`. <br>— **Пуст/отсутствует** → прежний self-set-флоу: `password_hash=NULL`, `password_reset_required=true`, `password_encrypted=NULL` (колонка «—»). Полная обратная совместимость. <br>**`additional_group_ids` (ADR-0038 §5, опирается на ADR-0030):** опционально; **только для `role='group_member'`**. Список доп. команд сверх домашней `group_id`. В **той же транзакции** после INSERT users + домашнего членства (`user_groups` для `group_id`) вставляются `user_groups` для каждой доп. команды. Дедуп с домашней и между собой (`ON CONFLICT (user_id, group_id) DO NOTHING`); валидация существования каждой (несуществующая → `400 group_not_found`). Для `group_leader` / отсутствия — игнорируется (`super_admin` через API не создаётся). Audit `user_group_add` на каждое реально добавленное доп. членство. |
| Доступ | Только `super_admin`. Лидеры/участники → 403. |
| 201 | `{id, username, email, display_name, role, group_id, group: {id, name}\|null, has_password: bool}` (`has_password = password_encrypted IS NOT NULL`; **сам пароль в ответ не кладётся**). |
| 409 | `conflict` (`field=username`). |
| 400 | `validation_error` (в т.ч. слабый `password`) / `group_id_must_be_null_for_new_leader` / `group_not_found`. |

##### Form-encoded request (no-JS)
```
POST /api/admin/users HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

username=bob&email=bob%40example.com&display_name=Bob+Smith&role=group_member&group_id=3&password=Secret12word&additional_group_ids=4&additional_group_ids=5&csrf_token=...
```
Пустое `email=` или `display_name=` интерпретируются как `null`. `group_id=` пустое → `null` (валидно только для `role=group_leader`). **`password=`** (ADR-0038): пустое → self-set-флоу (`password_encrypted=NULL`, «—»); непустое → admin-set (хеш + обратимая копия). **`additional_group_ids`** (ADR-0038 §5): повторяющиеся поля формы (`additional_group_ids=4&additional_group_ids=5`) → `form.getlist('additional_group_ids')`; только для `role='group_member'`; дедуп с `group_id`.

##### Form-encoded response
- Success: `303 See Other`, `Location: /admin`, flash="Пользователь создан".
- Validation/conflict error: re-render `admin/users.html` (с открытой формой создания) с error-context (значения сохранены).

#### `PATCH /api/admin/users/{id}`
| Запрос | `{display_name?: str\|null, role?: 'group_leader'\|'group_member', group_id?: int\|null}` (любое подмножество). |
| Поведение | Изменение полей пользователя через super-admin. <br>— `display_name`: trim → `null` если пусто. <br>— Смена `role` от/к `group_leader`: complex flow. (а) `group_member → group_leader`: backend требует, чтобы текущая группа user'а **не имела другого лидера**; иначе `400 conflict` (нужно сначала переразмерить старого лидера). Чтобы создать **новую группу** для лидера — клиент отдельно вызывает `POST /api/admin/groups` или передаёт `role='group_leader' + group_id=null` (тогда backend auto-create'ит группу как при POST users). (б) `group_leader → group_member`: тоже complex — у группы остаётся без лидера; backend требует, чтобы клиент **сначала** удалил/назначил нового лидера через переход на `PATCH /api/admin/users` с другого user'а (или удалил группу через DELETE). На текущем scope: `400 cannot_demote_lone_leader` если лидер единственный в группе. <br>— Смена `group_id` без смены role — **«Переместить»** (move, ADR-0030): переводит user'а в другую **домашнюю** команду (только для `group_member`). Помимо `users.group_id` backend **синхронизирует `user_groups`** в той же транзакции: удаляет старое домашнее членство, добавляет новое; **дополнительные** членства (ADR-0030) не трогаются; если новая домашняя совпала с уже существующим доп. членством — дедуп (`INSERT ... ON CONFLICT DO NOTHING`). **Перенос для `role='group_leader'` отклоняется** → `409 cannot_move_group_leader` (нарушил бы инвариант лидера; в UI пункт «Переместить» для лидеров скрыт). <br>— Изменение `role` к `super_admin` или с `super_admin` — **запрещено** (`400 forbidden`); super-admin один и определяется seed'ом. <br>— Все сессии target user'а **revoke'аются** (см. ADR-0019 §10) — чтобы `VisibilityScope.group_ids` перечитался из `user_groups`. |
| Доступ | Только `super_admin`. |
| 200 | `{id, username, ..., role, group_id, group: {id, name}\|null}`. |
| 400 | `validation_error` / `group_id_must_be_null_for_new_leader` / `cannot_demote_lone_leader` / `forbidden`. |
| 404 | `not_found` если user не существует. |
| 409 | `cannot_move_group_leader` (move для `group_leader`, ADR-0030). |
| Audit | `user_role_change` если role изменился; `user_group_change` если только group_id (move, ADR-0030 — синхронизация `user_groups` входит в ту же операцию); обе — если оба. `group_create` дополнительно если auto-create группы для нового лидера. |

##### Form-encoded request (no-JS)
Через method override:
```
POST /api/admin/users/42 HTTP/1.1
Content-Type: application/x-www-form-urlencoded

_method=PATCH&display_name=Alice&role=group_leader&csrf_token=...
```

##### Form-encoded response
- Success: `303`, `Location: /admin`, flash="Пользователь обновлён".
- Error: re-render `admin/users.html` с error-context.

#### `POST /api/admin/users/{user_id}/groups` (add membership, ADR-0030)

Добавить пользователю **дополнительное** членство в команде (multi-group). Источник истины — [ADR-0030](./adr/ADR-0030-multi-group-membership.md).

| | |
| --- | --- |
| Запрос | `{group_id: int}` |
| Поведение | INSERT в `user_groups(user_id, group_id)`. **НЕ меняет** `users.group_id` (домашнюю команду) и **НЕ меняет** `users.role`. Идемпотентно через UNIQUE `(user_id, group_id)`: повторное добавление — `409 membership_already_exists` (либо no-op в form-режиме с информирующим flash). Backend проверяет существование `group_id` (`404 group_not_found`). После INSERT — `SessionStore.revoke_all_for_user(user_id)` (чтобы `VisibilityScope.group_ids` перечитался). |
| Доступ | Только `super_admin`. Лидеры/участники → `403 forbidden`. |
| Цель | **Не может быть `super_admin`** → `400 cannot_add_super_admin_to_group` (он и так видит всё; членства нарушили бы инвариант `super_admin → group_id IS NULL` и «нет строк в `user_groups`»). Лидер и обычный участник — допустимы (лидер получает доп. членство, не теряя лидерства домашней команды). |
| CSRF | yes (как у всех admin POST). |
| 201 | `{user_id, group_id, group: {id, name}, created_at}` — созданное членство. |
| 400 | `validation_error` / `cannot_add_super_admin_to_group`. |
| 404 | `not_found` (user не существует) / `group_not_found`. |
| 409 | `membership_already_exists`. |
| Audit | `user_group_add` (`actor=super_admin`, `target_user_id=user_id`, `details={group_id}`). |

##### Form-encoded request (no-JS)
```
POST /api/admin/users/42/groups HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

group_id=3&csrf_token=...
```

##### Form-encoded response
- Success: `303 See Other`, `Location: /admin`, flash="Пользователь добавлен в команду".
- `membership_already_exists`: re-render `admin/users.html` с информирующим flash (идемпотентность — повтор безвреден).
- Validation/other error: re-render `admin/users.html` с error-context.

#### `DELETE /api/admin/users/{user_id}/groups/{group_id}` (remove membership, ADR-0030)

Удалить у пользователя **дополнительное** членство в команде. Источник истины — [ADR-0030](./adr/ADR-0030-multi-group-membership.md).

| | |
| --- | --- |
| Поведение | DELETE строки `user_groups(user_id, group_id)`. Нельзя удалить **домашнее** членство (`group_id == users.group_id`) → `400 cannot_remove_home_membership` (для смены домашней команды служит «Переместить», `PATCH /api/admin/users/{id}`). После DELETE — `SessionStore.revoke_all_for_user(user_id)`. |
| Доступ | Только `super_admin`. Лидеры/участники → `403 forbidden`. |
| CSRF | yes. |
| 204 | success. |
| 400 | `cannot_remove_home_membership`. |
| 404 | `not_found` (user не существует) / `membership_not_found` (нет такого доп. членства). |
| Audit | `user_group_remove` (`actor=super_admin`, `target_user_id=user_id`, `details={group_id}`). |

##### Form-encoded request (no-JS)
Через method override на sibling-роуте:
```
POST /api/admin/users/42/groups/3/delete HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

_method=DELETE&csrf_token=...
```

##### Form-encoded response
- Success: `303 See Other`, `Location: /admin`, flash="Членство в команде удалено".
- `cannot_remove_home_membership`: re-render `admin/users.html` с error-context.

#### `POST /api/admin/users/{id}/reset`
| Запрос | `{password?: str\|null}` (ADR-0038) — опционально. |
| Поведение | **`password` пуст/отсутствует** (текущее поведение): UPDATE `password_hash=NULL`, `password_encrypted=NULL`, `password_reset_required=true`, `lockout_until=NULL`, `failed_login_attempts=0`; revoke all sessions; audit `reset_password`. Колонка «Пароль» после этого — «—» (пока пользователь не задаст пароль сам через self-set или админ не задаст `password`). <br>**`password` задан** (ADR-0038 §3; 12..128, ≥1 буква + ≥1 цифра): UPDATE `password_hash`=argon2(pw) **И** `password_encrypted`=AES-GCM(pw, AAD `user_pw:{id}`), `password_reset_required=false`, `lockout_until=NULL`, `failed_login_attempts=0`; revoke all sessions; audit `reset_password` + `user_password_set`. Колонка «Пароль» показывает заданное значение. <br>В обоих случаях также `DELETE FROM telegram_links WHERE user_id=:id` (все привязки, ADR-0024 — см. Redis-таблицу ниже). |
| 200 | `{ok: true}` |
| 400 | если `id` совпадает с супер-админом — отказ (`code=cannot_reset_admin`); слабый `password` → `validation_error`. |

##### Form-encoded request (no-JS)
```
POST /api/admin/users/42/reset HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

password=Secret12word&csrf_token=...
```
`password=` пустое → force-self-set (текущее поведение, «—»); непустое → admin-set (хеш + обратимая копия).

##### Form-encoded response
- Success: `303 See Other`, `Location: /admin`, flash="Пароль сброшен".
- Error (`cannot_reset_admin` / слабый `password`): re-render `admin/users.html` с error-context.

#### `GET /api/admin/users/{id}/password` (ADR-0038 — показ пароля входа)

Возвращает расшифрованный пароль входа оператора для показа в колонке «Пароль» на `/admin`. **Read-only, НЕ мутация** — method-override не применяется, нового form-fallback-пути нет (см. примечание ADR-0038 в секции «Form-encoded fallback»).

| | |
| --- | --- |
| Доступ | **Только `super_admin`.** Лидеры/участники / обычные user → `403 forbidden`. |
| Поведение | Читает `users.password_encrypted`, расшифровывает под AAD `user_pw:{id}` (`decrypt_user_password`), возвращает plaintext. `password_encrypted IS NULL` → `404 password_not_set` (колонка «—»). Несуществующий user → `404 not_found`. |
| Rate-limit | `LIMIT_ADMIN_PASSWORD_REVEAL` (env `ADMIN_PASSWORD_REVEAL_RATE_LIMIT_PER_MINUTE`, `int`, default `30`, `ge=1`) — **per-actor** (по `super_admin` `user_id`). Анти-bulk-exfiltration + защита от заспамливания audit. 429 → `Retry-After`. |
| Audit | `user_password_revealed` на **каждый** успешный показ (`actor=super_admin`, `target_user_id={id}`, `details={}` — **без значения пароля**). |
| CSRF | не требуется (GET). |
| Логирование | Значение пароля **не логируется** (ни structlog, ни audit.details, ни access-log); response этого endpoint исключён из request/response-логирования — только статус-код. |
| 200 | `{"password": "<plaintext>"}`. |
| 403 | `forbidden` (не super_admin). |
| 404 | `password_not_set` (нет обратимой копии) / `not_found` (нет user). |
| 429 | `rate_limited` (+`Retry-After`). |

UI грузит значение **on-demand** по клику на маску (`08-frontend.md` §4.8) — пароль не встраивается в разметку списка заранее (минимальная экспозиция + audit на каждый показ).

#### `DELETE /api/admin/users/{id}`
| Поведение | См. S7 в `01-architecture.md`. CASCADE delete + MinIO cleanup + revoke sessions + audit log. |
| 200 | `{ok: true, deleted_attachments: N, deleted_messages: M, deleted_mail_accounts: K}` |
| 400 | если `id` совпадает с супер-админом — отказ (`code=cannot_delete_admin`). |

##### Form-encoded request (no-JS)
Через method override на sibling-роуте:
```
POST /api/admin/users/42/delete HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

_method=DELETE&csrf_token=...
```

##### Form-encoded response
- Success: `303 See Other`, `Location: /admin`, flash="Пользователь удалён".
- Error (`cannot_delete_admin`): re-render `admin/users.html` с error-context.

#### `GET /api/admin/audit`
| Query | `page?`, `limit?` (default 50, max 200), `action?`, `target_user_id?`, `from?` (ISO date), `to?` |
| 200 | `{items: [{id, actor_user_id, action, target_user_id, target_username, details, ip, created_at}], total, page, limit}` |

### Groups (admin-only, ADR-0019)

Все endpoints — только для `super_admin`. Лидер / участник → 403.

#### `GET /api/admin/groups`
| Query | `q?` (search by name substring), `page?`, `limit?` (default 50, max 200) |
| 200 | `{items: [{id, name, leader: {id, username, display_name}, members_count: int, created_at}], total, page, limit}` |
| Описание | List всех групп с краткой информацией о лидере и числе участников. Объёмы малые (≤ 5 групп), пагинация формальная. |

#### `POST /api/admin/groups`
| Запрос | `{name: str (1..100), leader_user_id: int}` |
| Поведение | Создаёт пустую группу с указанным лидером. Лидер должен существовать и иметь `role='group_member'` (или `'group_leader'` без группы — что невозможно по инвариантам, поэтому фактически только `group_member`). Backend в одной транзакции: (1) INSERT groups, (2) UPDATE users SET role='group_leader', group_id=:new_group_id WHERE id=:leader_user_id. Все сессии лидера revoke'аются. <br>На практике этот endpoint редко используется напрямую — обычно группа создаётся auto через `POST /api/admin/users role='group_leader'`. Manual create нужен для (а) переноса существующего user'а в роль лидера новой группы, (б) reorganization. |
| Доступ | super_admin. |
| 201 | `{id, name, leader: {id, username, display_name}, members_count: 1, created_at}`. |
| 400 | `validation_error` (некорректный name); `forbidden` если `leader_user_id` уже лидер другой группы или super_admin; `not_found` если user не существует. |
| 409 | `conflict` если у user'а уже есть группа в качестве лидера (UNIQUE `groups.leader_user_id`). |
| Audit | `group_create` (`actor=super_admin, target_user_id=leader_user_id, details={group_id, group_name, auto_created: false}`) + `user_role_change`. |

##### Form-encoded request (no-JS)
```
POST /api/admin/groups HTTP/1.1
Content-Type: application/x-www-form-urlencoded

name=Группа+Apple&leader_user_id=42&csrf_token=...
```

##### Form-encoded response
- Success: `303`, `Location: /admin/groups`, flash="Группа создана".
- Error: re-render `admin/groups/form.html` с error-context.

#### `GET /api/admin/groups/{id}`
| 200 | `{id, name, leader: {id, username, display_name}, members: [{id, username, display_name, role}], created_at}`. Включает полный список участников (включая лидера). |
| 404 | `group_not_found`. |

#### `PATCH /api/admin/groups/{id}`
| Запрос | `{name?: str (1..100)}`. На старте редактируется только имя. |
| 200 | объект (как в `GET /api/admin/groups/{id}`). |
| 404 | `group_not_found`. |
| 400 | `validation_error`. |
| Audit | `group_rename` (`actor, target=leader, details={group_id, from_name, to_name}`). |

##### Form-encoded request (no-JS)
Через method override:
```
POST /api/admin/groups/3 HTTP/1.1
Content-Type: application/x-www-form-urlencoded

_method=PATCH&name=Новое+имя+группы&csrf_token=...
```

##### Form-encoded response
- Success: `303`, `Location: /admin/groups`, flash="Группа переименована".

#### `DELETE /api/admin/groups/{id}`
| Поведение | Удаление пустой группы. Backend проверяет, что в группе нет участников (`SELECT 1 FROM users WHERE group_id=:id`); если есть — `400 group_has_members` с `details.members_count`. UI заранее показывает список участников и просит super-admin'а перевести их в другую группу или удалить. После удаления `users.group_id` бывшего лидера автоматически становится `NULL` (FK ON DELETE SET NULL). Backend ОБЯЗАН **перед** `DELETE FROM groups`: (1) UPDATE leader's `role='group_member'` и `group_id=:another_group_id` (нужна явная новая группа от super-admin'а — лидер не может остаться без группы); либо (2) DELETE leader user. На практике: **DELETE группы запрещён, пока в ней есть участники И лидер**. Super-admin делает: (а) перевести/удалить всех group_member, (б) перевести/удалить лидера в другую группу или удалить user'а лидера, (в) тогда `DELETE` пустой группы (без участников и без лидера-FK ссылки) выполняется. |
| Доступ | super_admin. |
| 204 | success. |
| 400 | `group_has_members` (`details.members_count`). |
| 404 | `group_not_found`. |
| Audit | `group_delete` (`actor, target_user_id=null, details={group_id, group_name}`). |

##### Form-encoded request (no-JS)
```
POST /api/admin/groups/3/delete HTTP/1.1
Content-Type: application/x-www-form-urlencoded

_method=DELETE&csrf_token=...
```

##### Form-encoded response
- Success: `303`, `Location: /admin/groups`, flash="Группа удалена".
- Error: re-render `admin/groups/list.html` с error-context.

---

## 4a. Telegram webhook + Persistent SSO + Push-нотификации

> **⚠️ ИСТОРИЧЕСКИЙ РАЗДЕЛ — вся Telegram-подсистема (ADR-0018/0022/0024/0027/0033) СНЯТО демонтажём ([ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md), выполнено на проде 2026-07-15).** Описанное ниже в коде/проде агрегатора НЕ существует; текст сохранён как record исходного решения. НЕ реализовывать и не принимать за действующий контракт. **Актуально:** `telegram_router` не смонтирован; Telegram целиком в CRM (CRM `ADR-044` §6).

Источники истины — [ADR-0018](./adr/ADR-0018-telegram-launcher.md) (launcher) + [ADR-0022](./adr/ADR-0022-telegram-sso-and-notifications.md) (persistent SSO + push-нотификации; partially supersedes ADR-0018).

- **Бот-launcher (ADR-0018)**: `/start` отдаёт inline-keyboard с WebApp-кнопкой на основной URL сервиса.
- **Persistent SSO (ADR-0022 §1)**: открытие WebApp с активной `telegram_links` записью даёт автоматический login без повторного ввода username+password. Реализуется отдельным эндпоинтом `POST /api/telegram/auth` (см. ниже), который валидирует подписанный Telegram `init_data` (HMAC-SHA256 + auth_date TTL 5 мин) и при наличии линковки выпускает session-cookie.
- **Push-нотификации (ADR-0022 §2)**: после ingest новых писем worker диспатчит `sendMessage` в Telegram всем получателям, у которых: (а) есть активная линковка, (б) право видеть письмо (super_admin/group/owner); при `TG_NOTIFY_ALL_MESSAGES=false` дополнительно требуется наличие тега, при `true` (default, round-31) — уведомление по всем письмам, (в) включены уведомления (`users_settings.tg_notifications_enabled = true`). Доставка под per-chat throttle `TG_SEND_PER_CHAT_PER_MINUTE` (§2.9).

### `POST /api/telegram/webhook/{secret}`

| | |
| --- | --- |
| Доступ | публичный, но защищён двойной проверкой secret. |
| Авторизация | (1) `{secret}` в URL-path должен совпадать с env `TELEGRAM_WEBHOOK_SECRET`; (2) header `X-Telegram-Bot-Api-Secret-Token` должен совпадать с тем же значением. Несовпадение любого — `403 forbidden`, лог `event=telegram_webhook_invalid_secret`, без обработки body. |
| Запрос | `application/json`, тело — Telegram [`Update`](https://core.telegram.org/bots/api#update). Обрабатываются только поля `message.chat.id`, `message.text`, `message.from.id`. Остальные поля игнорируются (forward-совместимость). |
| Поведение | Если `message.text` начинается с `/start` — backend асинхронно отправляет `sendMessage` в Bot API с inline-keyboard `[[{text: "Open Mail Aggregator", web_app: {url: TELEGRAM_WEBAPP_URL}}]]`. Если с `/help` — отправляет краткое "Send /start to open the app". Все остальные апдейты (произвольный текст, callback_query, edited_message и т.п.) — игнорируются. |
| CSRF | exempt (нет user-сессии и невозможно — Telegram не шлёт cookies). |
| Rate-limit | 60/min per IP (защита от шквала forwarded-update'ов; нормальная нагрузка — десятки апдейтов в день). |
| 200 | пустое тело (Telegram игнорирует body, важен только статус-код). Возвращается **всегда** при валидном secret — даже если апдейт не распознан, чтобы Telegram не ретраил. |
| 403 | `forbidden` (secret invalid). |
| 429 | `rate_limited` (превышен лимит). |
| 503 | `dependency_unavailable` (если бот включён, но `httpx`-вызов на api.telegram.org упал — возвращаем 503, чтобы Telegram повторил позже; обычно это transient). |

Если env `TELEGRAM_BOT_ENABLED=false`, endpoint регистрируется и валидирует secret, но `sendMessage` не отправляет — просто 200 OK. Это упрощает запуск окружений без bot-настройки (CI, dev без BotFather).

### `POST /api/telegram/push-webhook/{bot_name}` (ADR-0027 round-42 §10)

Webhook push-only бота по команде. Принимает **только** `callback_query` от кнопки «Посмотреть сообщение»; не launcher (никаких `/start`/`/help`).

| | |
| --- | --- |
| Доступ | публичный, защищён per-бот secret. |
| `bot_name` | path-параметр ∈ `{ivan, alexandra, andrei, business2}` (стабильный `PushTeamBot.name`; round-44 добавил `business2`). Несуществующий / не настроенный (нет `BOT_{NAME}_WEBHOOK_SECRET`) → `not_found` (404-эквивалент, неперечислимо — STRIDE-S; до обработки body). |
| Авторизация (webhook) | header `X-Telegram-Bot-Api-Secret-Token` должен совпадать с env `BOT_{NAME}_WEBHOOK_SECRET` (constant-time, `secrets.compare_digest`). Несовпадение/отсутствие нужного secret → `not_found`, лог `event=push_webhook_invalid_secret`, без обработки body. (Транспорт secret — header-only; path-вариант симметрии с основным допустим — Q-0027-1, non-blocking.) |
| Запрос | `application/json`, тело — Telegram `Update`. Обрабатывается **только** `callback_query` (`id`, `from.id`, `data`, `message.chat.id`). Любой другой update (`message`/`/start`/`edited_message`/…) → лог `push_webhook_ignored_non_callback` + 200 (push-боты не принимают inbound-команд). |
| Поведение callback | `callback_data` обязан матчить `^msg:(\d+)$` (reuse `_CALLBACK_PATTERN`). Авторизация нажавшего: `from.id` ∈ `ADMIN_TELEGRAM_IDS` (НЕ через `telegram_links`/visibility — отдельный push-путь). DEFENSIVE group-match: загруженное письмо должно принадлежать группе этого бота (`mail_accounts.group_id == BOT_{NAME}_GROUP_ID`). При успехе — тело письма (`_format_message_body` + `_split_for_telegram`, sanitize round-39/41) шлётся в чат **токеном этого бота** + `answerCallbackQuery`. |
| CSRF | exempt (нет user-сессии; Telegram не шлёт cookies). Маршрут добавляется в CSRF-exempt webhook-префикс рядом с основным. |
| Rate-limit | 60/min per IP (тот же `_LIMIT_TG_WEBHOOK`, до secret-проверки). |
| 200 | всегда при валидном secret — даже если update не callback / письмо недоступно / нажавший не админ (Telegram не ретраит). Пользовательская обратная связь — через `answerCallbackQuery` (toast/alert), не через HTTP-код. |
| 404 | `not_found` (secret invalid / бот не настроен / неизвестный `bot_name`). |
| 429 | `rate_limited`. |

Сценарии callback (видны пользователю как `answerCallbackQuery`):

| Условие | Ответ нажавшему |
| --- | --- |
| `from.id` ∉ `ADMIN_TELEGRAM_IDS` | «Нет доступа.» (`show_alert`), тело не показывается. |
| `account.group_id != BOT_{NAME}_GROUP_ID` (подделка id) | «Сообщение недоступно.», тело не показывается, лог `push_callback_group_mismatch`. |
| письмо/аккаунт удалены (retention) | «Сообщение больше не доступно.» |
| `callback_data` не `msg:{digits}` | «Неподдерживаемое действие.» |
| успех | тело письма в чат + silent ack. |

### `POST /api/telegram/auth` (ADR-0022 §1)

Persistent SSO endpoint: принимает Telegram WebApp `init_data`, валидирует HMAC, ищет линковку и либо выпускает session-cookie, либо ставит pending-cookie для последующей линковки после ручного login.

| | |
| --- | --- |
| Доступ | публичный |
| CSRF | exempt (нет session при first call; защита — HMAC + TTL) |
| Запрос | `application/json`, тело: `{"init_data": "<raw initData string from Telegram.WebApp.initData>"}`. `init_data` — строка 1..4096 chars. |
| Валидация init_data | (1) Parse как URL-encoded; (2) извлечь `hash`; (3) `data_check_string = "\n".join(sorted(k=v for non-hash keys))`; (4) `secret_key = HMAC_SHA256("WebAppData", TELEGRAM_BOT_TOKEN)`; (5) constant-time compare `HMAC_SHA256(secret_key, data_check_string)` vs `hash`; (6) `auth_date` не старше 5 минут (env `TG_AUTH_INIT_DATA_TTL_SEC=300`). Спецификация: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app |
| Rate-limit | 30/min per IP **+** 10/min per `telegram_user_id` (применяется ПОСЛЕ HMAC валидации). |
| Сессия | round-38 (self-heal): backend читает `mas_session` (если есть). При **наличии** валидной сессии endpoint работает в режиме self-heal (см. ниже) вместо SSO. При **отсутствии** — обычный SSO (linked/unlinked). |
| 200 (linked=true) | **Только при отсутствии сессии (аноним) и существующей привязке.** `{"linked": true, "redirect": "/"}` + Set-Cookie `mas_session` (HttpOnly, Secure, SameSite=Lax, sliding 12h) + Set-Cookie `mas_csrf` (не HttpOnly). |
| 200 (linked=false, аноним без привязки) | `{"linked": false, "redirect": "/login"}` + Set-Cookie `mas_tg_pending` (HttpOnly, Secure, SameSite=Lax, **15 минут**) — opaque token указывающий на Redis ключ `tg_pending:{token}` = `{telegram_user_id}`. После успешного `POST /login/password` или `POST /set-password` backend читает cookie, делает upsert в `telegram_links` и удаляет Redis ключ. |
| 200 (healed=true, **есть сессия — round-38 self-heal**) | `{"linked": false, "healed": true}` — **без** `redirect`, **без** Set-Cookie. Backend гарантировал привязку `telegram_links(telegram_user_id → current_session.user_id)` (rebind разрешён — initData доказывает владение TG). **`healed:true` возвращается одинаково и для NO-OP, и для реального восстановления** — пользователю это неотличимо и не должно различаться. **created_at-семантика (critical-fix §1.6 edge-3):** если привязка уже была живой на того же user (`dead_at IS NULL`) → **полный NO-OP** (строка не трогается, `created_at` НЕ сдвигается, audit не пишется) — это предотвращает потерю писем, пришедших в окне между двумя открытиями WebApp (recipient-SQL фильтрует `m.internal_date >= tl.created_at`). `created_at=now()` ставится **только** при реальном восстановлении: INSERT новой / реактивация `dead` / rebound. Вторая сессия **не** создаётся; `mas_tg_pending` **не** выставляется. Фронт `tg.js` по контракту перезагружается только при `linked===true && redirect`. Лимит `TG_MAX_LINKS_PER_USER` — soft: при достижении привязка не создаётся, ответ всё равно `healed:true` (best-effort). При внутренней ошибке — `{"linked": false, "healed": false}`. См. ADR-0022 §1.6. |
| 401 | `invalid_init_data` (HMAC mismatch / парсинг провалился). |
| 401 | `init_data_expired` (`auth_date` старше TTL). |
| 429 | `rate_limited` + `Retry-After`. |
| Audit | при успешной перепривязке (upsert обновил `user_id`) — `telegram_link_created`/`telegram_link_rebound` с `details={telegram_user_id, replaced: true|false, via}`; при достижении лимита `TG_MAX_LINKS_PER_USER` — `telegram_link_limit_reached` (ADR-0024). round-38: self-heal-ветка пишет те же действия с `details.via="self_heal"`. **Action `telegram_link_collision` — deprecated (ADR-0024): больше не пишется, инвариант «один user — один TG» снят.** |
| Side effects | См. ADR-0022 §1.3 (аноним) и §1.6 (self-heal). ADR-0024: `link_pending` применяет мягкий лимит `TG_MAX_LINKS_PER_USER` (default 10). round-38: при наличии сессии вызывается `TelegramSSOService.self_heal_link(...)` (rebind разрешён, best-effort, не поднимает исключений наружу). |

#### Связанные изменения flow

| Endpoint | Изменение от base-логики |
| --- | --- |
| `POST /login/password` | После успешного verify password, если в request есть cookie `mas_tg_pending` — backend читает Redis `tg_pending:{token}` и через `TelegramSSOService.link_pending(...)` гарантирует привязку. **created_at-семантика (round-38 §1.6 edge-3):** если привязка уже была **живой** на того же user (`dead_at IS NULL`) → **NO-OP** (`created_at` НЕ сдвигается, audit не пишется — не теряем письма из окна между upsert'ами). Иначе — `INSERT … ON CONFLICT (telegram_user_id) DO UPDATE SET user_id=EXCLUDED.user_id, created_at=now(), dead_at=NULL` при INSERT новой / реактивации `dead` / rebound; применяется soft-limit `COUNT(active) < TG_MAX_LINKS_PER_USER` (ADR-0024 §3). Удаляет Redis-ключ; clear cookie `mas_tg_pending`. Audit (кроме NO-OP): `telegram_link_created` (новая/реактивация своего TG) или `telegram_link_rebound`; при потолке — `telegram_link_limit_reached`, привязка не создаётся. |
| `POST /set-password` | То же поведение, что и `POST /login/password` — линковка создаётся после успешной установки пароля. |
| `POST /logout` | **(round-43 — ADR-0022 §1.5 / ADR-0024 §5)** Завершает **только** веб-сессию (revoke session + clear cookies + 302 `/login`). `telegram_links` **НЕ** трогаются — вызов `revoke_for_user(reason="logout")` **удалён**; audit `telegram_link_revoked` с `reason="logout"` больше **не** пишется. Push не требует активной веб-сессии (привязка самодостаточна). Отвязка TG — только явной кнопкой «Отвязать» → `DELETE /api/telegram/links/{tg_user_id}` (см. §4b, `reason="user_unlink"`). |
| `POST /api/admin/users/{id}/reset` | Дополнительно (ADR-0024): `DELETE FROM telegram_links WHERE user_id=:id` — **ВСЕ** привязки. Audit: `telegram_link_revoked` с `details={telegram_user_ids: [...], reason: 'password_reset'}`. |
| `DELETE /api/admin/users/{id}` | Каскад: `telegram_links ON DELETE CASCADE` автоматически удалит все rows (отдельный audit не нужен — покрыт `delete_user`). |

#### Управление TG-привязками при активной сессии (ADR-0024 §4)

Cookie-authenticated, CSRF-protected. Позволяют залогиненному пользователю иметь несколько TG-привязок (личный/рабочий) и управлять ими из настроек, без повторного ввода пароля.

##### `GET /api/telegram/links`
| | |
| --- | --- |
| Auth | session cookie. |
| 200 | `{"links": [{"telegram_user_id": int, "created_at": iso8601, "dead": bool}], "max": <TG_MAX_LINKS_PER_USER>}` — все привязки текущего user'а (`dead` = `dead_at IS NOT NULL`). |

##### `POST /api/telegram/links`
| | |
| --- | --- |
| Auth | session cookie + CSRF. |
| Запрос | `{"init_data": "<signed Telegram initData>"}` — initData свежего TG (открытого в нужном OctoBrowser/устройстве). |
| Логика | HMAC-валидация initData (как `/api/telegram/auth`); `TelegramSSOService.link_session_add(...)` привязывает `telegram_user_id` к `session.user_id` (НЕ через pending-flow). Применяет лимит `TG_MAX_LINKS_PER_USER`. **created_at-семантика (round-38 §1.6 edge-3):** уже-живая привязка того же user (`dead_at IS NULL`) → **NO-OP** (`created_at` не сдвигается; ответ 200 как успех); `created_at=now()` — только при INSERT / реактивации `dead`. Rebound на чужой TG здесь **запрещён** → 409 `tg_link_owned_by_other`. |
| 200 | `{"linked": true, "telegram_user_id": int}` (в т.ч. для NO-OP уже-живой привязки — идемпотентно). |
| 401 | `invalid_init_data` / `init_data_expired`. |
| 409 | `tg_link_limit` — достигнут `TG_MAX_LINKS_PER_USER`. Audit `telegram_link_limit_reached`. |
| 409 | `tg_link_owned_by_other` — этот `telegram_user_id` уже привязан к другому user'у (перепривязка из чужого аккаунта запрещена в этом flow; разрешена только через login-flow с паролем). |
| Audit | `telegram_link_created` с `details={telegram_user_id, via: 'session_add'}`. |

##### `DELETE /api/telegram/links/{telegram_user_id}`
| | |
| --- | --- |
| Auth | session cookie + CSRF. |
| Логика | `delete_one(user_id=session.user_id, telegram_user_id=path)` — WHERE по обоим полям (нельзя отвязать чужой TG). |
| 200 | `{"deleted": true}` (идемпотентно: если строки не было — `{"deleted": false}`, 200). |
| Audit | `telegram_link_revoked` с `details={telegram_user_id, reason: 'user_unlink'}`. |

### `GET /messages/{id}` — поддержка `embed=tg` (ADR-0022 §2.6)

| Query | `embed: str | None = None` — если `embed='tg'`, backend выставляет в Jinja-контекст `embed_tg=True`. Шаблон `message_view.html` при `embed_tg=True` скрывает секцию `<section class="attachments">`. Остальной функционал (mark-read, bottom-nav, logout) остаётся. **Примечание:** это **residual** web-route. Кнопка push-уведомления «Посмотреть сообщение» на него **больше не ведёт** — она `callback_data "msg:{id}"` (Bug-fix #5), тело письма шлётся в чат webhook'ом (см. ниже + ADR-0022 §2.5/§2.6). Route остаётся доступен как обычная web-страница. |

### Push-уведомления о новых письмах (ADR-0022 §2)

Доставка происходит **асинхронно** через worker-job (см. `05-modules.md` §14.1 + `worker → tg_notify_dispatch`). Нет публичного HTTP-эндпоинта для триггера/просмотра очереди — это внутренний механизм. Получатель видит уведомление в Telegram-боте как Message с inline-keyboard кнопкой «Посмотреть сообщение» (**`callback_data` button** `"msg:{message_id}"`, Bug-fix #5 — НЕ WebApp). Тап → `callback_query` на основной webhook → `callback_handler` резолвит владельца (telegram_user_id→users через `telegram_links`, visibility ADR-0019) и шлёт **полное тело письма в тот же чат** (`send_html_message`). Без открытия WebView/Mini-App. См. ADR-0022 §2.5/§2.6.

**ADR-0024 (multi-TG):** если у получателя несколько живых TG-привязок, уведомление доставляется **в каждый** живой чат. Recipient-SQL даёт по строке на каждый `telegram_user_id`; идемпотентность — per `(message_id, telegram_user_id)`. Мёртвый чат (`dead_at`) пропускается, остальные получают.

**ADR-0027 (push-only боты по командам) + round-42 (callback-кнопка); round-44 (+`business2`):** параллельно с основным ботом 4 push-бота (round-31: `ivan`/`alexandra`/`andrei`; round-44: +`business2`) шлют письма **своей команды** (по `group_id`) на `ADMIN_TELEGRAM_IDS` — fire-and-forget, без `telegram_links`/idempotency/recovery. **round-42:** их уведомления тоже несут кнопку «Посмотреть сообщение» (`callback_data "msg:{id}"`, при заданном `BOT_*_WEBHOOK_SECRET`); тап → `callback_query` на **push-webhook этого бота** (`POST /api/telegram/push-webhook/{bot_name}`, см. выше) → авторизация по `ADMIN_TELEGRAM_IDS` + group-match → тело письма в чат токеном этого бота. Отдельно от visibility-резолва основного callback. См. [ADR-0027](./adr/ADR-0027-push-team-bots.md) §10–§11.

**Объём уведомлений (round-31, env `TG_NOTIFY_ALL_MESSAGES`, default `true`):**
- `true` — уведомление по **каждому** новому письму (тег не обязателен);
- `false` — только письма с ≥1 тегом (историческое поведение).

Шаблон текста (HTML mode, **round-36**; источник истины — [ADR-0022](./adr/ADR-0022-telegram-sso-and-notifications.md) §2.5 + `05-modules.md` §14.1):
```
🆔: <b>{acc.display_name|acc.email}</b>     ← ВСЕГДА (ник почты; при пустом display_name → email)
#️⃣: <b>{теги через ", "|"Не сортировано"}</b> ← ВСЕГДА (все теги; если нет → "Не сортировано")

Клиент: <b>{from_name|from_addr}</b>        ← ВСЕГДА
Тема: <b>{subject|"(без темы)"}</b>          ← ВСЕГДА (при пустой теме → "(без темы)")

{первые 100 символов текста письма}          ← только если тело непусто (PREVIEW_LEN=100)

[ Посмотреть сообщение ]   ← inline_keyboard.callback_data = "msg:{message_id}" (Bug-fix #5; callback → тело письма в чат, НЕ web_app)
```
Строки `🆔`/`#️⃣`/`Клиент`/`Тема` — всегда; превью тела и предшествующий пустой разделитель — только при непустом теле. Все значения `html.escape()`-ятся. Доставка ограничена per-chat троттлингом `TG_SEND_PER_CHAT_PER_MINUTE` (default 20/мин; ADR-0022 §2.9).

### Setup webhook (one-shot)

После каждого изменения secret или URL — выполнить (на хост-машине, разово):

```bash
curl -F "url=https://postapp.store/api/telegram/webhook/${TELEGRAM_WEBHOOK_SECRET}" \
     -F "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
     "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook"
```

Подробнее — `07-deployment.md` секция 14 "Telegram bot setup".

---

## 4b. Outbound webhooks (ADR-0023)

> **⚠️ ИСТОРИЧЕСКИЙ РАЗДЕЛ — подсистема outbound-webhooks (в проде было 0 конфигураций) СНЯТО демонтажём ([ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md), выполнено на проде 2026-07-15).** Описанное ниже в коде/проде агрегатора НЕ существует; текст сохранён как record исходного решения. НЕ реализовывать и не принимать за действующий контракт.

Источник истины — [ADR-0023](./adr/ADR-0023-outbound-webhooks.md). Outbound HTTP-webhook одна на команду (`UNIQUE(group_id)`). Триггер — письма с тегами в любом из ящиков команды (фильтр `m.internal_date >= w.created_at`, симметричный TG-нотификациям). Auth — static `X-Webhook-Secret` header. Доставка через worker (см. `05-modules.md` модуль 19 + 14.2).

### Авторизация всех endpoint'ов

- `group_leader` — управляет webhook'ом своей группы (`scope.group_id`). Передача `?group_id=` **запрещена** (если попытается — `400 validation_error` `field=group_id`).
- `super_admin` — управляет webhook'ом любой группы; **обязан** передать `?group_id=<id>` в каждом запросе.
- `group_member` — `403 forbidden` на всех endpoint'ах.

Все state-changing endpoints — под CSRF (ADR-0010).

### `GET /my/integrations`

| | |
| --- | --- |
| Доступ | `group_leader` или `super_admin`. Group_member → `302 /` (без права видеть). |
| 200 | HTML render `templates/my/integrations.html`: URL input, статус (last_fired_at, last_error, consecutive_failures, dead-indicator), кнопки Save / Rotate / Test / Delete. |
| Note | Server-side flash-сообщения категории `secret_reveal` показываются один раз после `POST /api/webhooks/me` или `POST /api/webhooks/me/rotate-secret` (one-shot). |

### `GET /api/webhooks/me`

| | |
| --- | --- |
| Query | `group_id?: int` — обязателен для super_admin; для group_leader запрещён. |
| 200 | `{id, group_id, url, is_active, last_fired_at, last_error, dead_at, consecutive_failures, created_at, updated_at}` — **БЕЗ `secret`**. |
| 404 | `not_found` — у группы webhook не настроен. |
| 403 | `forbidden`. |

### `POST /api/webhooks/me`

| | |
| --- | --- |
| Запрос | JSON `{url: str}` (1..2048, `https://`); либо form-encoded (см. ниже). |
| Query | `group_id?: int` — для super_admin. |
| Валидация | URL `https://...`; max 2048; lexical reject `https://localhost\|127.0.0.1\|0.0.0.0\|[::1]`; DNS-резолв всех A/AAAA — запрет приватных CIDR (см. `06-security.md` §4); HTTP/2 unsupported — не критично, httpx auto-fallback. |
| Поведение | Backend: (1) `secret_plaintext = secrets.token_urlsafe(32)`; (2) `nextval('webhooks_id_seq')` → `webhook_id`; (3) `secret_encrypted = encrypt_webhook_secret(secret_plaintext, webhook_id)` (AAD=webhook_id); (4) INSERT с явным id. |
| Rate-limit | 10/час per `group_id`. |
| 201 | `{id, group_id, url, secret: "<plaintext>", is_active: true, last_fired_at: null, last_error: null, dead_at: null, consecutive_failures: 0, created_at, updated_at}` — **`secret` показан ОДИН РАЗ**. |
| 409 | `webhook_already_exists` (UNIQUE `group_id`). |
| 400 | `validation_error` / `webhook_url_private_ip`. |
| 403 | `forbidden`. |
| Audit | `webhook_created` (`details = {group_id, webhook_id, url}`). |

Sample request (JSON):
```http
POST /api/webhooks/me HTTP/1.1
Content-Type: application/json
Cookie: mas_session=...; mas_csrf=...
X-CSRF-Token: ...

{"url": "https://hooks.example.com/incoming/abc"}
```

Sample response:
```json
HTTP/1.1 201 Created
Content-Type: application/json

{
  "id": 7,
  "group_id": 5,
  "url": "https://hooks.example.com/incoming/abc",
  "secret": "Rt9_fJ-2kV...44chars",
  "is_active": true,
  "last_fired_at": null,
  "last_error": null,
  "dead_at": null,
  "consecutive_failures": 0,
  "created_at": "2026-05-20T12:00:00Z",
  "updated_at": "2026-05-20T12:00:00Z"
}
```

##### Form-encoded request (no-JS)
```
POST /api/webhooks/me HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

url=https%3A%2F%2Fhooks.example.com%2Fincoming%2Fabc&csrf_token=...
```

##### Form-encoded response
- Success: `303 See Other`, `Location: /my/integrations`, flash `[secret_reveal]` = `"Сохраните секрет: <plaintext>"` (one-shot, после next GET очищается).
- Validation error: re-render `my/integrations.html` с error-context.

### `PATCH /api/webhooks/me`

| | |
| --- | --- |
| Запрос | JSON `{url?: str, is_active?: bool}` (любое подмножество). |
| Query | `group_id?: int` для super_admin. |
| Поведение | (a) `url` — та же валидация. Смена URL не ротирует secret. (b) `is_active=true` после dead → `dead_at=NULL, consecutive_failures=0, last_error=NULL`. (c) `is_active=false` — диспатчер пропускает. |
| Rate-limit | 30/час per `webhook_id`. |
| 200 | объект как в `GET` (без secret). |
| 400 | `validation_error` / `webhook_url_private_ip`. |
| 404 | `not_found`. |
| 403 | `forbidden`. |
| Audit | `webhook_updated` (`details = {webhook_id, changed_fields: [...], previous_dead_at: ts\|null}`). |

##### Form-encoded request (no-JS) — через method override:
```
POST /api/webhooks/me HTTP/1.1
Content-Type: application/x-www-form-urlencoded

_method=PATCH&url=https%3A%2F%2Fhooks.example.com%2Fv2&csrf_token=...
```
(чекбокс `is_active`: `on`/`true`/`1` → true; отсутствие — backend интерпретирует как «не менять», т.к. это PATCH с подмножеством полей. Чтобы явно установить false — отдельная hidden-форма `is_active=0`/`is_active=false`.)

##### Form-encoded response
- Success: `303`, `Location: /my/integrations`, flash="Webhook обновлён".

### `DELETE /api/webhooks/me`

| | |
| --- | --- |
| Query | `group_id?: int` для super_admin. |
| Поведение | `DELETE FROM webhooks WHERE id=:wid` → CASCADE удалит `webhook_deliveries`. In-flight dispatch завершается естественно (диспатчер при следующем `dispatch_one_payload` не найдёт webhook'а — `recipient is None` → return). |
| Rate-limit | 10/час per `webhook_id`. |
| 204 | success. |
| 404 | `not_found`. |
| 403 | `forbidden`. |
| Audit | `webhook_deleted` (`details = {webhook_id, group_id, url}`). |

##### Form-encoded request (no-JS) — через sibling-роут:
```
POST /api/webhooks/me/delete HTTP/1.1
Content-Type: application/x-www-form-urlencoded

_method=DELETE&csrf_token=...
```

##### Form-encoded response
- Success: `303`, `Location: /my/integrations`, flash="Webhook удалён".

### `POST /api/webhooks/me/rotate-secret`

| | |
| --- | --- |
| Запрос | пустое тело + CSRF. |
| Query | `group_id?: int` для super_admin. |
| Поведение | (1) Новый `secret_plaintext = secrets.token_urlsafe(32)`; (2) шифрование с тем же `webhook_id` AAD; (3) UPDATE `secret_encrypted = <new>, updated_at = now()`. Старый secret немедленно недействителен. |
| Rate-limit | 5/час per `webhook_id`. |
| 200 | `{id, group_id, url, secret: "<new-plaintext>", is_active, ...}` — secret one-time-show. |
| 404 | `not_found`. |
| 403 | `forbidden`. |
| Audit | `webhook_secret_rotated` (`details = {webhook_id}`). |

##### Form-encoded
- Request: `POST /api/webhooks/me/rotate-secret`, form-body `csrf_token=...`.
- Success response: `303 See Other`, `Location: /my/integrations`, flash `[secret_reveal]` с новым plaintext.

### `POST /api/webhooks/me/test`

| | |
| --- | --- |
| Запрос | пустое тело + CSRF. |
| Query | `group_id?: int` для super_admin. |
| Поведение | Синхронно (внутри request) делает один POST на webhook URL с фиксированным payload `event="test"`. **НЕ** пишет `webhook_deliveries`; **НЕ** трогает `consecutive_failures`/`dead_at`/`last_error`. Это диагностика. |
| Rate-limit | 10/час per `webhook_id` (env `WEBHOOK_TEST_LIMIT`). |
| 200 | `{response_code: int, response_excerpt: str, duration_ms: int}` — даже при receiver 5xx (это диагностика). |
| 502 | `upstream_error` — DNS-резолв fail / timeout 10s / network unreachable. `details: {reason}`. |
| 404 | `not_found`. |
| 403 | `forbidden`. |
| 429 | `rate_limited` + `Retry-After`. |

Sample test-payload (исходящий POST от нас → receiver):
```json
{
  "event": "test",
  "timestamp": "2026-05-20T12:00:00.000Z",
  "webhook_id": 7,
  "team": {"id": 5, "name": "Команда A"}
}
```

##### Form-encoded request:
```
POST /api/webhooks/me/test HTTP/1.1
Content-Type: application/x-www-form-urlencoded

csrf_token=...
```

##### Form-encoded response
- Success: `303`, `Location: /my/integrations`, flash="Тест выполнен: HTTP {response_code}, {duration_ms} мс".
- Upstream error: re-render с flash-error.

### Исходящий POST (от нас → target webhook) при `event="message_tagged"`

| Заголовок | Значение |
| --- | --- |
| `Content-Type` | `application/json; charset=utf-8` |
| `X-Webhook-Secret` | `<plaintext-secret>` (расшифровка `secret_encrypted` с AAD=`webhook_id`) |
| `User-Agent` | `mas-webhook/1.0` |
| `X-Webhook-Event` | `message_tagged` или `test` |
| `X-Webhook-Delivery-Id` | `<webhook_deliveries.id>` (для `message_tagged`) |

`httpx.AsyncClient(timeout=10.0, follow_redirects=False)`. 3xx трактуются как failed (см. ADR-0023 §4.3 SSRF / redirect-policy).

Payload (`event="message_tagged"`):
```json
{
  "event": "message_tagged",
  "timestamp": "2026-05-20T12:00:00.000Z",
  "webhook_id": 7,
  "team": {"id": 5, "name": "Команда A"},
  "message": {
    "id": 12345,
    "internal_date": "2026-05-20T11:55:00Z",
    "from_addr": "sender@example.com",
    "from_name": "Sender Name",
    "subject": "Тема письма",
    "body_text": "Plain-text content, truncated to first 16384 chars",
    "body_truncated": false,
    "mail_account": {
      "id": 7,
      "email": "support@example.com",
      "display_name": "Support"
    },
    "tags": [
      {"id": 7, "name": "Urgent", "color": "#dc2626"}
    ]
  }
}
```

`tags[]` агрегируется по **всей команде** (DISTINCT теги всех users группы на этом сообщении; super_admin теги тоже учитываются — см. ADR-0023 §3.2 SQL). Один webhook = один POST = одна команда. Attachments **не включаются** в payload (receiver получает через наш API). `body_text` truncated до 16 KiB.

### Связанные изменения flow

| Endpoint | Изменение от base-логики |
| --- | --- |
| `DELETE /api/admin/groups/{id}` (ADR-0019) | Каскад: `webhooks` (FK ON DELETE CASCADE) → каскадно `webhook_deliveries`. Отдельный audit `webhook_deleted` **не** пишется — каскад покрыт `group_delete` (симметрично `telegram_links` каскаду при `delete_user`). |
| `DELETE /api/admin/users/{id}` | **Не** трогает webhook'и (они привязаны к группе, не user'у). При удалении лидера, у которого `consecutive_failures > 0`, статус webhook'а сохраняется — после переназначения лидера новый лидер видит state как-есть. |
| Sync_cycle COMMIT с `applied_count > 0` | Параллельно с TG-блоком (см. ADR-0022 §2.1) выполняется аналогичный try/except LPUSH в `webhook_dispatch_queue`. Падение LPUSH не валит sync_cycle. См. ADR-0023 §3.1 + `05-modules.md` модуль 14. |

---

## 4c. OAuth2 Outlook (ADR-0025)

> **⚠️ Раздел УСТАРЕЛ в части путей — session-`oauth_router` СНЯТ ([ADR-0044](./adr/ADR-0044-decommission-runbook.md) §7, Фаза A3).** Пути `/api/oauth/outlook/*`, описанные ниже, отдают `404`.
>
> **Actual:** OAuth-consent **жив** и восстановлен headless-роутами в `external/router.py` — `POST /api/external/mailboxes/oauth/authorize {crm_state}` + `GET /api/external/mailboxes/oauth/callback` ([ADR-0045](./adr/ADR-0045-external-outlook-oauth-headless.md), под `EXTERNAL_WRITE_ENABLED`); `OUTLOOK_REDIRECT_URI` = `{APP_BASE_URL}/api/external/mailboxes/oauth/callback`. Механика токенов/PKCE/refresh (ADR-0025) — **в силе**. Актуальный контракт — §4f.

Источник истины — [ADR-0025](./adr/ADR-0025-outlook-oauth2.md). Подключение личных Outlook-ящиков (`outlook.com`/`hotmail.com`/`live.com`) через OAuth2 + XOAUTH2 рядом с обычными password-аккаунтами. Consent через наш сайт + OctoBrowser. Все endpoint'ы доступны только когда `OUTLOOK_OAUTH_ENABLED` (заданы `OUTLOOK_CLIENT_ID` + `OUTLOOK_CLIENT_SECRET`); иначе `404 not_found` (route скрыт, симметрично telegram-bot-disabled).

### `GET /api/oauth/outlook/authorize`
| | |
| --- | --- |
| Auth | session cookie. |
| Логика | Генерит `state` (32B urlsafe) + PKCE `code_verifier`/`code_challenge` (S256), сохраняет в Redis `oauth_state:{state}` = `{user_id, code_verifier}` TTL `OUTLOOK_OAUTH_STATE_TTL_SECONDS` (default 600), привязка к `session.user_id`. Строит Microsoft authorize URL. |
| 200 | `{"authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?...", "state": "<32B urlsafe>"}` (`OAuthAuthorizeResponse`) — фронт показывает ссылку «открыть в OctoBrowser» (НЕ auto-redirect — пользователь открывает в нужном профиле). `state` отдаётся для отображения/трекинга на клиенте. |
| Rate-limit | 30 / час per user (token-bucket, ключ = `user_id`). Запас на подключение/переподключение нескольких Outlook-ящиков подряд. Расход на старте authorize, без refund при успешном callback — см. ADR-0025 §5. |
| 404 | `not_found` если `OUTLOOK_OAUTH_ENABLED=false`. |

### `GET /api/oauth/outlook/callback`
| | |
| --- | --- |
| Это | зарегистрированный в Azure `redirect_uri` = `{APP_BASE_URL}/api/oauth/outlook/callback`. |
| Query | `code`, `state` (успех) либо `error`, `error_description` (отказ). |
| Логика | GET+DEL `oauth_state:{state}` (одноразовый); нет/истёк → `400 oauth_state_invalid`. Обмен `code`→токены на token endpoint (`grant_type=authorization_code` + `code_verifier`). Email из `id_token`/Graph `GET /me`. Create/update `mail_account` (`auth_type='oauth_outlook'`, Outlook host/port, зашифрованные токены). Q-OAUTH-1: callback может прийти без cookie сессии (другой OctoBrowser-профиль) → доверяем Redis-state, привязанному к `user_id`. |
| 302 | Редирект на `/accounts?outlook=connected` (фронт показывает «Outlook подключён» по query-параметру). |
| 400 | `oauth_state_invalid` / `oauth_exchange_failed` (token endpoint вернул ошибку) / `oauth_consent_denied` (пришёл `error`). |
| Audit | `oauth_account_linked` с `details={mail_account_id, email, scopes}`. |
| CSRF | exempt (state выполняет роль anti-CSRF; cookie может отсутствовать). |

### Связанные изменения

| Endpoint | Изменение |
| --- | --- |
| `GET /api/mail-accounts`, `GET /api/mail-accounts/{id}` | DTO дополняется `auth_type` и (для oauth) `oauth_needs_consent`. UI показывает бейдж «Outlook OAuth» и кнопку «переподключить» при `oauth_needs_consent=true`. |
| `PATCH /api/mail-accounts/{id}` | Для `auth_type='oauth_outlook'` host/port/ssl/credentials фиксированы — менять можно только `display_name`. Форма редактирования общая с password-аккаунтами и шлёт полный снимок (`email`+`imap_*`+`smtp_*`+`display_name`); поле, переданное **равным текущему значению аккаунта, — no-op** и игнорируется (не ошибка). `400 validation_error` `field=auth_type` возвращается только если поле передано **и отличается** от текущего, либо передан непустой `password`/`smtp_password`. См. ADR-0025 §4c. |
| `POST /api/mail-accounts/test` | Для oauth-аккаунтов test использует XOAUTH2 (refresh→access→коннект); password-тест не применяется. |
| `POST /api/mail-accounts` (ручной IMAP/SMTP) | Без изменений — создаёт `auth_type='password'`. |

> **Q-OAUTH-3 (БЛОКЕР e2e):** требует реальный Azure App (`client_id`/`secret`) и проверки, что personal accounts выдают IMAP/SMTP XOAUTH2-доступ. Код и unit/integration-тесты с моками token endpoint можно реализовать без этого; e2e — после получения Azure App от пользователя.

---

## 4d. External pull-API (ADR-0029)

Источник истины — [ADR-0029](./adr/ADR-0029-external-pull-api.md). **PULL-канал** для доверенного B2B-партнёра: его сервис периодически опрашивает endpoint и инкрементально забирает новые письма. Отличие от ADR-0023 (push, per-team, tagged-only): здесь **pull**, **ВСЕ** письма системы (super_admin visibility), **сырое полное тело**.

### Авторизация

- Static `EXTERNAL_API_KEY` в заголовке `X-API-Key: <key>` **или** `Authorization: Bearer <key>` (`X-API-Key` имеет приоритет). Сравнение constant-time (`secrets.compare_digest`).
- Без cookie-сессии и `VisibilityScope`. API-key = доверенный сервис, видит все письма всех ящиков. Единственный read-фильтр — canonical-дедуп дубль-ящиков (`mail_account_id IN list_canonical_account_ids()`, `MIN(id)` per `LOWER(email)`): если один email подключён двумя командами, внешний сервис получает **одну** копию каждого письма (консистентно с super_admin inbox, round-18).
- Фича включена ⇔ `EXTERNAL_API_KEY` непустой. Пусто → endpoint отдаёт `401` (неперечислимо).
- CSRF — **exempt** (нет cookie-auth; GET-only). Rate-limit `LIMIT_EXTERNAL_API` (env `EXTERNAL_API_RATE_LIMIT_PER_MINUTE`, `int`, default `120`, `ge=1`; запросов в минуту на IP) — consume **до** проверки ключа (anti-flood).

### `GET /api/external/messages`

Два режима на одном endpoint: **forward** (ADR-0029, oldest→newest) и **backward/latest** (ADR-0036, newest-first для «бесконечной ленты» CRM). Режим выбирается параметром `order`; forward — дефолт и полностью обратно-совместим.

| | |
| --- | --- |
| Доступ | `EXTERNAL_API_KEY` (`X-API-Key` / `Bearer`). |
| Query `order` | enum `asc` \| `desc`, default `asc`. `asc` = forward (ADR-0029). `desc` = backward/latest (ADR-0036). [ADR-0036](./adr/ADR-0036-external-backward-pagination.md) |
| Query `since_id` | `int ≥ 0`, default `0`. Только при `order=asc`. Семантика `WHERE id > since_id`. При `order=desc` — `400`. |
| Query `before_id` | `int ≥ 1`, optional (нет default). Только при `order=desc`. Присутствует → `WHERE id < before_id`; отсутствует → latest N. При `order=asc` — `400`. |
| Query `limit` | `int`, `1..200`, default `50` (hard cap 200). Оба режима. |
| Query `mail_account_id` | `int ≥ 1`, optional, **повторяемый** (`list[int]`, ADR-0039). Письма этих ящиков. Эффективный набор = `{mail_account_id…} ∩ canonical_ids`. Работает в **обоих** режимах `order`. Несуществующий/чужой/non-canonical id → **пустая страница** (не 404). BC одиночного значения. [ADR-0037](./adr/ADR-0037-external-teams-mailboxes-message-filters.md) / [ADR-0039](./adr/ADR-0039-external-write-api.md) §3 |
| Query `group_id` | `int ≥ 1`, optional, **повторяемый** (`list[int]`, ADR-0039). Письма ящиков этих команд (union). Эффективный набор = `⋃ list_account_ids_in_group(group_id) ∩ canonical_ids`. Оба режима. Несуществующая/пустая команда → **пустая страница** (не 404). BC одиночного значения. Используется CRM для ролевой видимости (`MailScope.group_ids`). ADR-0037 / [ADR-0039](./adr/ADR-0039-external-write-api.md) §3 |
| Комбинирование фильтров | `mail_account_id` **и** `group_id` — **AND-комбинируемы** (ADR-0039 **supersedes** взаимоисключение ADR-0037): эффективный набор = `canonical ∩ (⋃ accounts of group_id, если задан) ∩ (set(mail_account_id), если задан)`. Пустое пересечение → **пустая страница** (не 404). Кода `field="filter"` больше нет. Используется CRM: scope-`group_id` (`MailScope.group_ids`) AND пользовательский `mail_account_id`. [ADR-0039](./adr/ADR-0039-external-write-api.md) §3 |
| Семантика `asc` | `WHERE m.id > :since_id AND m.mail_account_id IN (:canonical_ids) ORDER BY m.id ASC LIMIT :limit` — keyset по `messages.id BIGSERIAL` (монотонный insert-order; без пропусков/дублей курсора) + canonical-дедуп дубль-ящиков. Внешний сервис хранит `last_id` = `next_since_id`. |
| Семантика `desc` | latest: `WHERE m.mail_account_id IN (:canonical_ids) ORDER BY m.id DESC LIMIT :limit`; older: `+ AND m.id < :before_id`. Reverse-scan по PK `id` (без новых индексов). Потребитель хранит `next_before_id` и передаёт его в `before_id` для следующей (более старой) страницы. |
| 200 (`asc`) | `{messages:[ExternalMessageDTO] (id ASC), next_since_id:int, has_more:bool}`. |
| 200 (`desc`) | `{messages:[ExternalMessageDTO] (id DESC, newest-first), next_before_id:int\|null, has_more:bool}`. |
| 200 (пусто, `asc`) | `{messages:[], next_since_id:<входной since_id>, has_more:false}`. |
| 200 (пусто, `desc`) | `{messages:[], next_before_id:null, has_more:false}`. |
| 401 | `not_authenticated` — нет/неверный ключ **или** фича выключена (неперечислимо). |
| 429 | `rate_limited` (+`Retry-After`). |
| 400 | `validation_error` — `since_id<0`/нечисловой; `before_id<1`/нечисловой; `limit` вне `1..200`; `order`∉{asc,desc}; `before_id` при `order=asc`; `since_id` при `order=desc`; `since_id`+`before_id` одновременно; `mail_account_id<1`/нечисловой (`field=mail_account_id`); `group_id<1`/нечисловой (`field=group_id`). **`mail_account_id`+`group_id` вместе — НЕ ошибка** (AND-комбинируемы, ADR-0039 §3; кода `field=filter` больше нет — взаимоисключение ADR-0037 снято). |

`ExternalMessageDTO` (отдельный от UI `MessageDetail` — стабильный версионируемый контракт; **сырое stored** `body_text`/`body_html` без `collapse_blank_lines_*`):

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
      "mail_account": {"id": 7, "email": "support@corp.example", "display_name": "Support"},
      "body_text": "<raw stored>",
      "body_html": "<raw stored or null>",
      "body_present": true,
      "body_truncated": false,
      "tags": [{"id": 7, "name": "Urgent", "color": "#dc2626"}]
    }
  ],
  "next_since_id": 12345,
  "has_more": true
}
```

- `next_since_id` = `id` последнего элемента (`max(id)`); пусто → входной `since_id`. Поле **только** в `asc`-ответе.
- `has_more` = `len(messages) == limit` (оба режима).
- `to_addrs` — всегда строка (БД `NOT NULL DEFAULT ''`); `cc_addrs`/`from_name`/`subject`/`body_html`/`mail_account.display_name` — nullable.
- Вложения **не передаются** (Q-0029-1). `tags` — `[]` если нет тегов, возвращаются в **обоих** режимах (`ExternalTagDTO` не меняется). `body_present=false` → `body_text=""`, `body_html=null`.
- Возвращаются **только** поля письма — никаких паролей/токенов/secret'ов/IMAP-UID.

**Backward/latest (`order=desc`, ADR-0036).** `messages[]` тот же `ExternalMessageDTO`, порядок `id DESC` (newest-first); курсорное поле — `next_before_id` (вместо `next_since_id`):

```json
{
  "messages": [ { "id": 12100, "...": "…ExternalMessageDTO, id DESC…" } ],
  "next_before_id": 12001,
  "has_more": true
}
```

- `next_before_id` = `min(id)` батча (= `id` последнего элемента, т.к. DESC) — передать в `before_id` для следующей (более старой) страницы. `null`, если батч пуст (старых больше нет). Поле **только** в `desc`-ответе.
- Итерация ленты: первый экран `?order=desc&limit=50` → newest 50 + `next_before_id`; скролл вниз `?order=desc&before_id=<next_before_id>&limit=50`; стоп при `has_more=false` (или `messages=[]`/`next_before_id=null`).
- Курсор — по монотонному `messages.id` (не `internal_date`): поздно-пришедшее письмо имеет максимальный `id` и корректно попадает в latest-страницу — ADR-0036 снимает ограничение ADR-0029 «нет newest-first» без silent-loss. `EXTERNAL_API_KEY`/rate-limit `LIMIT_EXTERNAL_API`/CSRF-exempt/super_admin-visibility/canonical-дедуп — те же, что в forward; новых env/флагов/миграций нет.

> **Версионирование:** текущий путь `/api/external/` (неявная v1), поля добавляются аддитивно. Breaking change → `/api/external/v1/` + новый ADR.
>
> **id-gaps:** retention-cleanup (ADR-0011, 30д) удаляет старые письма → безвредные пропуски в `id` (keyset `id > since_id` их игнорирует). Контракт: внешний сервис поллит **чаще** окна ретенции.
>
> **Запись (reply):** внешний API read-only (ADR-0029); единственный write-endpoint — ответ на письмо — описан в **§4d-reply** ниже ([ADR-0035](./adr/ADR-0035-external-reply-endpoint.md)).

### 4d-teams. `GET /api/external/teams` (ADR-0037)

> **⚠️ ИСТОРИЧЕСКИЙ РАЗДЕЛ — эндпоинт `GET /api/external/teams` СНЯТО демонтажём ([ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md), выполнено на проде 2026-07-15).** Описанного в коде/проде агрегатора НЕТ; текст сохранён как record исходного решения. НЕ реализовывать. Групп в агрегаторе нет (`groups`/`user_groups` дропнуты, `mail_accounts.group_id` дропнут); роута в `backend/app/external/router.py` нет. Команды живут только в CRM (`teams`).

Источник истины — [ADR-0037](./adr/ADR-0037-external-teams-mailboxes-message-filters.md). Read-only список **команд** системы для внутреннего CRM-потребителя (фильтр «по команде», подписи). Команда = `groups`; **команда ≠ тег** (теги — в `ExternalMessageDTO.tags`, ADR-0017). Тот же `X-API-Key`-флоу, что `GET /api/external/messages` (ADR-0029 §4).

| | |
| --- | --- |
| Метод / путь | `GET /api/external/teams` (query-параметров нет). |
| Доступ | `EXTERNAL_API_KEY` (`X-API-Key` / `Bearer`). CSRF exempt. |
| Rate-limit | `LIMIT_EXTERNAL_API` (тот же read-бюджет 120/min per IP; нового лимита нет). |
| Visibility | super_admin — **все** команды. Источник `GroupsRepo.list_all_groups()` (`ORDER BY id`). |
| 200 | `{"teams": [{"id": int, "name": str}]}`. Пусто → `{"teams": []}`. |
| 401 | `not_authenticated` — нет/неверный ключ **или** фича выключена (неперечислимо). |
| 429 | `rate_limited` (+`Retry-After`). |

DTO `ExternalTeamDTO{id:int, name:str}` (обёртка `ExternalTeamsResponse{teams:[...]}`). **Только** `id`/`name` — без `leader_user_id`/`created_at`/`members_count` (в отличие от admin-`GET /api/admin/groups`).

### 4d-mailboxes. `GET /api/external/mailboxes` (ADR-0037)

Источник истины — ADR-0037. Read-only список **ящиков** со статусом для дропдауна почт CRM, счётчиков «активные/неактивные» и маппинга ящик→команда. Тот же auth/флоу.

| | |
| --- | --- |
| Метод / путь | `GET /api/external/mailboxes`. Query (ADR-0039 §4): `is_active: bool\|null` (None=все), `group_id: list[int]` (повторяемый, union; пусто=без фильтра). |
| Доступ | `EXTERNAL_API_KEY` (`X-API-Key` / `Bearer`). CSRF exempt. |
| Rate-limit | `LIMIT_EXTERNAL_API` (тот же read-бюджет). |
| Visibility | super_admin + **canonical-дедуп** (ADR-0029 §5): `MailAccountsRepo.list_by_ids(list_canonical_account_ids())` — один канонический (`MIN(id)`) ящик на `LOWER(email)`. Множество совпадает с ящиками, чьи письма отдаёт `GET /api/external/messages`. |
| 200 | `{"mailboxes": [{"id": int, "email": str, "display_name": str\|null, "group_id": int\|null, "is_active": bool}]}`. Пусто → `{"mailboxes": []}`. |
| 401 / 429 | как выше. |

DTO `ExternalMailboxDTO{id:int, email:str, display_name:str|null, group_id:int|null, is_active:bool, last_synced_at:datetime|null, last_sync_error:str|null, consecutive_failures:int}` (обёртка `ExternalMailboxesResponse{mailboxes:[...]}`). Поля:
- `id` = `mail_accounts.id` = `ExternalMessageDTO.mail_account.id` → CRM джойнит письма с ящиками по этому ключу.
- `display_name` — nullable (БД); хелпер `display_name || email`.
- `group_id` — маппинг ящик→команда (`mail_accounts.group_id`, nullable; `null` = персональный). `group_id` = `teams[].id`. **Раскрыт осознанно** для CRM (ADR-0037 §Security).
- `is_active` — статус (`false` = авто-отключён воркером, ADR-0033). Счётчики активные/неактивные CRM считает **клиентски** (server-side агрегатов нет).
- **Статус синка (ADR-0039 §4):** `last_synced_at` (nullable), `last_sync_error` (nullable), `consecutive_failures` (`int`, 0=здоров) — из одноимённых полей `mail_accounts`; для кружка статуса/диагностики на вкладке «Почты» CRM. Аддитивно.
- **Никаких** credentials/`user_id`/owner-структур/`oauth_*`/`smtp_*`/`imap_*`.

> **Консистентность:** `teams[].id` = `groups.id` = `mailboxes[].group_id`; `mailboxes[].id` = `messages[].mail_account.id`. Трёхуровневый джойн письмо → ящик → команда замыкается на стороне CRM. `ExternalMessageDTO`/`ExternalMailAccountDTO` **не меняются** — `group_id`/`is_active` доступны **только** через `mailboxes`, не в письме (стабильность контракта ADR-0029 §6).

### 4d-reply. `POST /api/external/messages/{id}/reply` (ADR-0035) — СНЯТ (Фаза A2.2, 2026-07-15)

> **⚠️ ИСТОРИЧЕСКИЙ РАЗДЕЛ — эндпоинт СНЯТ (проверено по коду).** Фаза A2.2 ([ADR-0044](./adr/ADR-0044-decommission-runbook.md) §4 / [ADR-0048](./adr/ADR-0048-external-send-contract-and-reply-restore.md) §3, коммиты `32f320d`/`e60d610`) выполнена: роута `/messages/{id}/reply` в `backend/app/external/router.py` **нет** (живые роуты — строки декораторов `@router.*`: `:156`, `:225`, `:319`, `:331`, `:344`, `:366`, `:378`, `:395`, `:528`, `:552`), гейт `EXTERNAL_REPLY_ENABLED` и лимит `LIMIT_EXTERNAL_REPLY` удалены, writer `sent_messages` снят вместе с дропнутой таблицей. **Действующая замена — обобщённый send, §4f-send** (`POST /api/external/mailboxes/{id}/send` → `200 {smtp_message_id}`). Текст ниже сохранён как record исходного решения ADR-0035; НЕ реализовывать.

Источник истины — [ADR-0035](./adr/ADR-0035-external-reply-endpoint.md). **Единственный write** во внешнем API: ответ на **существующее** входящее письмо тем же `X-API-Key`. Не рушит read-only-модель ADR-0029 — узкая поверхность: отвечаем только на письмо, которое партнёр мог получить pull-каналом; отправитель **не выбирается** (= `mail_account` этого письма); нет CRUD/произвольной отправки/выбора `from`.

#### Feature-флаги

- `EXTERNAL_API_KEY` (как ADR-0029): пусто ⇒ весь внешний API (read+reply) выключен.
- `EXTERNAL_REPLY_ENABLED` (`bool`, default `false`): **отдельный** write-гейт. `false` ⇒ reply отвечает `403 forbidden` даже при валидном ключе. Read-only ADR-0029 остаётся дефолтом; запись — явный opt-in.

#### Авторизация

Тот же флоу, что `GET /api/external/messages` (ADR-0029 §4): rate-limit **до** ключа → `X-API-Key`/`Bearer` (constant-time) → `external_api_enabled` → **затем** write-гейт `EXTERNAL_REPLY_ENABLED`. CSRF **exempt**. Rate-limit — **отдельный** `LIMIT_EXTERNAL_REPLY` (env `EXTERNAL_REPLY_RATE_LIMIT_PER_MINUTE`, `int`, default `30`, `ge=1`; запросов в минуту на IP), **не** переиспользует read-лимит `120/min` (write дороже/abuse-опаснее; изоляция бюджетов).

| | |
| --- | --- |
| Метод / путь | `POST /api/external/messages/{id}/reply` (`{id}` — `int ≥ 1`, `messages.id` оригинала). |
| Доступ | `EXTERNAL_API_KEY` (`X-API-Key` / `Bearer`) **и** `EXTERNAL_REPLY_ENABLED=true`. |
| Content-Type | `application/json` (form-fallback не предоставляется — машинный канал). |
| Семантика | Резолв оригинала в canonical scope (как pull, ADR-0029 §5); `from = original.mail_account_id`; отправка через `SendService` c `in_reply_to_message_id={id}` (threading по оригиналу: `In-Reply-To`/`References`). MIME/SMTP/IMAP-append переиспользуются, не дублируются. |

**Тело запроса** (`ExternalReplyRequest`):

| Поле | Тип | Обяз. | Валидация / default |
| --- | --- | --- | --- |
| `to` | `list[str] \| null` | нет | не передан/`null`/пустой ⇒ `[<оригинал.from_addr>]`. E-mail-паттерн; `max_length=100`. |
| `cc` | `list[str] \| null` | нет | default `null`. E-mail-паттерн; `max_length=100`. |
| `subject` | `str \| null` | нет | не передан/`null` ⇒ `"Re: " + (<оригинал.subject> or "")`. `max_length=998`. |
| `body` | `str` | **да** | непустой (после `strip` длина `≥1`); `max_length=1_048_576` (1 MiB). `text/plain`. |

Нет `from_account_id` (сервер), нет `bcc`, нет `in_reply_to_message_id` (сервер из `{id}`).

**Ответ 200** (`ExternalReplyResponse` — подмножество `SendMessageResponse`):
```json
{ "sent_id": 987, "smtp_message_id": "<generated-msgid@postapp.store>" }
```
`appended_to_sent` **не** во внешнем контракте (best-effort IMAP-append — внутренняя деталь; `SendService` его делает, external DTO опускает).

**Коды:**

| HTTP | code | Когда |
| --- | --- | --- |
| 200 | — | Отправлено (SMTP успех). Тело — `ExternalReplyResponse`. |
| 400 | `validation_error` | Пустой/whitespace `body`; `body`>1 MiB; невалидный e-mail `to`/`cc`; `subject`>998; `>100` адресов. `details.errors[]`. |
| 401 | `not_authenticated` | Нет/неверный ключ **или** `EXTERNAL_API_KEY` пуст. Неперечислимо. |
| 403 | `forbidden` | Ключ валиден, но `EXTERNAL_REPLY_ENABLED=false`. |
| 404 | `not_found` | Письма `{id}` нет **или** оно вне canonical scope (в т.ч. non-canonical дубль). |
| 409 | `oauth_reconsent_required` | Ящик оригинала — `oauth_outlook` с истёкшим consent. |
| 429 | `rate_limited` | Превышен `LIMIT_EXTERNAL_REPLY` (+`Retry-After`). |
| 502 | `smtp_failed` | SMTP-отправка не удалась. |

Sample request:
```http
POST /api/external/messages/12345/reply HTTP/1.1
X-API-Key: <key>
Content-Type: application/json

{"body": "Спасибо, приняли в работу.", "cc": ["ops@corp.example"]}
```

---

## 4f. External write API — mailboxes + tags CRUD (ADR-0039 / ADR-0040)

> **⚠️ Заголовок раздела УСТАРЕЛ: `tags CRUD` СНЯТ** ([ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) — теги в CRM). **Действующий состав раздела** (`backend/app/external/router.py`, строки декораторов `@router.*`; все под `EXTERNAL_API_KEY` + `EXTERNAL_WRITE_ENABLED` + `LIMIT_EXTERNAL_WRITE`): `POST /mailboxes/test` (`:319`), `POST /mailboxes` (`:331`), `PATCH /mailboxes/{id}` (`:344`), `DELETE /mailboxes/{id}` (`:366`), `POST /mailboxes/{id}/sync` (`:378`), `POST /mailboxes/{id}/send` (`:395`, §4f-send, [ADR-0048](./adr/ADR-0048-external-send-contract-and-reply-restore.md)), `POST /mailboxes/oauth/authorize` (`:528`) + `GET /mailboxes/oauth/callback` (`:552`, §4f-oauth, [ADR-0045](./adr/ADR-0045-external-outlook-oauth-headless.md)). Подразделы §4f-teams и §4f-tags — **историчны**.

Источник истины — [ADR-0039](./adr/ADR-0039-external-write-api.md) (mailboxes + read-фильтры) и [ADR-0040](./adr/ADR-0040-global-tags.md) (глобальные теги). Headless-контур для CRM: полный CRUD почт и глобальных тегов под `EXTERNAL_API_KEY`.

### Feature-gate + rate-limit

- `EXTERNAL_API_KEY` (как ADR-0029): пусто ⇒ весь внешний API выключен.
- **`EXTERNAL_WRITE_ENABLED`** (`bool`, default `false`) — отдельный write-гейт для всех mailboxes/tags CRUD (по образцу `EXTERNAL_REPLY_ENABLED`). `false` ⇒ write-эндпоинты отвечают `403 forbidden` даже при валидном ключе. Read (`GET /tags`, `GET /mailboxes`, `GET /messages`) — под `EXTERNAL_API_KEY` без write-гейта.
- **`LIMIT_EXTERNAL_WRITE`** (env `EXTERNAL_WRITE_RATE_LIMIT_PER_MINUTE`, `int`, default `60`, `ge=1`; запросов/мин на IP) — **отдельный** бюджет, не делит с read `120` и reply `30`.
- **Auth-flow (строго, ADR-0029 §4 / ADR-0035 §3):** `consume(LIMIT_EXTERNAL_WRITE, ip)` → `X-API-Key`/`Bearer` (constant-time) → `external_api_enabled` → **write-гейт `EXTERNAL_WRITE_ENABLED`** → валидация тела → delegate. CSRF exempt (`/api/external/`).

### Владелец создаваемого ящика (Q-0039-1)

Ящики, создаваемые через external write, принадлежат техпользователю **`crm-service`** (роль `super_admin`, `group_id NULL`, без `telegram_links`/пароля входа; сидируется в lifespan по образцу `seed_super_admin`). Обоснование и аудит каналов доставки — [ADR-0039](./adr/ADR-0039-external-write-api.md) §Q-0039-1. Следствие: `uq_mail_accounts_user_email (user_id, email)` делает email глобально-уникальным для headless-пути (дубль → `409 conflict`).

### 4f-mailboxes. `/api/external/mailboxes` (write)

Все — под `EXTERNAL_WRITE_ENABLED`. Переиспользуют `accounts/service.py` (create/test/update/delete + SSRF-guard `assert_public_host`). Полные тела/поля — [ADR-0039](./adr/ADR-0039-external-write-api.md) §2.

| Метод / путь | Семантика | Ответ |
| --- | --- | --- |
| `POST /api/external/mailboxes/test` | Проверка IMAP/SMTP-соединения без сохранения. Тело `ExternalMailboxTestRequest{email, imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_ssl, smtp_starttls, smtp_username?, password, smtp_password?}`. **Медленный путь** — ходит на удалённый почтовый сервер (ADR-0047). | `200 {imap_ok:true, smtp_ok:true}`; иначе `422` (`imap_login_failed`/`smtp_login_failed` — сбой логина/коннекта **или исчерпание дедлайна** (`details.detail="timeout"`), `invalid_host` — SSRF-guard) или `400 validation_error` (битое тело). **`502` путь `test` не отдаёт** (см. «Коды»). **Верхняя граница — `MAILBOX_TEST_DEADLINE_SECONDS` (45 с, ADR-0047 §1/§2):** ответ гарантированно доменный, `504` от прокси на этом пути недостижим. Клиент (CRM) обязан дать бюджет ≥ 60 с — `ADR-053` §1 (`MAIL_API_MAILSERVER_TIMEOUT_SEC = 75`). |
| `POST /api/external/mailboxes` | Создание. Тело = поля `test` + `display_name?`, `group_id?` (валидируется на существование, иначе `404 group_not_found`; `null` — без команды). Owner=`crm-service`. **Медленный путь:** до вставки прогоняется тот же connection-test (`accounts/service.py:667`) под дедлайном ADR-0047 — коды таймаута те же, что у `test`. | `201 ExternalMailboxDTO` (расширенный). `409 conflict field=email` при дубле. |
| `PATCH /api/external/mailboxes/{id}` | Правка (креды/`is_active`/`group_id`/хосты; presence-семантика полей). **Медленный путь при смене кредов/хостов:** прогоняется connection-test (`accounts/service.py:891`) под дедлайном ADR-0047 (перенос `group_id` / `is_active` теста не требуют). | `200 ExternalMailboxDTO`; `404 not_found`. |
| `DELETE /api/external/mailboxes/{id}` | Удаление (+каскад вложений/MinIO). | `204`; `404`. |
| `POST /api/external/mailboxes/{id}/sync` | Форс-синк: Redis-маркер `force_sync:{id}` (ex=60). | `202 {queued:true}`; `404`. |

Пароли (`password`/`smtp_password`) — только в запросе; в `ExternalMailboxDTO` не возвращаются; redact в логах.

**Коды (все mailboxes-write):** `200`/`201`/`202`/`204` · `400 validation_error` (битое тело) · `401 not_authenticated` (нет/неверный ключ или `EXTERNAL_API_KEY` пуст) · `403 forbidden` (`EXTERNAL_WRITE_ENABLED=false`) · `404 not_found`/`group_not_found` · `409 conflict` (email) · `422 imap_login_failed`/`smtp_login_failed` (сбой IMAP/SMTP-логина или коннекта при `test`/create/update — переиспользуемый `MailAccountService.test`/`accounts/testers.py`) + `422 invalid_host` (SSRF-guard `assert_public_host`) · `429 rate_limited` (`LIMIT_EXTERNAL_WRITE`). **`502 smtp_failed` здесь не возникает** — это код фактической отправки (send-ядро, ADR-0035), не проверки соединения.

### 4f-send. `POST /api/external/mailboxes/{id}/send` (обобщённая SMTP-отправка, ADR-0043 §3 + [ADR-0048](./adr/ADR-0048-external-send-contract-and-reply-restore.md))

> **✅ Статус реализации (проверено по коду 2026-07-16): РЕАЛИЗОВАН и работает на проде.** `backend/app/external/router.py:395` — `@router.post("/mailboxes/{account_id}/send", response_model=ExternalSendResponse)`; Фаза A2.1 ([ADR-0044](./adr/ADR-0044-decommission-runbook.md) §4) выполнена коммитом `32f320d`, парный релиз CRM — `ac0df07`. Ответ на письмо из CRM восстановлен; долг [TD-059](./100-known-tech-debt.md) **закрыт**. **Это действующий (и единственный) путь исходящей отправки** — message-scoped reply §4d-reply снят.

Заменяет message-scoped reply (§4d-reply, ADR-0035): письма живут в CRM, threading/дефолты формирует CRM, агрегатор — тонкий SMTP-исполнитель.

| | |
| --- | --- |
| Метод / путь | `POST /api/external/mailboxes/{id}/send` (`{id}` — `mail_accounts.id`, `int ≥ 1`) |
| Авторизация | `EXTERNAL_API_KEY` (`X-API-Key` \| `Bearer`) + **`EXTERNAL_WRITE_ENABLED`** (auth-flow ADR-0039 §1) |
| Rate-limit | `LIMIT_EXTERNAL_WRITE` (60/min per IP) — общий машинный бюджет write-API |
| Запрос | `{ to: string[], cc?: string[] \| null, subject?: string \| null, body_text: string, in_reply_to?: string, refs?: string }` |
| **Ответ 200** | **`{ smtp_message_id: string }`** — **`sent_id` НЕТ** ([ADR-0048](./adr/ADR-0048-external-send-contract-and-reply-restore.md) §1: агрегатор не ведёт `sent_messages`; идентификатор отправки выдаёт CRM из своей `mail_sent_messages`) |
| Семантика | `from` = ящик `{id}` (креды/OAuth-токен оттуда); MIME + SMTP + best-effort IMAP APPEND в «Sent» — реюз `send/service.py::_send_core`; заголовки `In-Reply-To`/`References` пишутся **ровно как переданы** (агрегатор их не сочиняет). **Локальная запись отправленного НЕ делается.** |
| Валидация (из ADR-0035, не теряется) | каждый адрес `to`+`cc` — валидный e-mail; `to+cc` **≤ 100**; `subject` **≤ 998**; `body_text` непустой после `strip`, **≤ 1 MiB** |
| Коды | `200` · `400 validation_error` (битое тело) · `401 not_authenticated` · `403 forbidden` (`EXTERNAL_WRITE_ENABLED=false`) · **`404 not_found` = ЯЩИКА `{id}` нет** (не «письма нет» — письма в контракте вовсе нет; CRM маппит это как рассинхрон каталога) · `409 conflict` · `422` (нарушение норм валидации) · **`502 smtp_failed`** (удалённый SMTP отклонил/не ответил) · `429 rate_limited` |
| Бюджет времени | mail-server-путь: агрегатор идёт на удалённый SMTP; верхняя граница — send-инвариант (`send/service.py:41-52`), клиентский бюджет CRM — их `ADR-053` §1.1 (`MAIL_API_MAILSERVER_TIMEOUT_SEC = 75` / deadline `85`) |

**Расширение поверхности (осознанно):** reply мог слать только с ящика хранимого оригинала, send — с любого ящика любому адресату под машинным ключом. Компенсация: ключ + `EXTERNAL_WRITE_ENABLED` (default `false`) + rate-limit; инициатор — CRM под JWT/RBAC пользователя.

### 4f-teams. `/api/external/teams` (write — create + guarded delete, ADR-0042)

> **⚠️ ИСТОРИЧЕСКИЙ РАЗДЕЛ — `POST /api/external/teams` и `DELETE /api/external/teams/{id}` (ADR-0042) СНЯТО демонтажём ([ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md), выполнено на проде 2026-07-15).** Описанного в коде/проде агрегатора НЕТ; текст сохранён как record исходного решения. НЕ реализовывать. Роутов в коде нет; `TD-048` (уборка осиротевших пустых групп) вместе с ними неактуален для агрегатора.

Источник истины — [ADR-0042](./adr/ADR-0042-external-team-create-delete.md). Ленивый провижининг почтовых групп из headless-CRM (CRM `ADR-043`): CRM создаёт группу (имя = имя CRM-команды) по первому добавлению почты, получает `id`, привязывает `teams.mail_group_id`, создаёт ящик с этим `group_id`. Все — под `EXTERNAL_WRITE_ENABLED` (auth-flow ADR-0039 §1, `_authorize_write`). `«team» = внутренний groups`-ряд (как read `GET /api/external/teams`, ADR-0037); `ExternalTeamDTO.id` == значение, передаваемое затем как `group_id` при создании ящика.

| Метод / путь | Семантика | Ответ |
| --- | --- | --- |
| `POST /api/external/teams` | Создание **leaderless**-группы. Тело `ExternalTeamCreateRequest{name}` (`min_length=1, max_length=100` = `ck_groups_name_length`). `GroupsRepo.insert(name, leader_user_id=None)`; owner/actor аудита = `crm-service`. **НЕ идемпотентно, дубль имени НЕ конфликтит** (`groups.name` не UNIQUE — реюз/`409` по имени отвергнут, связь по `id`; ADR-0042 §2). | `201 ExternalTeamDTO{id:int, name:str}` (`leader_user_id` всегда null, не раскрывается). |
| `DELETE /api/external/teams/{id}` | Guarded-реклейм **пустой** группы в **локальной транзакции с `SELECT ... FOR UPDATE` на строке `groups`** (анти-TOCTOU; лок короткий, без сетевого I/O — легитимен, в отличие от отвергнутого CRM-side row-lock через внешний вызов). Затем EXISTS-проверки: отказ, если есть ящики (`mail_accounts.group_id=id`) / участники (`user_groups`) / лидер — FK `mail_accounts.group_id` = `ON DELETE SET NULL`, тихое обнуление недопустимо. | `204` (пустая); `409 conflict` (непустая); `404 not_found` (нет id). |

**Коды (teams-write):** `POST` — `201` · `400 validation_error` (`name` вне 1..100 / битое тело; класс `ValidationError`) · `401 not_authenticated` (нет/неверный ключ или `EXTERNAL_API_KEY` пуст) · `403 forbidden` (`EXTERNAL_WRITE_ENABLED=false`) · `429 rate_limited` (`LIMIT_EXTERNAL_WRITE`). **Нет `404`/`409` на `POST`.** `DELETE` — `204` · `401` · `403` · `404 not_found` (класс `NotFoundError`, `code="not_found"`) · `409 conflict` (класс `ConflictError`, `code="conflict"` — группа непустая) · `429`.

**Не добавлено (ADR-0042 §4):** `PATCH /api/external/teams/{id}` (rename) — имя группы косметическое в headless-режиме; отложено (tech-debt). `GET /api/external/teams` (список, ADR-0037) — без изменений.

### 4f-tags. `/api/external/tags` (CRUD, глобальные теги ADR-0040)

> **⚠️ ИСТОРИЧЕСКИЙ РАЗДЕЛ — весь раздел `/api/external/tags` CRUD СНЯТО демонтажём ([ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md), выполнено на проде 2026-07-15).** Описанного в коде/проде агрегатора НЕТ; текст сохранён как record исходного решения. НЕ реализовывать. Теги целиком переехали в CRM (движок матчинга перенесён ПОБУКВЕННО — CRM `ADR-044` §5); таблицы `tags`/`tag_rules`/`message_tags` дропнуты, модуль `backend/app/tags/` удалён.

Все от имени глобального владельца (`tags.user_id IS NULL`). Read (`GET`) — под `EXTERNAL_API_KEY`; write — дополнительно под `EXTERNAL_WRITE_ENABLED`. Модель глобальных тегов — [ADR-0040](./adr/ADR-0040-global-tags.md).

| Метод / путь | Тело | Ответ |
| --- | --- | --- |
| `GET /api/external/tags` | — | `200 {tags:[ExternalTagFullDTO]}` |
| `POST /api/external/tags` | `{name, color, match_mode?}` (`match_mode` default `any`; `color` ∈ палитра `^#[0-9A-Fa-f]{6}$`) | `201 ExternalTagFullDTO`; `409 conflict` (имя занято) |
| `PATCH /api/external/tags/{id}` | `{name?, color?, match_mode?}` | `200 ExternalTagFullDTO`; `404` |
| `DELETE /api/external/tags/{id}` | — | `204`; `409 conflict` (builtin — удалять нельзя); `404` |
| `POST /api/external/tags/{id}/rules` | `{type, pattern}` (`type ∈ {subject_contains, body_contains, sender_contains, sender_exact}`; `pattern` 1..256) | `201 {id, type, pattern, created_at}`; `404` |
| `DELETE /api/external/tags/{id}/rules/{rule_id}` | — | `204`; `404` |
| `POST /api/external/tags/{id}/apply-to-existing` | — | `200 {applied_count}`; `404`; `422` (> `APPLY_TO_EXISTING_LIMIT`) |

`ExternalTagFullDTO = {id, name, color, match_mode, is_builtin, rules:[{id, type, pattern, created_at}], created_at, updated_at}`.

**Коды (tags):** как выше + `400/422 validation_error` (color/name/type), `401`, `403` (write-гейт), `429`.

### Read-фильтры: расширение (ADR-0039 §3/§4)

> **⚠️ Раздел частично ИСТОРИЧЕН.** **В силе:** повторяемый AND-комбинируемый фильтр `mail_account_id` (`list[int]`), эффективный набор = canonical ∩ ящики, незнакомый id → **пустая страница** (не `404`), кода `field=filter` нет. **СНЯТО:** фильтр **`group_id`** в `GET /api/external/messages` и `GET /api/external/mailboxes`, а также поле `group_id` в `ExternalMailboxDTO` — групп в агрегаторе нет ([ADR-0044](./adr/ADR-0044-decommission-runbook.md) Фаза C дропнула `mail_accounts.group_id`). Поля `is_active`/`last_synced_at`/`last_sync_error`/`consecutive_failures` — **в силе**.

- **`GET /api/external/messages`** — `group_id` и `mail_account_id` становятся **повторяемыми** (`list[int]`; `?group_id=1&group_id=2`) **и AND-комбинируемыми** (взаимоисключение ADR-0037 **снято**, supersede). Эффективный набор = `canonical ∩ (⋃ accounts of group_id, если задан) ∩ (set(mail_account_id), если задан)`; пустое пересечение → пустая страница (не 404); незнакомый/чужой/non-canonical id просто не добавляется в пересечение; **BC** single-filter. Кода `field=filter` нет. Мотивация — безопасная ролевая видимость CRM (scope-`group_id` AND пользовательский `mail_account_id`).
- **`GET /api/external/mailboxes`** — новые query `is_active: bool|null` (None=все) и повторяемый `group_id: list[int]`; **`ExternalMailboxDTO` += `last_synced_at:datetime|null`, `last_sync_error:str|null`, `consecutive_failures:int`** (аддитивно; секреты по-прежнему не раскрываются).

### 4f-oauth. `/api/external/mailboxes/oauth/*` (External Outlook OAuth, ADR-0045)

Источник истины — [ADR-0045](./adr/ADR-0045-external-outlook-oauth-headless.md) (парный CRM `ADR-045`; закрывает `TD-052`). Headless-восстановление Outlook OAuth-consent для добавления/переподключения ящиков из CRM. `OutlookOAuthService` (реюз `build_authorize_url`/`exchange_code`, ADR-0025) остаётся и адаптирован: `OAuthState = {code_verifier, crm_state}`, owner создаваемого ящика = **`crm-service`**, **без `group_id`** (колонка дропнута `ADR-0044`).

Требуется `outlook_oauth_enabled` (`bool(OUTLOOK_CLIENT_ID and OUTLOOK_CLIENT_SECRET)`); при `false` оба эндпоинта → `404 not_found` (фича скрыта, симметрично старому `_require_enabled`). `redirect_uri` (Azure App + env `OUTLOOK_REDIRECT_URI`) = **`{APP_BASE_URL}/api/external/mailboxes/oauth/callback`** (обновляется при cut-over — devops).

| Метод / путь | Auth | Семантика | Ответ |
| --- | --- | --- | --- |
| `POST /api/external/mailboxes/oauth/authorize` | `EXTERNAL_WRITE_ENABLED` (auth-flow ADR-0039 §1) | Тело `ExternalOAuthAuthorizeRequest{crm_state: str}` (непрозрачный CRM-токен, ≤512; агрегатор не интерпретирует). `build_authorize_url(crm_state)`: state+PKCE S256 → Redis `oauth_state:{state}={code_verifier, crm_state}` TTL `OUTLOOK_OAUTH_STATE_TTL_SECONDS`; собирает Microsoft authorize URL. | `200 ExternalOAuthAuthorizeResponse{authorize_url:str, state:str}`; `404 not_found` (`outlook_oauth_enabled=false`); `401`/`403`/`400`/`429`. |
| `GET /api/external/mailboxes/oauth/callback` | одноразовый `state` в Redis + PKCE (**без ключа/сессии** — Microsoft-редирект; CSRF-exempt по префиксу) | Query `code`/`state`/`error`/`error_description`. Атомарный GET+DEL `state` → `exchange_code` (code→токены, resolve email из `id_token`, create owner=`crm-service`/relink existing) → уведомить CRM (§ниже) → минимальная self-contained HTML-страница **«Outlook подключён — вернитесь в CRM»**. `error`/битый state/сбой обмена → HTML-страница ошибки (ящик НЕ создаётся). | `200` (HTML success/error); `404` (`outlook_oauth_enabled=false`). |

**Уведомление CRM (server-to-server, HMAC).** После успешного create/relink (ДО первого push письма ящика) агрегатор POST'ит **`{CRM_OAUTH_INGEST_URL}` (= CRM `/api/mail/oauth/ingest`)** тем же HMAC-механизмом и секретом **`CRM_PUSH_SECRET`**, что `/api/mail/ingest` (`ADR-0043` §2): заголовки `X-Mail-Signature: sha256=<hex>` + `X-Mail-Timestamp`, каноническая подпись `str(ts).encode("ascii") + b"." + raw_body_bytes`. Тело `{crm_state, mail_account_id:int, email:str, display_name:str|null, is_active:bool}`. **Connect-only-ретрай** (анти-двойная-запись; CRM upsert идемпотентен по `mail_account_id`); best-effort — сбой не откатывает созданный ящик (reconcile добирает, CRM `TD-047`). `CRM_OAUTH_INGEST_URL` пуст → уведомление не шлётся (endpoint фактически выключен).

**Env (ADR-0045 §4, амендмент `ADR-0044` Phase G — НЕ удалять):** `OUTLOOK_CLIENT_ID`, `OUTLOOK_CLIENT_SECRET` (redact), `OUTLOOK_REDIRECT_URI` (→ callback выше), `OUTLOOK_TENANT`, `OUTLOOK_OAUTH_STATE_TTL_SECONDS`; **новый** `CRM_OAUTH_INGEST_URL`; реюз `CRM_PUSH_SECRET`.

---

## 4e. Mail forwarding — переадресация писем команды лидеру (ADR-0034)

> **⚠️ ИСТОРИЧЕСКИЙ РАЗДЕЛ — подсистема forwarding (таблицы `group_forwarding`/`message_forwards`, job `forward_dispatch`, CRUD `/api/forwarding/me`) СНЯТО демонтажём ([ADR-0043](./adr/ADR-0043-strip-to-connector-push-to-crm.md) / [ADR-0044](./adr/ADR-0044-decommission-runbook.md), выполнено на проде 2026-07-15).** Описанное ниже в коде/проде агрегатора НЕ существует; текст сохранён как record исходного решения. НЕ реализовывать и не принимать за действующий контракт. **Актуально:** пересылка/маршрутизация по команде — на стороне CRM; SMTP-ядро реюзается обобщённым send (§4f-send).

Источник истины — [ADR-0034](./adr/ADR-0034-leader-mail-forwarding.md). Конфигурация переадресации входящих писем команды на e-mail лидера — **одна запись на команду** (`group_forwarding.group_id UNIQUE`). Пересылка выполняется worker'ом (см. `05-modules.md` §20 + §14.4): при получении нового письма любым ящиком команды сервис пересылает его целиком (`body_text`+`body_html`+вложения) на `forward_to` **SMTP-кредами получившего ящика** (`From`=ящик, `To`=лидер, блок «пересланное сообщение»). Секрета/URL нет (в отличие от webhooks ADR-0023) — отправка идёт через SMTP самого ящика; отдельной URL-SSRF-проверки не требуется.

### Авторизация всех endpoint'ов

- `group_leader` — управляет переадресацией своей команды (`scope.group_id`). Передача `?group_id=` **запрещена** (иначе `400 validation_error` `field=group_id`).
- `super_admin` — управляет переадресацией любой команды; **обязан** передать `?group_id=<id>` в каждом запросе.
- `group_member` — `403 forbidden` на всех endpoint'ах.

ACL — копия `WebhooksService._resolve_target_group_id` (ADR-0023 §2). Все state-changing endpoints — под CSRF (ADR-0010).

### `GET /my/integrations`

Та же HTML-страница, что и для webhooks (ADR-0023 §2.7). При `group_leader`/`super_admin` рендерит секцию «Переадресация» (см. `08-frontend.md`). `group_member` → `302 /`.

### `GET /api/forwarding/me`

| | |
| --- | --- |
| Query | `group_id?: int` — обязателен для super_admin; для group_leader запрещён. |
| 200 | `{id, group_id, forward_to, is_active, created_at, updated_at}` — **без секретов** (их нет). |
| 404 | `not_found` — у команды переадресация не настроена. |
| 403 | `forbidden`. |

### `PUT /api/forwarding/me` (upsert)

| | |
| --- | --- |
| Запрос | JSON `{forward_to: str, is_active?: bool}`; либо form-encoded (см. ниже). |
| Query | `group_id?: int` — для super_admin. |
| Валидация | `forward_to` — ручной e-mail-паттерн (`accounts/schemas.py`: один `@`, домен с точкой, без `..`, длина 3..254). `is_active` default `true` при создании; при обновлении — сохраняет прежнее, если не передан. |
| Поведение | Upsert: если записи для команды нет — создаётся (`201`); иначе обновляется (`200`). `created_at` при обновлении не меняется (anchor «не флудим историей»). |
| Rate-limit | 30/час per `group_id`. |
| 200 / 201 | тело как в `GET`. |
| 400 | `validation_error` (`field=forward_to` — невалидный/пустой e-mail; `field=group_id` — нарушение ACL-правила query). |
| 403 | `forbidden`. |
| Audit | `forwarding_updated` (`details = {group_id, forward_to, is_active}`). |

Sample request (JSON):
```http
PUT /api/forwarding/me HTTP/1.1
Content-Type: application/json
Cookie: mas_session=...; mas_csrf=...
X-CSRF-Token: ...

{"forward_to": "lead@example.com", "is_active": true}
```

Sample response:
```json
HTTP/1.1 200 OK
Content-Type: application/json

{
  "id": 3,
  "group_id": 5,
  "forward_to": "lead@example.com",
  "is_active": true,
  "created_at": "2026-07-03T10:00:00Z",
  "updated_at": "2026-07-03T10:05:00Z"
}
```

##### Form-encoded request (no-JS)

Через method override (whitelist exact-путь `/api/forwarding/me`, см. «Form-encoded fallback»):
```
POST /api/forwarding/me
Content-Type: application/x-www-form-urlencoded

_method=PUT&forward_to=lead@example.com&is_active=on&csrf_token=...
```
Success → `303 /my/integrations` + flash «Переадресация сохранена». Validation error → re-render `integrations.html` с `form_errors`.

### `DELETE /api/forwarding/me`

| | |
| --- | --- |
| Query | `group_id?: int` — для super_admin. |
| Поведение | `DELETE FROM group_forwarding WHERE group_id=:gid`. История `message_forwards` **не** трогается (живёт по retention `messages`). Будущая переадресация прекращается. |
| Rate-limit | 10/час per `group_id`. |
| 204 | success. |
| 404 | `not_found`. |
| 403 | `forbidden`. |
| Audit | `forwarding_deleted` (`details = {group_id}`). |
| Form-fallback | `POST /api/forwarding/me/delete` + `_method=DELETE`. Success → `303 /my/integrations` + flash «Переадресация удалена». |

---

## 5. Health & ops

### `GET /healthz`
| Доступ | публичный |
| 200 | `{"status":"ok"}` (только проверка процесса). |

### `GET /readyz`
| Доступ | публичный |
| Поведение | Проверяет Postgres `SELECT 1`, Redis `PING`, MinIO `head_bucket(mail-attachments)`. |
| 200 | `{"db":"ok","redis":"ok","s3":"ok"}` |
| 503 | `dependency_unavailable` + `details: {db,redis,s3}` |

### `GET /metrics` (опционально, см. tech-debt)
Не реализуется в первой итерации.

---

## 6. OpenAPI

FastAPI автогенерит OpenAPI 3.1. UI:

- `/openapi.json` — публично.
- `/docs` — Swagger UI, **доступен только в dev** (env `ENABLE_DOCS=true`); в prod возвращает 404.
- `/redoc` — disabled всегда.

---

## 7. Versioning

Префикс `/api` без версии. На текущей итерации single-version. Если потребуется breaking change — добавим `/api/v2`.

---

## 8. Сводная таблица всех endpoints

> **⚠️ Сводная таблица ниже — ДО-демонтажная и НЕ отражает прод.** Актуальная поверхность агрегатора (`backend/app/main.py:99-100` — смонтированы только `external_router` + `health_router`):
>
> | Endpoint | Гейт |
> | --- | --- |
> | `GET /healthz`, `GET /readyz` | — |
> | `GET /api/external/messages` | `EXTERNAL_API_KEY` + `LIMIT_EXTERNAL_API` |
> | `GET /api/external/mailboxes` | `EXTERNAL_API_KEY` + `LIMIT_EXTERNAL_API` |
> | `POST /api/external/mailboxes/test`, `POST /api/external/mailboxes`, `PATCH`/`DELETE /api/external/mailboxes/{id}`, `POST /api/external/mailboxes/{id}/sync` | + `EXTERNAL_WRITE_ENABLED` + `LIMIT_EXTERNAL_WRITE` |
> | `POST /api/external/mailboxes/{id}/send` ([ADR-0048](./adr/ADR-0048-external-send-contract-and-reply-restore.md)) | + `EXTERNAL_WRITE_ENABLED` + `LIMIT_EXTERNAL_WRITE` |
> | `POST /api/external/mailboxes/oauth/authorize`, `GET /api/external/mailboxes/oauth/callback` ([ADR-0045](./adr/ADR-0045-external-outlook-oauth-headless.md)) | + `EXTERNAL_WRITE_ENABLED` |
>
> Всё остальное в таблице ниже (HTML-страницы, `/api/admin/*`, `/api/mail-accounts*`, `/api/messages*`, `/api/tags*`, `/api/telegram/*`, `/api/webhooks/*`, `/api/forwarding/*`, `/api/oauth/outlook/*`, `/api/external/teams`, `/api/external/tags*`, `POST /api/external/messages/{id}/reply`) — **СНЯТО, отдаёт `404`**.

Колонка **Form** отмечает endpoints, поддерживающие form-encoded fallback (см. раздел "Form-encoded fallback" выше и ADR-0015). Значение `yes` = endpoint принимает оба content-type (json + form) и при form-запросе отвечает 303+flash. Sibling-роуты `.../delete` существуют для override DELETE через `POST + _method=DELETE`.

| Метод | Путь | Auth | CSRF | Rate-limit | Form | Назначение |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/login` | none | — | — | — | login form (step-1 username) |
| POST | `/login` | none | exempt | 30/15min per IP | — | step-1 of two-step login (ADR-0016): submit username, redirect to `/login/password` или `/set-password` |
| GET | `/login/password` | mas_login cookie | — | — | — | login form (step-2 password) |
| POST | `/login/password` | none | exempt | 5/15min per username\|IP | — | step-2: verify password, create session |
| GET | `/set-password` | setup-session | — | — | — | set-password form |
| POST | `/set-password` | setup-session | yes | 5/15min | — | set password (всегда form-encoded; уже native form-сценарий) |
| POST | `/logout` | user | yes | — | — | logout (всегда form-POST → 302 на `/login`) |
| GET | `/` | user | — | — | — | inbox |
| GET | `/messages/{id}` | user | — | — | — | message view |
| GET | `/compose` | user | — | — | — | compose form |
| GET | `/accounts` | user | — | — | — | accounts list |
| GET | `/accounts/new` | user | — | — | — | add account form |
| GET | `/accounts/{id}/edit` | user | — | — | — | edit account form |
| GET | `/api/me` | user | — | — | — | self info |
| GET | `/api/mail-accounts` | user | — | — | — | list |
| POST | `/api/mail-accounts` | user | yes | 10/h | yes | add |
| POST | `/api/mail-accounts/test` | user | yes | 10/h | — | test (только AJAX, no-JS не использует) |
| GET | `/api/mail-accounts/{id}` | user | — | — | — | get |
| PATCH | `/api/mail-accounts/{id}` | user | yes | 10/h | yes | update; через override: `POST` + `_method=PATCH` |
| POST | `/api/mail-accounts/{id}` | user | yes | 10/h | yes | (form-fallback к PATCH через `_method=PATCH`; не используется отдельно) |
| DELETE | `/api/mail-accounts/{id}` | user | yes | 10/h | yes | delete (canonical) |
| POST | `/api/mail-accounts/{id}/delete` | user | yes | 10/h | yes | form-fallback delete (`_method=DELETE`) |
| POST | `/api/mail-accounts/{id}/sync-now` | user | yes | 5/h per acc | yes | force sync |
| GET | `/api/messages` | user | — | — | — | list |
| GET | `/api/messages/{id}` | user | — | — | — | get |
| POST | `/api/messages/{id}/mark-read` | user | yes | — | — | toggle read (только AJAX) |
| GET | `/api/messages/{id}/attachments/{aid}` | user | — | — | — | download |
| POST | `/api/messages/send` | user | yes | 30/h | yes | send |
| GET | `/tags` | user | — | — | — | tags list page |
| GET | `/tags/new` | user | — | — | — | new-tag form |
| GET | `/tags/{id}/edit` | user | — | — | — | edit-tag form |
| GET | `/api/tags` | user | — | — | — | list tags |
| POST | `/api/tags` | user | yes | 30/h | yes | create tag |
| GET | `/api/tags/{id}` | user | — | — | — | get tag |
| PATCH | `/api/tags/{id}` | user | yes | 30/h | yes | update name/color; через override: `POST` + `_method=PATCH` |
| POST | `/api/tags/{id}` | user | yes | 30/h | yes | (form-fallback к PATCH через `_method=PATCH`) |
| DELETE | `/api/tags/{id}` | user | yes | 30/h | yes | delete (canonical) |
| POST | `/api/tags/{id}/delete` | user | yes | 30/h | yes | form-fallback delete (`_method=DELETE`) |
| GET | `/api/tags/{id}/rules` | user | — | — | — | list rules |
| POST | `/api/tags/{id}/rules` | user | yes | 30/h | yes | add rule |
| DELETE | `/api/tags/{id}/rules/{rule_id}` | user | yes | 30/h | yes | delete rule (canonical) |
| POST | `/api/tags/{id}/rules/{rule_id}/delete` | user | yes | 30/h | yes | form-fallback delete (`_method=DELETE`) |
| POST | `/api/tags/{id}/apply-to-existing` | user | yes | 50/h per user | yes | bulk apply tag to existing messages |
| GET | `/admin` | super_admin | — | — | — | admin dashboard |
| GET | `/admin/audit` | super_admin | — | — | — | audit page |
| GET | `/admin/groups` | super_admin | — | — | — | groups list page (ADR-0019) |
| GET | `/admin/groups/new` | super_admin | — | — | — | new-group form |
| GET | `/admin/groups/{id}/edit` | super_admin | — | — | — | edit-group form |
| GET | `/api/admin/users` | super_admin | — | — | — | list users |
| POST | `/api/admin/users` | super_admin | yes | 50/h | yes | create user (with role+group) |
| PATCH | `/api/admin/users/{id}` | super_admin | yes | 50/h | yes | update role/group/display_name; через override |
| POST | `/api/admin/users/{id}` | super_admin | yes | 50/h | yes | (form-fallback к PATCH через `_method=PATCH`) |
| POST | `/api/admin/users/{id}/reset` | super_admin | yes | 50/h | yes | reset password (+ опц. `password` → admin-set обратимой копии, ADR-0038) |
| GET | `/api/admin/users/{id}/password` | super_admin | — | 30/min per actor (`LIMIT_ADMIN_PASSWORD_REVEAL`) | — | **ADR-0038:** показать пароль входа (decrypt `password_encrypted`); `404 password_not_set` при NULL; audit `user_password_revealed` на каждый показ; значение не в логи |
| DELETE | `/api/admin/users/{id}` | super_admin | yes | 50/h | yes | delete user (canonical) |
| POST | `/api/admin/users/{id}/delete` | super_admin | yes | 50/h | yes | form-fallback delete (`_method=DELETE`) |
| POST | `/api/admin/users/{id}/groups` | super_admin | yes | 50/h | yes | add membership (ADR-0030); цель ≠ super_admin; идемпотентно через UNIQUE |
| DELETE | `/api/admin/users/{id}/groups/{group_id}` | super_admin | yes | 50/h | yes | remove additional membership (ADR-0030, canonical); нельзя удалить домашнее |
| POST | `/api/admin/users/{id}/groups/{group_id}/delete` | super_admin | yes | 50/h | yes | form-fallback delete membership (`_method=DELETE`, ADR-0030) |
| GET | `/api/admin/groups` | super_admin | — | — | — | list groups (ADR-0019) |
| POST | `/api/admin/groups` | super_admin | yes | 50/h | yes | create group |
| GET | `/api/admin/groups/{id}` | super_admin | — | — | — | get group with members |
| PATCH | `/api/admin/groups/{id}` | super_admin | yes | 50/h | yes | rename; через override |
| POST | `/api/admin/groups/{id}` | super_admin | yes | 50/h | yes | (form-fallback к PATCH) |
| DELETE | `/api/admin/groups/{id}` | super_admin | yes | 50/h | yes | delete (canonical) |
| POST | `/api/admin/groups/{id}/delete` | super_admin | yes | 50/h | yes | form-fallback delete (`_method=DELETE`) |
| GET | `/api/admin/audit` | super_admin | — | — | — | audit log |
| POST | `/api/telegram/webhook/{secret}` | secret in URL + header | exempt | 60/min per IP | — | Telegram bot webhook: `/start` → WebApp launcher button (ADR-0018); `callback_query` `msg:{id}` от кнопки уведомления → `callback_handler` шлёт тело письма в чат (Bug-fix #5, ADR-0022 §2.5/§2.6) |
| POST | `/api/telegram/push-webhook/{bot_name}` | per-бот secret in header | exempt | 60/min per IP | — | **round-42 (ADR-0027 §10):** push-бот webhook. ТОЛЬКО `callback_query` `msg:{id}`; авторизация `from.id ∈ ADMIN_TELEGRAM_IDS` + group-match → тело письма в чат токеном бота. Неизвестный/ненастроенный `bot_name`/secret → `not_found` |
| POST | `/api/telegram/auth` | initData HMAC | exempt | 30/min per IP + 10/min per tg_user_id | — | Persistent SSO: validate Telegram WebApp initData, выпустить session либо pending-cookie; см. ADR-0022 |
| GET | `/api/telegram/links` | user | — | — | — | список TG-привязок текущего user'а (ADR-0024) |
| POST | `/api/telegram/links` | user | yes | 10/h | — | добавить TG-привязку при активной сессии (ADR-0024) |
| DELETE | `/api/telegram/links/{tg_user_id}` | user | yes | 10/h | yes | отвязать конкретный TG (ADR-0024) |
| POST | `/api/telegram/links/{tg_user_id}/delete` | user | yes | 10/h | yes | form-fallback delete (`_method=DELETE`) |
| PATCH | `/api/me/settings` | user | yes | — | — | user preferences (tg_notifications_enabled); см. ADR-0022 |
| GET | `/api/oauth/outlook/authorize` | user | — | 30/h | — | сгенерить Microsoft authorize URL + state (ADR-0025) |
| GET | `/api/oauth/outlook/callback` | state in Redis | exempt | 30/min per IP | — | OAuth callback: code→токены, create mail_account (ADR-0025) |
| GET | `/api/external/messages` | `EXTERNAL_API_KEY` (X-API-Key \| Bearer) | exempt | 120/min per IP (`LIMIT_EXTERNAL_API`) | — | **ADR-0029:** external pull-API — keyset по `messages.id`, ВСЕ письма системы, сырое полное тело, no attachments. **ADR-0036:** + backward/latest режим `order=asc\|desc` (`desc`+`before_id` → newest-first лента, курсор `next_before_id`); forward BC неизменен, тот же rate-limit/auth. **ADR-0037 + ADR-0039 §3:** + опц. фильтры `mail_account_id`/`group_id` (`ge=1`, оба режима, **повторяемые `list[int]`**, сужают набор ∩ canonical; **AND-комбинируемы** — заданные вместе НЕ ошибка (взаимоисключение ADR-0037 снято, кода `field=filter` нет), пустое пересечение/несовпадающий id → пустая страница, не 404) |
| GET | `/api/external/teams` | `EXTERNAL_API_KEY` (X-API-Key \| Bearer) | exempt | 120/min per IP (`LIMIT_EXTERNAL_API`) | — | **ADR-0037:** список команд `{teams:[{id,name}]}` (`GroupsRepo.list_all_groups()`), super_admin-visibility, минимальная проекция |
| POST | `/api/external/teams` | `EXTERNAL_API_KEY` + `EXTERNAL_WRITE_ENABLED` | exempt | 60/min per IP (`LIMIT_EXTERNAL_WRITE`) | — | **ADR-0042:** создать leaderless-группу `{name}` → `201 {id,name}`; ленивый провижининг из CRM; НЕ идемпотентно, дубль имени НЕ конфликтит (`groups.name` не UNIQUE); owner=`crm-service` |
| DELETE | `/api/external/teams/{id}` | `EXTERNAL_API_KEY` + `EXTERNAL_WRITE_ENABLED` | exempt | 60/min per IP (`LIMIT_EXTERNAL_WRITE`) | — | **ADR-0042:** guarded-реклейм **пустой** группы → `204`; непустая (ящики/участники/лидер) → `409 conflict`; нет id → `404 not_found` |
| GET | `/api/external/mailboxes` | `EXTERNAL_API_KEY` (X-API-Key \| Bearer) | exempt | 120/min per IP (`LIMIT_EXTERNAL_API`) | — | **ADR-0037:** список ящиков `{mailboxes:[{id,email,display_name,group_id,is_active}]}` (canonical-дедуп), для дропдауна/счётчиков/маппинга ящик→команда CRM |
| POST | `/api/external/messages/{id}/reply` | `EXTERNAL_API_KEY` (X-API-Key \| Bearer) + `EXTERNAL_REPLY_ENABLED` | exempt | 30/min per IP (`LIMIT_EXTERNAL_REPLY`) | — | **ADR-0035:** external reply — ответ на существующее письмо; `from`=ящик оригинала (не выбирается), threading по `{id}`; write opt-in (`EXTERNAL_REPLY_ENABLED`, default off). **⚠️ Заменяется обобщённым send (строка ниже) и СНИМАЕТСЯ в Фазе A2.2** ([ADR-0048](./adr/ADR-0048-external-send-contract-and-reply-restore.md) §3). CRM его **не зовёт и звать не должна** (id писем CRM ≠ id агрегатора после cut-over + ретенция 30 дней — `ADR-0048` §5) |
| POST | `/api/external/mailboxes/{id}/send` | `EXTERNAL_API_KEY` (X-API-Key \| Bearer) + `EXTERNAL_WRITE_ENABLED` | exempt | 60/min per IP (`LIMIT_EXTERNAL_WRITE`) | — | **ADR-0043 §3 + [ADR-0048](./adr/ADR-0048-external-send-contract-and-reply-restore.md):** обобщённая SMTP-отправка от имени ящика `{id}` (reply/forward из CRM). Запрос `{to, cc?, subject?, body_text, in_reply_to?, refs?}` → **`200 {smtp_message_id}`** (без `sent_id`); `404` = ящик не найден; `502 smtp_failed` — SMTP отклонил. **В коде ЕЩЁ НЕ РЕАЛИЗОВАН** (Фаза A2.1) — reply из CRM на проде сломан, [TD-059](./100-known-tech-debt.md) |
| GET | `/my/integrations` | group_leader \| super_admin | — | — | — | webhook config page (ADR-0023) |
| GET | `/api/webhooks/me` | group_leader \| super_admin | — | — | — | get webhook config (no secret) |
| POST | `/api/webhooks/me` | group_leader \| super_admin | yes | 10/h per group | yes | create webhook + one-shot secret reveal |
| PATCH | `/api/webhooks/me` | group_leader \| super_admin | yes | 30/h per webhook | yes | update url / is_active; через override |
| DELETE | `/api/webhooks/me` | group_leader \| super_admin | yes | 10/h per webhook | yes | delete (canonical) |
| POST | `/api/webhooks/me/delete` | group_leader \| super_admin | yes | 10/h per webhook | yes | form-fallback delete (`_method=DELETE`) |
| POST | `/api/webhooks/me/rotate-secret` | group_leader \| super_admin | yes | 5/h per webhook | yes | rotate secret + reveal one-shot |
| POST | `/api/webhooks/me/test` | group_leader \| super_admin | yes | 10/h per webhook | yes | synchronous test POST to receiver |
| GET | `/api/forwarding/me` | group_leader \| super_admin | — | — | — | **ADR-0034:** get forwarding config |
| PUT | `/api/forwarding/me` | group_leader \| super_admin | yes | 30/h per group | yes | **ADR-0034:** upsert forward_to / is_active; через override |
| DELETE | `/api/forwarding/me` | group_leader \| super_admin | yes | 10/h per group | yes | **ADR-0034:** delete (canonical) |
| POST | `/api/forwarding/me/delete` | group_leader \| super_admin | yes | 10/h per group | yes | **ADR-0034:** form-fallback delete (`_method=DELETE`) |
| GET | `/healthz` | none | — | — | — | liveness |
| GET | `/readyz` | none | — | — | — | readiness |
