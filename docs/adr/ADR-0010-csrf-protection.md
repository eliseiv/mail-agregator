# ADR-0010: CSRF-защита для всех cookie-аутентифицированных POST

- **Статус:** accepted
- **Дата:** 2026-05-05

## Context

Используем cookie-сессии (ADR-0004). По умолчанию браузер прикрепляет cookie ко всем запросам с домена сервиса -> уязвимы к CSRF.

`SameSite=Lax` снижает риск, но не закрывает его полностью (например, top-level navigation form-POST с другого сайта).

## Decision

- **Double-submit cookie** pattern + хранение токена в server-side сессии (для усиления).
- При создании сессии:
  - Генерируется `csrf_token` = `secrets.token_urlsafe(32)`.
  - Сохраняется в `session:{token}.csrf_token` (Redis).
  - Кладётся в cookie `mas_csrf` (НЕ HttpOnly, `SameSite=Lax`, `Secure` в проде).
- При рендере шаблона: `{{ csrf_token }}` доступен; вставляется как `<input type="hidden" name="csrf_token" value="...">`.
- Для AJAX (vanilla `fetch`): JS читает `document.cookie -> mas_csrf` и шлёт в заголовке `X-CSRF-Token`.
- На сервере middleware (см. модуль `auth` в `05-modules.md`) для всех методов `POST/PUT/PATCH/DELETE`:
  1. Получить session (обычная `session:{token}` ИЛИ `setup_session:{token}` для `/set-password`).
  2. Получить токен из заголовка `X-CSRF-Token` или формы `csrf_token`.
  3. Сравнить с `session.csrf_token` через `secrets.compare_digest`.
  4. Mismatch -> 403.
- Исключения CSRF middleware (полный список): `POST /login` (сессии ещё нет; защищается rate-limit), `GET *`, `HEAD *`, `OPTIONS *`. Все остальные state-changing запросы — обязательная CSRF-проверка, **включая `POST /set-password`** (он идёт под cookie `mas_setup`, токен берётся из соответствующей setup-session в Redis).

### Set-password flow

1. После создания пользователя или сброса — пользователь вводит логин на `/login`.
2. Если у пользователя `password_reset_required=true` — backend создаёт **setup-session** в Redis (`setup_session:{token}` JSON `{user_id, scope:"set_password", csrf_token}`, TTL=15 мин), кладёт cookie `mas_setup` (HttpOnly, Secure в prod, SameSite=Lax) и редиректит на `/set-password`. Любой пароль на этом шаге игнорируется.
3. `GET /set-password` рендерит форму, передавая `csrf_token` из setup-session как hidden-input.
4. `POST /set-password` идёт под cookie `mas_setup`. Полная CSRF-проверка: `csrf_token` из формы сравнивается через `secrets.compare_digest` с `setup_session.csrf_token`. Mismatch -> 403.
5. После установки пароля setup-session уничтожается (`DEL setup_session:{token}`), cookie `mas_setup` удаляется, создаётся обычная сессия + cookie `mas_session`/`mas_csrf`.

## Consequences

**Плюсы:**
- Стандартный паттерн, низкий риск ошибки.
- Заголовок `X-CSRF-Token` плюс double-submit покрывают и формы, и AJAX.

**Минусы:**
- Каждая форма должна включать токен. Mitigation: в Jinja2 — base-шаблон с macro `{{ csrf_input() }}`.

## Alternatives considered

- **Только SameSite=Strict**: ломает UX (пользователь приходит из ссылки в письме — нет cookie). Lax + double-submit — золотая середина.
- **Custom header только** (CORS-style): требует отказа от классических HTML form-POST; неудобно для серверного рендера.

## Revisions

- **2026-05-05 (rev. 2):** убраны устаревшие пути `/auth/login`, `/auth/set-password` — реальные пути `/login` и `/set-password` (см. `04-api-contracts.md`). Явно зафиксировано, что `POST /set-password` имеет полную CSRF-проверку через токен из setup-session (cookie `mas_setup`); упоминание «одноразовой ссылки» удалено как нерелевантное.
