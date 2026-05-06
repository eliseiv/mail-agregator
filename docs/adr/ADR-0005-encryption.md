# ADR-0005: Шифрование почтовых паролей — AES-256-GCM

- **Статус:** accepted
- **Дата:** 2026-05-05

## Context

Сервис хранит пароли от внешних почтовых аккаунтов пользователей. В отличие от паролей пользователей сервиса (которые мы только хешируем), эти пароли мы должны иметь возможность **расшифровать** для подключения к IMAP/SMTP.

Требования:
- Шифрование at-rest в БД.
- Authenticated encryption (защита от подмены ciphertext).
- Один мастер-ключ из env, ротация возможна без потери данных.
- Не зависеть от vendor (никаких AWS KMS).

## Decision

- Алгоритм: **AES-256-GCM** через `cryptography` (`cryptography.hazmat.primitives.ciphers.aead.AESGCM`).
- Мастер-ключ: переменная окружения `MAIL_ENCRYPTION_KEY` — 32 байта в base64 (44 символа). Генерация при первом деплое: `python -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"`.
- Версионирование ключа: переменные `MAIL_ENCRYPTION_KEY` (current) и опционально `MAIL_ENCRYPTION_KEY_PREV` (для ротации).
- Поле в БД (см. `03-data-model.md`, таблица `mail_accounts`):
  - `encrypted_password BYTEA NOT NULL` — формат: `version_byte (1B) || iv (12B) || ciphertext_with_tag (variable)`. `version_byte=0x01` соответствует ключу `MAIL_ENCRYPTION_KEY`, `0x00` — `MAIL_ENCRYPTION_KEY_PREV` (если используется во время ротации).
- AAD (associated data): `b"mail_account_password|" + str(mail_account_id).encode()` — байтовая привязка к ID аккаунта. Препятствует переносу зашифрованного пароля между аккаунтами.
- IV: уникальный 96-битный, `os.urandom(12)`, генерируется при каждом сохранении.
- Расшифровка: `read version_byte -> select key -> AESGCM(key).decrypt(iv, ct, aad)`. Если AAD не совпадает или tag invalid — `InvalidTag` исключение, ошибка логируется, операция отклоняется.

### Схема ротации ключей

1. Установить `MAIL_ENCRYPTION_KEY_PREV = <старый>`, `MAIL_ENCRYPTION_KEY = <новый>`. Перезапустить сервисы.
2. Запустить `mas-cli reencrypt` (отдельная команда worker-image): для каждой записи `mail_accounts` — расшифровать (старый ключ по version_byte=0x00), зашифровать новым (`version_byte=0x01`), сохранить.
3. После 100% завершения — удалить `MAIL_ENCRYPTION_KEY_PREV` из env, перезапустить.

## Consequences

**Плюсы:**
- AES-GCM — стандарт, аудитированный, FIPS-friendly.
- Per-record IV + AAD предотвращают tampering и копирование между записями.
- Явный versioning поддерживает плановую ротацию.

**Минусы:**
- Утечка `MAIL_ENCRYPTION_KEY` = компрометация всех почтовых паролей. Mitigation: env-only, ограничения доступа к серверу, ротация раз в год (см. `06-security.md`).

## Alternatives considered

- **Fernet (cryptography)**: использует AES-128-CBC + HMAC; не GCM, ключ 32-байтовый, но 128-bit AES. Отклонено — хотим 256-bit.
- **libsodium / nacl SecretBox**: хороший вариант (XSalsa20-Poly1305), но `cryptography` уже у нас как зависимость и AES-GCM достаточно.
- **Хранение в Vault**: оверкилл для текущего scope; в будущем — отдельный ADR.
