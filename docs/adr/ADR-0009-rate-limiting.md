# ADR-0009: Rate-limit на login и password-set

- **Статус:** accepted
- **Дата:** 2026-05-05

## Context

Чувствительные эндпоинты:
- `POST /login` — username + password.
- `POST /set-password` — установка пароля при первом входе или после сброса.
- `POST /api/mail-accounts/test` — IMAP/SMTP тест-логин (потенциально может быть использован для перебора чужих почтовых паролей со сторонних серверов, но это бот-уязвимость провайдера, а не наша; всё равно ограничиваем).

## Decision

- Реализация: **slowapi** (`slowapi==0.1.9+`) поверх Redis (как backend).
- Лимиты:

| Эндпоинт | Лимит | Окно | Ключ |
| --- | --- | --- | --- |
| `POST /login` | 5 попыток | 15 минут | по `username` (lower-case) **И** по IP (whichever hits first) |
| `POST /set-password` | 5 попыток | 15 минут | по setup-session-token из cookie `mas_setup`, **fallback на IP** только если cookie отсутствует/невалиден |
| `POST /api/mail-accounts/test` | 10 попыток | 1 час | по `user_id` |
| `POST /api/mail-accounts` (add) | 10 в час | 1 час | по `user_id` |

- Превышение -> HTTP 429 + `Retry-After`.
- Lockout: при 5 фейлах login для конкретного `username` — фиксируется в `users.lockout_until = now() + 15min`. При login сначала проверяется lockout, потом argon2-verify (анти-timing — argon2 всё равно дорогой, поэтому таймингом не выдадим существование аккаунта).
- Все 429 и срабатывания lockout пишутся в audit log (для super-admin: только админские эндпоинты; для обычных пользователей — в общий приложение-лог).
- При успешном login — счётчик сбрасывается, `lockout_until=NULL`.

## Consequences

**Плюсы:**
- Защита от brute-force.
- Лимиты не мешают добросовестному пользователю (5 попыток за 15 мин — щедро).

**Минусы:**
- Атакующий с большого пула IP может всё равно перебирать. Mitigation на уровне приложения — невозможна без CAPTCHA; для текущего scope это приемлемо (admin создаёт фиксированный набор пользователей).

## Alternatives considered

- **Свой in-memory счётчик**: не работает между несколькими инстансами API.
- **fastapi-limiter** (asyncio + Redis): эквивалентен slowapi; выбран slowapi из-за более удобной конфигурации лимитов через декораторы.
- **CAPTCHA** (hCaptcha/Turnstile): добавим в backlog; сейчас scope не требует.

## Revisions

- **2026-05-05 (rev. 2):** пути приведены к фактическим — `/login` и `/set-password` (без префикса `/auth/`). Ключ rate-limit для `/set-password` уточнён: setup-session-token из cookie `mas_setup`, fallback на IP только если cookie отсутствует.
