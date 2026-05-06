# ADR-0004: Сессии — server-side через Redis

- **Статус:** accepted
- **Дата:** 2026-05-05

## Context

Нужно аутентифицировать пользователей в веб-UI. Варианты:

| Подход | Плюсы | Минусы |
| --- | --- | --- |
| **JWT в cookie** | Stateless, не требует backend storage | Сложнее invalidation (logout, force reset password, удаление пользователя) — нужен blacklist; CSRF-защита всё равно нужна; ротация ключей сложнее |
| **Server-side session, opaque ID в HttpOnly cookie** | Простая инвалидация (delete key); естественный fit для UI на серверных шаблонах; CSRF-protect стандартный pattern | Требует Redis (он у нас уже есть) |
| **JWT в localStorage** | — | XSS = потеря токена; запрещено для UI с серверными формами |

Также:
- Кейс "сброс пароля админом" требует немедленного выкидывания пользователя из всех сессий — это естественно для server-side, и неудобно для JWT.
- Кейс "удаление пользователя" — то же.
- Audit log должен содержать привязку к user_id; opaque session id это позволяет напрямую.

## Decision

- **Server-side сессии** в Redis.
- Cookie: имя `mas_session` (Mail Aggregator Session), значение — 32 случайных байта в base64-url.
- Cookie атрибуты: `HttpOnly`, `Secure` (в проде; в dev отключаем флагом), `SameSite=Lax`, `Path=/`.
- TTL сессии: **скользящий**, 12 часов (продлевается на каждый запрос). Абсолютный TTL — 7 дней (даже если пользователь активен — после 7 дней нужен релогин).
- Хранение в Redis: ключ `session:{token}` -> JSON `{user_id, role, csrf_token, created_at, last_seen_at, ip, user_agent_hash}`. TTL = 12 часов скользящий.
- **CSRF-токен** генерируется при создании сессии, хранится в session-store, возвращается в cookie `mas_csrf` (НЕ HttpOnly) или подставляется в шаблоны как `{{ csrf_token }}`. Для всех POST/PUT/DELETE — обязательная проверка double-submit (см. ADR-0010).
- Logout: удаление ключа из Redis + удаление cookie на клиенте.
- Force-invalidate (сброс пароля админом, удаление пользователя): scan `session:*` и удаление всех принадлежащих user_id. Реализация: вспомогательный set `user_sessions:{user_id}` -> {session_token, ...} обновляется атомарно при создании/удалении сессии.

## Consequences

**Плюсы:**
- Одной командой выкидываем пользователя при сбросе пароля или удалении.
- Не нужно ротировать JWT-ключи.
- Прозрачный аудит: можно показать активные сессии по пользователю.

**Минусы:**
- Зависимость от Redis для каждого аутентифицированного запроса. Mitigation: Redis лежит на той же сети, latency <1ms; redis является shared-сервисом проекта.

## Alternatives considered

- **JWT с RS256 + JWKS**: оверкилл для одного сервиса; хуже UX при force-logout.
- **Подписанные cookie без storage** (itsdangerous): не позволяет force-invalidate, не подходит.
