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
| GET | `/` | Inbox (объединённый список писем со всех аккаунтов). Параметры query: `account_id` (фильтр), `cursor` (keyset, тот же формат что у `GET /api/messages`), `unread` (bool), `limit` (default 50, max 200). Page-based pagination не поддерживается — только cursor. |
| GET | `/messages/{id}` | Просмотр одного письма (plain text) + список вложений. |
| GET | `/compose` | Форма нового письма (выбор from-аккаунта). |
| GET | `/compose?reply_to={message_id}` | Форма ответа (subject prefilled "Re: ...", body цитата). |
| GET | `/accounts` | Список mail-аккаунтов пользователя. |
| GET | `/accounts/new` | Форма добавления mail-аккаунта (с auto-suggest). |
| GET | `/accounts/{id}/edit` | Форма редактирования (без отображения пароля; новый — опционально). |

Все требуют user-сессии; иначе 302 → `/login`.

---

## 3. User JSON API

Префикс `/api`. Все требуют user-сессии. Все state-changing — CSRF.

### Mail accounts

#### `GET /api/mail-accounts`
| 200 | `[{id, email, imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_ssl, smtp_starttls, smtp_username, is_active, last_synced_at, last_sync_error, consecutive_failures, created_at}]` |

#### `POST /api/mail-accounts`
| Запрос | `{email, password, imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_ssl, smtp_starttls, smtp_username?, smtp_password?}` |
| Валидация | email — RFC 5322; ports 1..65535; `smtp_ssl XOR smtp_starttls` (не оба true); `password` 1..256. |
| Поведение | Перед сохранением — IMAP login + SMTP login (тест). При успехе — шифруем (AES-GCM), вставляем строку, возвращаем. |
| Rate-limit | 10 / час per user. |
| 201 | `{id, email, ...}` (без паролей). |
| 422 | `imap_login_failed` или `smtp_login_failed` + `details.detail` (текст ошибки от провайдера, без сензитива). |
| 409 | `conflict` (`field=email`) — этот email уже есть у пользователя. |

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
| Запрос | любое подмножество полей. Если `password` присутствует — пере-шифровываем. Если меняются хосты/порты/auth — backend обязан повторить тест IMAP/SMTP перед сохранением. |
| 200 | объект. |
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
| Query | `account_id?` (filter), `unread?` (bool), `cursor?` (id для keyset pagination), `limit?` (default 50, max 200) |
| Pagination | Keyset по `(internal_date DESC, id DESC)`. Курсор — base64(`{internal_date_iso}:{id}`). |
| 200 | `{items: [{id, mail_account_id, mail_account_email, from_addr, from_name, subject, internal_date, is_read, has_attachments}], next_cursor: str|null}` |

#### `GET /api/messages/{id}`
| 200 | `{id, mail_account_id, mail_account_email, from_addr, from_name, to_addrs, cc_addrs, subject, internal_date, body_text, body_truncated, body_present, in_reply_to, is_read, attachments: [{id, filename, content_type, size_bytes, skipped_too_large}]}` |
| 404 | если письмо не принадлежит пользователю (через mail_account.user_id). |

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

### Self

#### `GET /api/me`
| 200 | `{id, username, is_admin, last_login_at, mail_accounts_count}` |

---

## 4. Admin API

Префикс `/admin` (HTML) и `/api/admin` (JSON). Требует session.is_admin=true.

### Pages

| Метод | Путь | Описание |
| --- | --- | --- |
| GET | `/admin` | Дашборд: список пользователей, у каждого раскрывающийся список mail-аккаунтов. |
| GET | `/admin/audit` | Audit log (paginated, default 50/page). |

### JSON

#### `GET /api/admin/users`
| Query | `q?` (search by username substring), `page?`, `limit?` (default 50, max 200) |
| 200 | `{items: [{id, username, email, is_admin, password_reset_required, lockout_until, last_login_at, created_at, mail_accounts: [{id, email, is_active, last_synced_at, last_sync_error}]}], total, page, limit}` |

#### `POST /api/admin/users`
| Запрос | `{username: str (3..64, [A-Za-z0-9_.-]), email?: str}` |
| Поведение | Создаёт пользователя с `is_admin=false`, `password_hash=NULL`, `password_reset_required=true`. Audit log: `create_user`. |
| 201 | `{id, username, email}` |
| 409 | `conflict` (`field=username`). |

##### Form-encoded request (no-JS)
```
POST /api/admin/users HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Cookie: mas_session=...; mas_csrf=...

username=bob&email=bob%40example.com&csrf_token=...
```
Пустое `email=` интерпретируется как `null`.

##### Form-encoded response
- Success: `303 See Other`, `Location: /admin`, flash="Пользователь создан".
- Validation/conflict error: re-render `admin/users.html` (с открытой формой создания) с error-context.

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
| GET | `/admin` | admin | — | — | — | admin dashboard |
| GET | `/admin/audit` | admin | — | — | — | audit page |
| GET | `/api/admin/users` | admin | — | — | — | list users |
| POST | `/api/admin/users` | admin | yes | 50/h | yes | create user |
| POST | `/api/admin/users/{id}/reset` | admin | yes | 50/h | yes | reset password |
| DELETE | `/api/admin/users/{id}` | admin | yes | 50/h | yes | delete user (canonical) |
| POST | `/api/admin/users/{id}/delete` | admin | yes | 50/h | yes | form-fallback delete (`_method=DELETE`) |
| GET | `/api/admin/audit` | admin | — | — | — | audit log |
| GET | `/healthz` | none | — | — | — | liveness |
| GET | `/readyz` | none | — | — | — | readiness |
