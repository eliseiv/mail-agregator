# ADR-0016 — Two-step login (username then password)

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-05-06 |
| Заменяет / отменён | — |

## Context

Изначальный `POST /login` принимал `username` + `password` в одной форме
(см. предыдущая редакция `04-api-contracts.md`). Этот flow не поддерживал
сценарий "новый пользователь, у которого пароль ещё не установлен":

- `users.password_hash` для свежесозданного user — `NULL`,
  `password_reset_required = true`.
- Pydantic-схема `LoginRequest` требовала `password ∈ [1..128]` — пустой
  password отвергался валидатором с `validation_error`, до того как
  service-слой успевал перенаправить на set-password.

Кроме того, single-form flow затрудняет внедрение SSO/OIDC в будущем
(где username/email сначала уходит на SSO-провайдера, и только потом
возвращается password) — отсутствует естественная точка ветвления.

## Decision

Разбиваем login на два шага по образцу Google/Microsoft:

1. **Step-1** (`POST /login`) — принимает только `username`.
   Backend смотрит в БД и возвращает один из трёх редиректов:
   - `set_password_required` -> set cookie `mas_setup`, redirect 303 на
     `/set-password`;
   - `ready_for_password` (user найден, пароль есть) -> set cookie
     `mas_login`, redirect 303 на `/login/password`;
   - `not_found` -> ведёт себя как `ready_for_password` (set cookie,
     redirect на `/login/password`); на step-2 backend вернёт generic
     `invalid_credentials`. Это устраняет user-enumeration через
     timing/redirect.
2. **Step-2** (`POST /login/password`) — принимает `password`. Username
   читается из cookie `mas_login` (HttpOnly, 15 мин TTL). Логика
   идентична старому `POST /login` с обоими полями: rate-limit
   `username|ip`, argon2 verify, lockout, session creation.

Дополнительные изменения:

- `POST /login/password` exempted from CSRF — у user ещё нет session.
  Защита: rate-limit + lockout + короткий TTL cookie `mas_login`.
- Cookie `mas_login`: HttpOnly, Secure (prod), SameSite=Lax, Max-Age=900.
  Содержимое — plain lower-case username (не secret).
- Старый combined `LoginRequest` (username+password) удалён.

## Consequences

### Positive

- Новые пользователи без пароля корректно попадают на set-password без
  trick'а "введи любой пароль".
- Устранён enumeration vector: оба ветки ("user exists with password" и
  "user not found") дают идентичный response, идентичный redirect и
  cookie, идентичные timing.
- Естественная точка ветвления для будущего SSO/OIDC.
- UX совпадает с Google/Microsoft — пользователи знакомы с паттерном.

### Negative

- Существующие интеграционные клиенты (curl, тесты), которые делают
  `curl -X POST -d "username=...&password=..." /login` в один hop,
  ломаются. Это intentional — clients надо обновить на два POST'а.
- Дополнительный round-trip при login (step-1 + step-2). Приемлемо: ~10 ms
  на step-1 запрос, не влияет на UX.
- Новый cookie `mas_login` в наборе. Минимальное расширение surface area.

## Alternatives considered

1. **Оставить single-form, но сделать password опциональным в Pydantic.**
   Отклонено: усложняет валидацию (нужно условно требовать password по
   результату DB lookup'а); не решает enumeration vector; не дает SSO-hook.

2. **Использовать query string `/login?username=...&password=...`.**
   Отклонено: passwords в query попадают в access logs / referers / proxy
   logs; стандартное "не делать так".

3. **Использовать одну страницу с JS-маневром (показать password после
   валидации username через AJAX).** Отклонено: ломает no-JS fallback
   (см. ADR-0015); требует двух endpoint'ов всё равно (один JSON для
   AJAX-шага, другой для submit).

4. **Хранить username в session storage (Redis) вместо cookie.**
   Отклонено: усложняет (создание server-side state до auth); cookie
   обеспечивает то же самое с TTL и без storage cost.

## Cross-references

- `docs/04-api-contracts.md` — секция Public Auth (обновлена).
- `docs/05-modules.md` — модуль 7 (auth).
- `docs/06-security.md` — секция 1.1 (Login STRIDE: enumeration через
  timing).
- ADR-0009 — rate-limit (правила сохраняются для step-2).
- ADR-0010 — CSRF (login exempt).
