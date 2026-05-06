# ADR-0006: Хеширование паролей пользователей — argon2id

- **Статус:** accepted
- **Дата:** 2026-05-05

## Context

Нужно хешировать пароли супер-админа и пользователей. Кандидаты:

| Алгоритм | Оценка |
| --- | --- |
| bcrypt | Зрелый стандарт, 72-байтовое ограничение длины, нет защиты от GPU/ASIC |
| scrypt | Memory-hard, но реже встречается в библиотеках |
| **argon2id** | Победитель PHC (2015), memory-hard, защита от side-channel и GPU; рекомендуется OWASP |

## Decision

- Алгоритм: **argon2id**.
- Библиотека: `argon2-cffi` >= 23.1.
- Параметры по умолчанию из `argon2.PasswordHasher()` — соответствуют OWASP 2024:
  - time_cost = 3
  - memory_cost = 64 MiB (65 536 KiB)
  - parallelism = 4
  - hash_len = 32, salt_len = 16
- Хеш хранится строкой формата `$argon2id$v=19$m=65536,t=3,p=4$<salt_b64>$<hash_b64>` в поле `users.password_hash VARCHAR(255)` (NULL если пароль ещё не задан).
- При логине — `PasswordHasher.verify()`. Если `check_needs_rehash()` true (например, повысили cost) — re-hash и сохраняем.

## Consequences

**Плюсы:**
- Memory-hard защита от GPU.
- Стандарт OWASP 2024.
- Готовая библиотека, никаких костылей.

**Минусы:**
- ~50–100 ms на верификацию пароля. Для login это приемлемо и даже желательно (анти-brute-force). Rate-limit (см. ADR-0009) дополнительно ограничивает попытки.

## Alternatives considered

- **bcrypt**: рабочий вариант, но без memory-hardness. Отклонено в пользу более современного.
- **scrypt**: чуть менее распространённая поддержка; argon2id предпочтителен в новых проектах.
