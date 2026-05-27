# 04. API Contracts

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
| 422 | `imap_login_failed`, `smtp_login_failed`, `smtp_failed` | Ошибки внешних систем при тесте/отправке. |
| 423 | `account_locked` | login lockout (`Retry-After` присутствует). |
| 429 | `rate_limited` | (`Retry-After` присутствует). |
| 500 | `internal_error` | Непредвиденная ошибка (тело не утечка детали). |
| 502 | `upstream_error` | Сбой IMAP/SMTP вне auth. |
| 503 | `dependency_unavailable` | Postgres/Redis/MinIO недоступны (от healthcheck). |
| 400 | `method_override_not_allowed` | Запрос `POST` с полем `_method` пришёл на роут, не входящий в whitelist form-fallback (см. ниже). |
| 400 | `cannot_delete_builtin_tag` | DELETE на тег с `is_builtin=true`. |
| 422 | `tag_apply_too_many` | `apply_to_existing=true` при числе писем у пользователя > 100 000 (см. ADR-0017 §7). |
| 400 | `group_id_must_be_null_for_new_leader` | `POST /api/admin/users` с `role=group_leader` И заданным `group_id` (новый лидер всегда auto-create'ит группу; см. ADR-0019 §5). |
| 400 | `group_has_members` | `DELETE /api/admin/groups/{id}` пока в группе остались участники или лидер; super-admin сначала переводит/удаляет их. |
| 400 | `group_leader_consistency_violation` | Инвариант лидерства нарушен (raw из триггера; обычно ловится backend'ом перед SQL). |
| 400 | `cannot_delete_group_with_super_admin_target` | Внутренняя защита от ошибочного удаления группы, ссылающейся на super_admin как лидера (по инварианту невозможно, но defensive). |
| 404 | `group_not_found` | Запрос про группу, которой нет (или у запрашивающего нет прав её видеть). |
| 403 | `user_not_in_group_scope` | Лидер пытается выполнить действие на пользователя/аккаунт вне своей группы. |
| 401 | `invalid_init_data` | `POST /api/telegram/auth`: HMAC-подпись Telegram `init_data` некорректна. См. [ADR-0022](./adr/ADR-0022-telegram-sso-and-notifications.md) §1.2. |
| 401 | `init_data_expired` | `POST /api/telegram/auth`: `auth_date` в `init_data` старше 5 минут. |
| 400 | `webhook_url_private_ip` | `POST/PATCH /api/webhooks/me`: URL резолвится в приватный CIDR / localhost. SSRF-защита. См. [ADR-0023](./adr/ADR-0023-outbound-webhooks.md) §4.3. |
| 409 | `webhook_already_exists` | `POST /api/webhooks/me`: у группы уже есть webhook (`UNIQUE(group_id)`). Используется `PATCH` для update или `DELETE` + `POST` для пересоздания. |

---

## Form-encoded fallback (no-JS support)

Источник истины — [ADR-0015](./adr/ADR-0015-no-js-fallback.md). Требование вытекает из `08-frontend.md` секция 8 (обязательный no-JS режим для базовых сценариев).

### Whitelist endpoints, принимающих form-encoded

Перечисленные ниже endpoints принимают **оба** content-type'а — `application/json` (для AJAX-клиентов) **И** `application/x-www-form-urlencoded` (для HTML-форм без JS). Маппинг полей идентичен.

| Endpoint (canonical) | Также доступен через form-fallback |
| --- | --- |
| `POST /api/messages/send` | (тот же путь и метод) |
| `POST /api/mail-accounts` (create) | (тот же путь и метод) |
| `PATCH /api/mail-accounts/{id}` (edit) | `POST /api/mail-accounts/{id}` + form-поле `_method=PATCH` |
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
| `POST /api/webhooks/me` (create) | (тот же путь и метод) |
| `PATCH /api/webhooks/me` (edit) | `POST /api/webhooks/me` + form-поле `_method=PATCH` |
| `DELETE /api/webhooks/me` | `POST /api/webhooks/me/delete` + form-поле `_method=DELETE` |
| `POST /api/webhooks/me/rotate-secret` | (тот же путь и метод) |
| `POST /api/webhooks/me/test` | (тот же путь и метод) |

Любые остальные роуты не принимают `_method` — `POST` с этим полем на не-whitelist-роуте даёт `400 method_override_not_allowed`.

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
| `PATCH /api/admin/users/{id}` | `/admin` | "Пользователь обновлён" |
| `POST /api/webhooks/me` | `/my/integrations` | "Webhook создан" + one-shot flash `[secret_reveal]` с plaintext |
| `PATCH /api/webhooks/me` | `/my/integrations` | "Webhook обновлён" |
| `DELETE /api/webhooks/me` | `/my/integrations` | "Webhook удалён" |
| `POST /api/webhooks/me/rotate-secret` | `/my/integrations` | one-shot flash `[secret_reveal]` с новым plaintext |
| `POST /api/webhooks/me/test` | `/my/integrations` | "Тест выполнен: HTTP {code}, {duration_ms} мс" |

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

| Метод | Путь | Описание |
| --- | --- | --- |
| GET | `/` | Inbox (объединённый список писем со всех аккаунтов). Параметры query: `account_id` (фильтр), `tag_id` (фильтр по тегу; ownership проверяется), `cursor` (keyset, тот же формат что у `GET /api/messages`), `unread` (bool), `limit` (default 50, max 200). Page-based pagination не поддерживается — только cursor. |
| GET | `/messages/{id}` | Просмотр одного письма (plain text) + список вложений + теги. |
| GET | `/compose` | Форма нового письма (выбор from-аккаунта). |
| GET | `/compose?reply_to={message_id}` | Форма ответа (subject prefilled "Re: ...", body цитата). |
| GET | `/accounts` | Список mail-аккаунтов пользователя. |
| GET | `/accounts/new` | Форма добавления mail-аккаунта (с auto-suggest). |
| GET | `/accounts/{id}/edit` | Форма редактирования (без отображения пароля; новый — опционально). |
| GET | `/tags` | Список пользовательских тегов с кнопкой "+ Добавить тег". |
| GET | `/tags/new` | Форма создания тега (имя + цвет + список conditions + checkbox `apply_to_existing`). |
| GET | `/tags/{id}/edit` | Форма редактирования тега (имя + цвет + add/remove rules). |

Все требуют user-сессии; иначе 302 → `/login`.

---

## 3. User JSON API

Префикс `/api`. Все требуют user-сессии. Все state-changing — CSRF.

### Mail accounts

#### `GET /api/mail-accounts`
| Query | `group_id?` (только для super_admin — фильтр по группе; для остальных игнорируется), `user_id?` (фильтр по конкретному владельцу — для super_admin/лидера в рамках своей группы) |
| 200 | `[{id, user_id, owner: {id, username, display_name}, email, display_name, imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_ssl, smtp_starttls, smtp_username, is_active, last_synced_at, last_sync_error, consecutive_failures, created_at}]` |
| Visibility | Применяется `VisibilityScope` (см. ADR-0019 §7): super_admin видит все, лидер/участник — все аккаунты участников своей группы. Поле `owner` показывает, кто владелец аккаунта в группе (для UI «чей это ящик»). |

#### `POST /api/mail-accounts`
| Запрос | `{email, password, display_name?: str\|null, imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_ssl, smtp_starttls, smtp_username?, smtp_password?, target_user_id?: int}` |
| Валидация | email — RFC 5322; ports 1..65535; `smtp_ssl XOR smtp_starttls` (не оба true); `password` 1..256; `display_name` 1..100 (после trim'а пустая → null, см. ADR-0020). |
| Поведение | Перед сохранением — IMAP login + SMTP login (тест). При успехе — шифруем (AES-GCM), вставляем строку, возвращаем. <br>**`target_user_id` логика** (ADR-0019 §8): <br>— `super_admin`: `target_user_id` опционален (default = own id; если указан — backend проверяет существование). <br>— `group_leader`: `target_user_id` опционален (default = own id; если указан — backend проверяет, что target в той же группе, иначе `403 user_not_in_group_scope`). <br>— `group_member`: `target_user_id` запрещён или должен `== own id`; иначе `400 validation_error`. |
| Rate-limit | 10 / час per user. |
| 201 | `{id, user_id, owner: {...}, email, display_name, ...}` (без паролей). |
| 422 | `imap_login_failed` или `smtp_login_failed` + `details.detail` (текст ошибки от провайдера, без сензитива). |
| 409 | `conflict` (`field=email`) — этот email уже есть у этого `user_id` (т.е. `target_user_id`). UNIQUE по `(user_id, email)` — два разных user'а одной группы могут добавить одинаковый email; это намеренно, хотя на практике в группе делают один общий ящик. |
| 403 | `user_not_in_group_scope` (target не в группе лидера). |

##### Form-encoded request (no-JS)
```
POST /api/mail-accounts HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

email=user%40gmail.com&password=secret&imap_host=imap.gmail.com&imap_port=993&imap_ssl=on&smtp_host=smtp.gmail.com&smtp_port=465&smtp_ssl=on&csrf_token=...
```
Чекбоксы (`imap_ssl`, `smtp_ssl`, `smtp_starttls`): значение `on`/`true`/`1` → true; отсутствие поля → false. Опциональные поля (`smtp_username`, `smtp_password`) — допускают пустую строку, backend интерпретирует как «не задано».

##### Form-encoded response
- Success: `303 See Other`, `Location: /accounts`, flash="Email-аккаунт добавлен".
- Validation/external error: re-render `accounts/form.html` с error-context.

#### `POST /api/mail-accounts/test`
| Запрос | те же поля что POST mail-accounts |
| Назначение | Сухой прогон IMAP+SMTP без сохранения. |
| Rate-limit | 10 / час per user. |
| 200 | `{imap_ok: true, smtp_ok: true}`. |
| 422 | первый fail возвращает соответствующий код. |

#### `GET /api/mail-accounts/{id}`
| 200 | объект (как в list). |
| 404 | если не принадлежит пользователю. |

#### `PATCH /api/mail-accounts/{id}`
| Запрос | любое подмножество полей, включая `display_name?: str\|null` (см. ADR-0020). Если `password` присутствует — пере-шифровываем. Если меняются хосты/порты/auth — backend обязан повторить тест IMAP/SMTP перед сохранением. |
| Visibility | Можно редактировать любой аккаунт в области видимости текущего пользователя (super_admin — все; лидер/участник — все аккаунты своей группы). См. ADR-0019 §8. |
| 200 | объект (включая `display_name`). |
| 422 / 409 | как при POST. |

##### Form-encoded request (no-JS)
Через method override:
```
POST /api/mail-accounts/42 HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

_method=PATCH&imap_port=993&imap_ssl=on&csrf_token=...
```
Пустые поля (`password=`) интерпретируются как "не менять"; чтобы реально очистить опциональное поле — backend поддерживает (для edit-формы это не применяется; пароль НЕ может быть очищен).

##### Form-encoded response
- Success: `303 See Other`, `Location: /accounts`, flash="Изменения сохранены".
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
| Доступ | user-сессия (любая роль). |
| CSRF | yes. |
| 200 | `{tg_notifications_enabled: bool}` — итоговое значение. |
| 400 | `validation_error` если поле не bool. |

##### Form-encoded request (no-JS) — не требуется на MVP

UI toggle отложен (см. ADR-0022 Open question Q-002-1). API endpoint реализуется в этом спринте; form-fallback добавится в следующем sprint вместе с UI.

---

## 4. Admin API

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
| 200 | `{items: [{id, username, email, is_admin, password_reset_required, lockout_until, last_login_at, created_at, mail_accounts: [{id, email, is_active, last_synced_at, last_sync_error}]}], total, page, limit}` |

#### `POST /api/admin/users`
| Запрос | `{username: str (3..64, [A-Za-z0-9_.-]), email?: str, display_name?: str (1..100), role: 'group_leader'\|'group_member' (DEFAULT 'group_member'), group_id?: int}` |
| Поведение | Создаёт пользователя с `password_hash=NULL`, `password_reset_required=true`. Логика по ролям (см. ADR-0019 §5): <br>— `role='group_leader'`: `group_id` **должен быть пуст** (иначе `400 group_id_must_be_null_for_new_leader`); backend в одной транзакции (1) INSERT users без group_id, (2) INSERT groups с `name='Группа {display_name\|username}'` и `leader_user_id=user.id`, (3) UPDATE users.group_id. Audit: `create_user` + `group_create`. <br>— `role='group_member'`: `group_id` **обязателен** (existing group). Backend проверяет существование. Audit: `create_user`. <br>— `role` не передан — default `group_member`; `group_id` обязателен. <br>— Создание `super_admin` через API **запрещено**: super-admin создаётся только через seed (ADR-0019 §1). |
| Доступ | Только `super_admin`. Лидеры/участники → 403. |
| 201 | `{id, username, email, display_name, role, group_id, group: {id, name}\|null}` |
| 409 | `conflict` (`field=username`). |
| 400 | `validation_error` / `group_id_must_be_null_for_new_leader` / `group_not_found`. |

##### Form-encoded request (no-JS)
```
POST /api/admin/users HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

username=bob&email=bob%40example.com&display_name=Bob+Smith&role=group_member&group_id=3&csrf_token=...
```
Пустое `email=` или `display_name=` интерпретируются как `null`. `group_id=` пустое → `null` (валидно только для `role=group_leader`).

##### Form-encoded response
- Success: `303 See Other`, `Location: /admin`, flash="Пользователь создан".
- Validation/conflict error: re-render `admin/users.html` (с открытой формой создания) с error-context (значения сохранены).

#### `PATCH /api/admin/users/{id}`
| Запрос | `{display_name?: str\|null, role?: 'group_leader'\|'group_member', group_id?: int\|null}` (любое подмножество). |
| Поведение | Изменение полей пользователя через super-admin. <br>— `display_name`: trim → `null` если пусто. <br>— Смена `role` от/к `group_leader`: complex flow. (а) `group_member → group_leader`: backend требует, чтобы текущая группа user'а **не имела другого лидера**; иначе `400 conflict` (нужно сначала переразмерить старого лидера). Чтобы создать **новую группу** для лидера — клиент отдельно вызывает `POST /api/admin/groups` или передаёт `role='group_leader' + group_id=null` (тогда backend auto-create'ит группу как при POST users). (б) `group_leader → group_member`: тоже complex — у группы остаётся без лидера; backend требует, чтобы клиент **сначала** удалил/назначил нового лидера через переход на `PATCH /api/admin/users` с другого user'а (или удалил группу через DELETE). На текущем scope: `400 cannot_demote_lone_leader` если лидер единственный в группе. <br>— Смена `group_id` без смены role: переводит user'а в другую группу (только для `group_member`). <br>— Изменение `role` к `super_admin` или с `super_admin` — **запрещено** (`400 forbidden`); super-admin один и определяется seed'ом. <br>— Все сессии target user'а **revoke'аются** (см. ADR-0019 §10). |
| Доступ | Только `super_admin`. |
| 200 | `{id, username, ..., role, group_id, group: {id, name}\|null}`. |
| 400 | `validation_error` / `group_id_must_be_null_for_new_leader` / `cannot_demote_lone_leader` / `forbidden`. |
| 404 | `not_found` если user не существует. |
| Audit | `user_role_change` если role изменился; `user_group_change` если только group_id; обе — если оба. `group_create` дополнительно если auto-create группы для нового лидера. |

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

#### `POST /api/admin/users/{id}/reset`
| Поведение | UPDATE password_hash=NULL, password_reset_required=true, lockout_until=NULL, failed_login_attempts=0; revoke all sessions; audit log: `reset_password`. |
| 200 | `{ok: true}` |
| 400 | если `id` совпадает с супер-админом — отказ (`code=cannot_reset_admin`). |

##### Form-encoded request (no-JS)
```
POST /api/admin/users/42/reset HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

csrf_token=...
```

##### Form-encoded response
- Success: `303 See Other`, `Location: /admin`, flash="Пароль сброшен".
- Error (`cannot_reset_admin`): re-render `admin/users.html` с error-context.

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

### `POST /api/telegram/auth` (ADR-0022 §1)

Persistent SSO endpoint: принимает Telegram WebApp `init_data`, валидирует HMAC, ищет линковку и либо выпускает session-cookie, либо ставит pending-cookie для последующей линковки после ручного login.

| | |
| --- | --- |
| Доступ | публичный |
| CSRF | exempt (нет session при first call; защита — HMAC + TTL) |
| Запрос | `application/json`, тело: `{"init_data": "<raw initData string from Telegram.WebApp.initData>"}`. `init_data` — строка 1..4096 chars. |
| Валидация init_data | (1) Parse как URL-encoded; (2) извлечь `hash`; (3) `data_check_string = "\n".join(sorted(k=v for non-hash keys))`; (4) `secret_key = HMAC_SHA256("WebAppData", TELEGRAM_BOT_TOKEN)`; (5) constant-time compare `HMAC_SHA256(secret_key, data_check_string)` vs `hash`; (6) `auth_date` не старше 5 минут (env `TG_AUTH_INIT_DATA_TTL_SEC=300`). Спецификация: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app |
| Rate-limit | 30/min per IP **+** 10/min per `telegram_user_id` (применяется ПОСЛЕ HMAC валидации). |
| 200 (linked=true) | `{"linked": true, "redirect": "/"}` + Set-Cookie `mas_session` (HttpOnly, Secure, SameSite=Lax, sliding 12h) + Set-Cookie `mas_csrf` (не HttpOnly). |
| 200 (linked=false) | `{"linked": false, "redirect": "/login"}` + Set-Cookie `mas_tg_pending` (HttpOnly, Secure, SameSite=Lax, **15 минут**) — opaque token указывающий на Redis ключ `tg_pending:{token}` = `{telegram_user_id}`. После успешного `POST /login/password` или `POST /set-password` backend читает cookie, делает upsert в `telegram_links` и удаляет Redis ключ. |
| 401 | `invalid_init_data` (HMAC mismatch / парсинг провалился). |
| 401 | `init_data_expired` (`auth_date` старше TTL). |
| 429 | `rate_limited` + `Retry-After`. |
| Audit | при успешной перепривязке (upsert обновил `user_id`) — `telegram_link_created`/`telegram_link_rebound` с `details={telegram_user_id, replaced: true|false}`; при достижении лимита `TG_MAX_LINKS_PER_USER` — `telegram_link_limit_reached` (ADR-0024). **Action `telegram_link_collision` — deprecated (ADR-0024): больше не пишется, инвариант «один user — один TG» снят.** |
| Side effects | См. ADR-0022 §1.3 sequence diagram. ADR-0024: `link_pending` теперь применяет мягкий лимит `TG_MAX_LINKS_PER_USER` (default 10) вместо collision-проверки. |

#### Связанные изменения flow

| Endpoint | Изменение от base-логики |
| --- | --- |
| `POST /login/password` | После успешного verify password, если в request есть cookie `mas_tg_pending` — backend читает Redis `tg_pending:{token}`, применяет soft-limit `COUNT(active) < TG_MAX_LINKS_PER_USER` (ADR-0024 §3), делает `INSERT INTO telegram_links (telegram_user_id, user_id) … ON CONFLICT (telegram_user_id) DO UPDATE SET user_id=EXCLUDED.user_id, created_at=now(), dead_at=NULL` (атомарно перепривязывает); удаляет Redis-ключ; clear cookie `mas_tg_pending`. Audit: `telegram_link_created` (новая/свой TG) или `telegram_link_rebound` (TG перепривязан с другого user'а); при достижении потолка — `telegram_link_limit_reached`, привязка не создаётся (ADR-0024 §3). |
| `POST /set-password` | То же поведение, что и `POST /login/password` — линковка создаётся после успешной установки пароля. |
| `POST /logout` | Дополнительно (ADR-0024): `DELETE FROM telegram_links WHERE user_id=:uid` — удаляет **ВСЕ** привязки user'а в той же транзакции с revoke session (явный выход из аккаунта системы прекращает persistent SSO во всех TG; Q-MTG-1). Audit: `telegram_link_revoked` с `details={telegram_user_ids: [...]}`. |
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
| Логика | HMAC-валидация initData (как `/api/telegram/auth`); привязать `telegram_user_id` к `session.user_id` (НЕ через pending-flow). Применяет лимит `TG_MAX_LINKS_PER_USER`. |
| 200 | `{"linked": true, "telegram_user_id": int}`. |
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

| Query | `embed: str | None = None` — если `embed='tg'` (рендер внутри Telegram WebApp по inline-keyboard button), backend выставляет в Jinja-контекст `embed_tg=True`. Шаблон `message_view.html` при `embed_tg=True` скрывает секцию `<section class="attachments">`. Остальной функционал (mark-read, bottom-nav, logout) остаётся. |

### Push-уведомления о новых письмах (ADR-0022 §2)

Доставка происходит **асинхронно** через worker-job (см. `05-modules.md` §14.1 + `worker → tg_notify_dispatch`). Нет публичного HTTP-эндпоинта для триггера/просмотра очереди — это внутренний механизм. Получатель видит уведомление в Telegram-боте как Message с inline-keyboard кнопкой «Посмотреть сообщение» (WebApp-button → открывает `/messages/{id}?embed=tg` внутри Telegram WebView с persistent SSO).

**ADR-0024 (multi-TG):** если у получателя несколько живых TG-привязок, уведомление доставляется **в каждый** живой чат. Recipient-SQL даёт по строке на каждый `telegram_user_id`; идемпотентность — per `(message_id, telegram_user_id)`. Мёртвый чат (`dead_at`) пропускается, остальные получают.

**Объём уведомлений (round-31, env `TG_NOTIFY_ALL_MESSAGES`, default `true`):**
- `true` — уведомление по **каждому** новому письму (тег не обязателен);
- `false` — только письма с ≥1 тегом (историческое поведение).

Шаблон текста (HTML mode) — строка тегов **опциональна**:
```
Вы получили письмо на почту <b>{acc.display_name|acc.email}</b>
Тег «<b>X</b>»  /  Теги «<b>X</b>», «<b>Y</b>»     ← ТОЛЬКО если у письма есть теги (иначе строка отсутствует)
Отправитель <b>{from_name|from_addr}</b>

[ Посмотреть сообщение ]   ← inline_keyboard.web_app.url = {TELEGRAM_WEBAPP_URL}/messages/{message_id}?embed=tg
```
С тегами — 3 строки, без тегов — 2 строки. Доставка ограничена per-chat троттлингом `TG_SEND_PER_CHAT_PER_MINUTE` (default 20/мин; ADR-0022 §2.9).

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

Источник истины — [ADR-0025](./adr/ADR-0025-outlook-oauth2.md). Подключение личных Outlook-ящиков (`outlook.com`/`hotmail.com`/`live.com`) через OAuth2 + XOAUTH2 рядом с обычными password-аккаунтами. Consent через наш сайт + OctoBrowser. Все endpoint'ы доступны только когда `OUTLOOK_OAUTH_ENABLED` (заданы `OUTLOOK_CLIENT_ID` + `OUTLOOK_CLIENT_SECRET`); иначе `404 not_found` (route скрыт, симметрично telegram-bot-disabled).

### `GET /api/oauth/outlook/authorize`
| | |
| --- | --- |
| Auth | session cookie. |
| Логика | Генерит `state` (32B urlsafe) + PKCE `code_verifier`/`code_challenge` (S256), сохраняет в Redis `oauth_state:{state}` = `{user_id, code_verifier}` TTL `OUTLOOK_OAUTH_STATE_TTL_SECONDS` (default 600), привязка к `session.user_id`. Строит Microsoft authorize URL. |
| 200 | `{"authorize_url": "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?..."}` — фронт показывает ссылку «открыть в OctoBrowser» (НЕ auto-redirect — пользователь открывает в нужном профиле). |
| 404 | `not_found` если `OUTLOOK_OAUTH_ENABLED=false`. |

### `GET /api/oauth/outlook/callback`
| | |
| --- | --- |
| Это | зарегистрированный в Azure `redirect_uri` = `{APP_BASE_URL}/api/oauth/outlook/callback`. |
| Query | `code`, `state` (успех) либо `error`, `error_description` (отказ). |
| Логика | GET+DEL `oauth_state:{state}` (одноразовый); нет/истёк → `400 oauth_state_invalid`. Обмен `code`→токены на token endpoint (`grant_type=authorization_code` + `code_verifier`). Email из `id_token`/Graph `GET /me`. Create/update `mail_account` (`auth_type='oauth_outlook'`, Outlook host/port, зашифрованные токены). Q-OAUTH-1: callback может прийти без cookie сессии (другой OctoBrowser-профиль) → доверяем Redis-state, привязанному к `user_id`. |
| 302 | Редирект на `/accounts` с flash «Outlook подключён». |
| 400 | `oauth_state_invalid` / `oauth_exchange_failed` (token endpoint вернул ошибку) / `oauth_consent_denied` (пришёл `error`). |
| Audit | `oauth_account_linked` с `details={mail_account_id, email, scopes}`. |
| CSRF | exempt (state выполняет роль anti-CSRF; cookie может отсутствовать). |

### Связанные изменения

| Endpoint | Изменение |
| --- | --- |
| `GET /api/mail-accounts`, `GET /api/mail-accounts/{id}` | DTO дополняется `auth_type` и (для oauth) `oauth_needs_consent`. UI показывает бейдж «Outlook OAuth» и кнопку «переподключить» при `oauth_needs_consent=true`. |
| `PATCH /api/mail-accounts/{id}` | Для `auth_type='oauth_outlook'` запрещено менять `password`/`imap_*`/`smtp_*` креды (они фиксированы); `400 validation_error` `field=auth_type` при попытке. Допускается только `display_name`. |
| `POST /api/mail-accounts/test` | Для oauth-аккаунтов test использует XOAUTH2 (refresh→access→коннект); password-тест не применяется. |
| `POST /api/mail-accounts` (ручной IMAP/SMTP) | Без изменений — создаёт `auth_type='password'`. |

> **Q-OAUTH-3 (БЛОКЕР e2e):** требует реальный Azure App (`client_id`/`secret`) и проверки, что personal accounts выдают IMAP/SMTP XOAUTH2-доступ. Код и unit/integration-тесты с моками token endpoint можно реализовать без этого; e2e — после получения Azure App от пользователя.

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
| POST | `/api/admin/users/{id}/reset` | super_admin | yes | 50/h | yes | reset password |
| DELETE | `/api/admin/users/{id}` | super_admin | yes | 50/h | yes | delete user (canonical) |
| POST | `/api/admin/users/{id}/delete` | super_admin | yes | 50/h | yes | form-fallback delete (`_method=DELETE`) |
| GET | `/api/admin/groups` | super_admin | — | — | — | list groups (ADR-0019) |
| POST | `/api/admin/groups` | super_admin | yes | 50/h | yes | create group |
| GET | `/api/admin/groups/{id}` | super_admin | — | — | — | get group with members |
| PATCH | `/api/admin/groups/{id}` | super_admin | yes | 50/h | yes | rename; через override |
| POST | `/api/admin/groups/{id}` | super_admin | yes | 50/h | yes | (form-fallback к PATCH) |
| DELETE | `/api/admin/groups/{id}` | super_admin | yes | 50/h | yes | delete (canonical) |
| POST | `/api/admin/groups/{id}/delete` | super_admin | yes | 50/h | yes | form-fallback delete (`_method=DELETE`) |
| GET | `/api/admin/audit` | super_admin | — | — | — | audit log |
| POST | `/api/telegram/webhook/{secret}` | secret in URL + header | exempt | 60/min per IP | — | Telegram bot webhook (launcher only — `/start` отдаёт WebApp button); см. ADR-0018 |
| POST | `/api/telegram/auth` | initData HMAC | exempt | 30/min per IP + 10/min per tg_user_id | — | Persistent SSO: validate Telegram WebApp initData, выпустить session либо pending-cookie; см. ADR-0022 |
| GET | `/api/telegram/links` | user | — | — | — | список TG-привязок текущего user'а (ADR-0024) |
| POST | `/api/telegram/links` | user | yes | 10/h | — | добавить TG-привязку при активной сессии (ADR-0024) |
| DELETE | `/api/telegram/links/{tg_user_id}` | user | yes | 10/h | yes | отвязать конкретный TG (ADR-0024) |
| POST | `/api/telegram/links/{tg_user_id}/delete` | user | yes | 10/h | yes | form-fallback delete (`_method=DELETE`) |
| PATCH | `/api/me/settings` | user | yes | — | — | user preferences (tg_notifications_enabled); см. ADR-0022 |
| GET | `/api/oauth/outlook/authorize` | user | — | 10/h | — | сгенерить Microsoft authorize URL + state (ADR-0025) |
| GET | `/api/oauth/outlook/callback` | state in Redis | exempt | 30/min per IP | — | OAuth callback: code→токены, create mail_account (ADR-0025) |
| GET | `/my/integrations` | group_leader \| super_admin | — | — | — | webhook config page (ADR-0023) |
| GET | `/api/webhooks/me` | group_leader \| super_admin | — | — | — | get webhook config (no secret) |
| POST | `/api/webhooks/me` | group_leader \| super_admin | yes | 10/h per group | yes | create webhook + one-shot secret reveal |
| PATCH | `/api/webhooks/me` | group_leader \| super_admin | yes | 30/h per webhook | yes | update url / is_active; через override |
| DELETE | `/api/webhooks/me` | group_leader \| super_admin | yes | 10/h per webhook | yes | delete (canonical) |
| POST | `/api/webhooks/me/delete` | group_leader \| super_admin | yes | 10/h per webhook | yes | form-fallback delete (`_method=DELETE`) |
| POST | `/api/webhooks/me/rotate-secret` | group_leader \| super_admin | yes | 5/h per webhook | yes | rotate secret + reveal one-shot |
| POST | `/api/webhooks/me/test` | group_leader \| super_admin | yes | 10/h per webhook | yes | synchronous test POST to receiver |
| GET | `/healthz` | none | — | — | — | liveness |
| GET | `/readyz` | none | — | — | — | readiness |
