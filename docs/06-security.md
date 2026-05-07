# 06. Security

Этот документ — нормативный по безопасности. STRIDE-модель угроз основных потоков, схема шифрования почтовых паролей, хеширование паролей сервиса, сессии, CSRF, rate-limit, audit log, политика ротации ключей.

---

## 1. STRIDE по основным flow

### 1.1 Login

| Угроза | Описание | Митигация |
| --- | --- | --- |
| **S**poofing identity | Кража сессионного cookie | HttpOnly + Secure + SameSite=Lax; короткий sliding TTL (12h); abs TTL 7d; revoke при подозрении |
| **T**ampering | Модификация cookie | opaque random token; hash-lookup в Redis; cookie без подписи бесполезен |
| **R**epudiation | Отрицание факта входа | `last_login_at`, `admin_audit` для admin login |
| **I**nformation disclosure | Утечка существования username через timing/error | Generic "invalid credentials"; argon2 всегда выполняется (даже при отсутствии user) — сравнение с фиксированным dummy hash |
| **D**oS | Brute-force | rate-limit 5/15min per username + IP; lockout 15 min при 5 failures (ADR-0009) |
| **E**levation | Получение admin-сессии без пароля | `is_admin` берётся из БД при создании сессии; нет смены роли через payload |

### 1.2 Set password

| Угроза | Описание | Митигация |
| --- | --- | --- |
| S | Перехват setup-cookie | HttpOnly + Secure; TTL 15 мин; одноразовая (revoke после set) |
| T | Подмена user_id в setup-session | Хранится server-side в Redis, клиент не видит |
| R | Слабый пароль | Min 12 chars, требуется буква + цифра |
| I | Утечка пароля в логах | Redact-list (см. ADR-0014) |
| D | Брут setup endpoint | rate-limit 5/15min per setup-session/IP |
| E | Установка пароля чужому user | setup-session содержит user_id, не принимается из формы |

### 1.3 Add mail account

| Угроза | Описание | Митигация |
| --- | --- | --- |
| S | Подмена user (CSRF) | CSRF double-submit + server-side check |
| T | Подмена IMAP/SMTP host на свой | Это пользователь сам себе вводит; защита: только сам owner видит |
| R | Логирование пароля провайдера | Redact-list; в audit пишется только `mail_account_id`, без пароля |
| I | Кража мастер-ключа -> расшифровка всех паролей | env-only, restricted file perms; ротация раз в год |
| I | Кража БД -> расшифровка | Без `MAIL_ENCRYPTION_KEY` blob бесполезен (AES-GCM); защита БД-бэкапов = защита ключа |
| D | Скан портов через POST test (SSRF-like) | Валидация: порт в 1..65535; host — RFC valid hostname; **запрет** загрузки с приватных IP-адресов: backend перед connect резолвит DNS и отказывает, если результат — 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 127.0.0.0/8, 169.254.0.0/16, ::1, fc00::/7 (см. секцию 4 ниже) |
| E | Кража чужой почты | Test-login требует валидных credentials провайдера; сервис не сохраняет, если IMAP/SMTP отвергают |

### 1.4 Read message + download attachment

| Угроза | Описание | Митигация |
| --- | --- | --- |
| S | IDOR (доступ к чужому письму) | Все queries имеют JOIN по `mail_accounts.user_id = :user_id` |
| T | Подмена attachment URL | Каждый GET attachment проверяет ownership через JOIN по messages -> mail_accounts |
| I | XSS через HTML письма | Тела хранятся plain text (ADR-0012); UI рендерит как `<pre>` или escape'ит |
| I | XSS через filename | sanitize при выводе в HTML (`|e` Jinja2 default); Content-Disposition с правильным RFC 5987 encoding |
| D | Огромное вложение -> DoS | Max 25 MiB на attachment; не загружаем больше (skipped_too_large) |

### 1.5 Send message

| Угроза | Описание | Митигация |
| --- | --- | --- |
| S | Отправка от чужого аккаунта | Проверка ownership `from_account_id` |
| T | Подмена headers (Subject injection -> CRLF) | Используем stdlib `email.message.EmailMessage` + `policy.SMTP` — он валидирует и normalize |
| R | Отрицание отправки | Запись в `sent_messages` |
| I | Утечка через BCC раскрытый | BCC удаляется из MIME headers, добавляется только в RCPT TO |
| D | Спам-рассылка через сервис | rate-limit 30/час per user; no anonymous send (только аутентифицированные) |
| E | Отправка от имени admin | from_account_id принадлежит обычному user; admin сам имеет account только если ему создали (но он же админ — управляет, не пользуется) |

### 1.6 Sync cycle (worker)

| Угроза | Описание | Митигация |
| --- | --- | --- |
| S | Подделка ответа IMAP сервера (MITM) | Все IMAP-соединения только TLS (imap_ssl=true рекомендованный default); если provider возвращает неподдерживаемый сертификат — connect fail |
| T | Подмена UID в БД (если кто-то получил DB-доступ) | Это уже компрометация БД; не наша граница |
| R | Логирование IMAP-команд с паролем | imap-tools не логирует пароль; structlog redact-list |
| I | Утечка мастер-ключа из памяти worker'а через crash dump | Linux: ограничение core dump (ulimit -c 0 в Dockerfile); ключ не пишется в FS никем |
| D | Провайдер банит за частые подключения | Cap по semaphore=10; интервал 5 минут; не используем IDLE |
| E | Worker получает доступ к чужим данным | Worker имеет полный доступ к БД (по дизайну); граница — сетевая изоляция в docker-compose |

### 1.7 Admin actions

| Угроза | Описание | Митигация |
| --- | --- | --- |
| S | Spoof admin via cookie theft | Защита та же, что для user (sec 1.1); admin-сессии помечены `role=admin` в Redis, проверяется на каждом admin endpoint |
| T | Privilege escalation | `is_admin` фиксируется при создании сессии из БД, не из cookie/JWT |
| R | Скрытие действий | Admin actions всегда пишут `admin_audit` |
| I | — | — |
| D | Brute admin password | Тот же rate-limit + lockout |
| E | Self-delete admin | Endpoint отказывает (`cannot_delete_admin`) |

### 1.8 Telegram webhook (ADR-0018)

| Угроза | Описание | Митигация |
| --- | --- | --- |
| S | Поддельный webhook от чужого процесса | Двойная проверка `TELEGRAM_WEBHOOK_SECRET`: (1) в URL-path `/api/telegram/webhook/{secret}`, (2) в header `X-Telegram-Bot-Api-Secret-Token` (выставляется Telegram'ом из аргумента `setWebhook?secret_token=`). Несовпадение любого — 403, без обработки body |
| T | Подмена body update'а | Telegram гарантирует целостность через TLS до своего edge; secret-проверка отсекает не-Telegram отправителя |
| R | Логирование Bot-token | `TELEGRAM_BOT_TOKEN` в structlog redact-list рядом с `MAIL_ENCRYPTION_KEY`/`password`/`session_token` (см. ADR-0014); webhook-handler НЕ логирует path-segment `{secret}` (только хэш / маркер `present|absent`) — иначе secret попадёт в access-log nginx |
| I | Утечка Bot-token | env-only, `chmod 600`; компрометация позволяет атакующему слать сообщения от имени бота, но НЕ даёт доступа к user-данным сервиса (нет линковки telegram_user_id ↔ user_id) |
| D | Шквал spoofed webhook'ов | Rate-limit `60/min per IP` на webhook-роуте (см. `04-api-contracts.md` секция 4a); 403 на secret fail возвращается после rate-limit checks |
| E | Получение auth/session через Telegram | Намеренно отсутствует. Бот — только launcher; пользователь, открывший WebApp, проходит обычный two-step login (ADR-0016). Telegram не может создать session без знания username+password |

Дополнительно (общая позиция по WebApp):
- WebApp открывается на основном URL сервиса. Telegram WebView shares cookies with system WebView; auth-cookies (`mas_session`, `mas_csrf`) работают штатно с `SameSite=Lax` + `Secure` поверх HTTPS.
- В WebView невозможна attack `frame-ancestors` (Telegram не вкладывает наш URL в iframe — он открывает в native WebView), CSP `frame-ancestors 'none'` сохраняется.
- Никаких новых таблиц/полей в БД — нет surface area для атак на персональные данные через бот-канал.

---

## 2. Шифрование почтовых паролей (схема)

См. также ADR-0005.

```
plaintext (UTF-8 string, max 256 chars)
   │
   ├── key  = base64decode(env.MAIL_ENCRYPTION_KEY)   # 32 bytes
   ├── iv   = os.urandom(12)                          # 96 bits
   ├── aad  = b"mail_account_password|" + str(mail_account_id).encode("ascii")
   ▼
ciphertext + tag = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), aad)
   │
   ▼
blob = b"\x01" || iv (12B) || ciphertext_with_tag (variable)
            ^
            │
            └── version_byte: 0x01 = current key, 0x00 = previous (для rotation)
```

Decrypt:
1. `version_byte = blob[0]`.
2. Выбор ключа: 0x01 -> `MAIL_ENCRYPTION_KEY`, 0x00 -> `MAIL_ENCRYPTION_KEY_PREV` (если задан, иначе ошибка).
3. `iv = blob[1:13]; ct = blob[13:]`.
4. `plaintext = AESGCM(key).decrypt(iv, ct, aad)`.

**AAD-привязка** к `mail_account_id`: атакующий, даже имея БД, не сможет переставить blob между записями (расшифровка упадёт на InvalidTag).

**Невозможность INSERT без id**: используется `nextval('mail_accounts_id_seq')` для предсказания id, шифрование с этим id, INSERT с явным id (см. модуль `crypto` в `05-modules.md`).

---

## 3. Хеширование паролей (argon2id)

См. ADR-0006. Параметры:

```
time_cost   = 3
memory_cost = 65536 KiB (64 MiB)
parallelism = 4
hash_len    = 32
salt_len    = 16
```

Хранение: `users.password_hash VARCHAR(255)`. Формат `$argon2id$v=19$m=65536,t=3,p=4$<salt_b64>$<hash_b64>`.

При login:
1. argon2 verify.
2. Если `check_needs_rehash()` -> rehash + UPDATE.

**Анти-timing**: при отсутствии user — выполняется dummy verify против фиксированного hash, возвращается 401. Время ответа сравнимо с реальным.

---

## 4. SSRF-защита для IMAP/SMTP test/connect

Перед открытием IMAP/SMTP-соединения backend (для test) и worker (для sync) **обязаны**:
1. DNS-resolve `host` (A + AAAA).
2. Проверить, что ни один резолвленный адрес не входит в:
   - `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`, `169.254.0.0/16`, `0.0.0.0/8`, `100.64.0.0/10`;
   - `::1/128`, `fc00::/7`, `fe80::/10`.
3. При попадании — отказ с `code=invalid_host`.

Reason: предотвращение использования сервиса как SSRF-зонда внутренней сети (например, попытка подключиться к `127.0.0.1:6379` Redis).

Исключение для dev-режима (`APP_ENV=dev`): private IPs разрешены (нужно для теста с локальным mock-сервером IMAP).

---

## 5. Сессии

См. ADR-0004. Сводно:

| Параметр | Значение |
| --- | --- |
| Storage | Redis (`session:{token}` JSON) |
| Token | 32 random bytes -> base64url |
| Cookie name | `mas_session` |
| Cookie attrs | `HttpOnly`, `Secure` (prod), `SameSite=Lax`, `Path=/` |
| Sliding TTL | 12 hours |
| Absolute TTL | 7 days |
| Revoke | DEL key + SREM из `user_sessions:{user_id}` |
| Force revoke per user | Через set `user_sessions:{user_id}` |

CSRF: см. ADR-0010. `mas_csrf` cookie + `X-CSRF-Token` header / `csrf_token` form field; double-submit + server-side compare.

**Method override и CSRF.** `MethodOverrideMiddleware` (см. ADR-0015 и `05-modules.md` модуль 13) переписывает `request.method` (`POST` → `DELETE`/`PATCH`/`PUT`) для whitelist-роутов на основании скрытого поля `_method` в form-body. CSRF-проверка выполняется **после** override и видит итоговый метод; токен в скрытом поле формы `csrf_token` обязателен наравне с любым другим state-changing запросом. Никаких bypass'ов CSRF, auth или rate-limit для override не предусмотрено. Запрос с `_method` вне whitelist-роута возвращает `400 method_override_not_allowed` (см. `04-api-contracts.md`).

---

## 6. HTTP security headers

Устанавливаются на каждом HTML-ответе и (минимум — `X-Content-Type-Options`, `X-Request-ID`) на каждом JSON-ответе.

| Заголовок | Значение | Зачем |
| --- | --- | --- |
| `Content-Security-Policy` | `default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self' https://telegram.org; form-action 'self'; frame-ancestors 'none'; base-uri 'self'` | XSS, clickjacking, data exfiltration. `script-src` включает `https://telegram.org` для официального Telegram WebApp SDK (`telegram-web-app.js`) — см. ADR-0018; CDN отдаёт только этот один файл |
| `X-Content-Type-Options` | `nosniff` | MIME confusion |
| `X-Frame-Options` | `DENY` | Clickjacking (legacy, дополнение к CSP frame-ancestors) |
| `Referrer-Policy` | `same-origin` | Минимизация утечек |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` | (только prod) HTTPS enforcement |
| `Cache-Control` | `no-store` (HTML под auth) | Sensitive data cache |
| `Permissions-Policy` | `geolocation=(), camera=(), microphone=()` | Default-deny |

CSP запрещает inline JS — все скрипты только из `/static/js/` и единственного external `https://telegram.org/js/telegram-web-app.js` (см. ADR-0018). Inline-данные в шаблоны — через `data-*` атрибуты, не `<script>`. CSP `style-src` остаётся строгим (`'self'`) — Telegram SDK не подгружает CSS.

---

## 7. Rate limiting & lockout

См. ADR-0009. Сводная таблица в `04-api-contracts.md` секция 8.

Все 429 и lockout-события пишутся:
- В application log (level=info).
- При `lockout_triggered` для существующего user — в `admin_audit` с `action="lockout_triggered"`, `target_user_id`, `details={ip}`.

---

## 8. Audit log

- Хранится в `admin_audit` (таблица в `03-data-model.md`).
- Все admin actions: create_user, reset_password, delete_user, admin_login, admin_logout.
- Authentication-related: lockout_triggered.
- System: account_auto_disabled (worker отключил аккаунт за 3 fail).
- Доступен через `/admin/audit` UI и `GET /api/admin/audit`.
- Бессрочное хранение.
- WORM-семантика — нет UPDATE/DELETE на `admin_audit` (приложение не делает; на уровне БД ограничение можно ввести через REVOKE permissions для роли app — рекомендация для devops, optional).

---

## 9. TLS / в проде

- Reverse proxy (nginx 1.27) обязателен в проде.
- Сертификат Let's Encrypt получается через certbot/webroot (см. `07-deployment.md` sec. 6).
- Backend `api` слушает только на internal docker network, не публикуется наружу.
- Минимальная версия TLS — 1.2 (nginx 1.27 default — см. `deploy/nginx/nginx.conf`), включён 1.3.
- HSTS (`max-age=63072000; includeSubDomains; preload`) выставляется на nginx-уровне в server-блоке `:443` — единая точка ответственности; backend такой header не дублирует.

---

## 10. Рекомендации по ротации ключей

| Ключ | Частота | Процедура |
| --- | --- | --- |
| `MAIL_ENCRYPTION_KEY` | Раз в 12 месяцев или при компрометации | См. ADR-0005 (`mas-cli reencrypt`) |
| `ADMIN_PASSWORD` | По требованию | Обновить `.env` → `docker compose restart api worker`. `seed_super_admin` идемпотентно перезапишет `users.password_hash` (см. `07-deployment.md` sec. 11.1). UI смены пароля для супер-админа сознательно не предусмотрен. |
| Session cookie name / domain | По требованию | Через env, разовая настройка |

`MAIL_ENCRYPTION_KEY` ротация (детально):
1. Сгенерировать новый: `python -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"`.
2. Установить env:
   - `MAIL_ENCRYPTION_KEY_PREV=<старый>`
   - `MAIL_ENCRYPTION_KEY=<новый>`
3. `docker compose up -d --force-recreate api worker`.
4. `docker compose run --rm worker python -m worker.cli reencrypt` — пересохраняет все blob с version_byte=0x01.
5. Удалить `MAIL_ENCRYPTION_KEY_PREV` из env. Ещё раз `up -d --force-recreate`.

---

## 11. Резервные копии (kratko, детали в 07-deployment.md)

- БД: ежедневный `pg_dump` (логический), хранение 14 дней.
- MinIO: `mc mirror` или snapshot volume; хранение 14 дней.
- `MAIL_ENCRYPTION_KEY` хранится отдельно (например, password manager / sealed env). Без него restore БД бесполезен — почтовые пароли не расшифровываются.

---

## 12. MinIO — least-privilege для приложения

Сервис MinIO запускается с парой root-credentials (`MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`), но эти ключи **не передаются** в `api`/`worker`. Вместо этого:

1. При первом старте compose-проекта одноразовый init-контейнер `minio-bootstrap` (на базе `minio/mc`) подключается под root, создаёт bucket `mail-attachments`, политику `mas-app` и service account `MINIO_APP_ACCESS_KEY` / `MINIO_APP_SECRET_KEY`.
2. Политика `mas-app` разрешает только `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket`, `s3:GetBucketLocation` на ресурс `arn:aws:s3:::mail-attachments` (и `/*`).
3. `api` и `worker` получают через env только `MINIO_APP_*` — root-ключ им недоступен.
4. Все операции `mc` идемпотентны — `minio-bootstrap` безопасно перезапускается.

Подробности и пример docker-compose / `mc`-скрипт — в `07-deployment.md` секция 12. Таблица env-переменных там же.

Последствия компрометации:
- Утечка `MINIO_APP_*`: атакующий получает CRUD на единственный bucket; не может управлять другими bucket'ами, пользователями, политиками MinIO.
- Утечка `MINIO_ROOT_*`: полный контроль над MinIO. Хранятся **только** на сервере в `.env` (`chmod 600`); в `api`/`worker` контейнеры не передаются.

## 13. Принципы

1. **Defense in depth**: даже если один слой прорван (например, XSS) — следующий (CSP, HttpOnly cookie, server-side session) должен сдержать.
2. **Least privilege**: app-роль в Postgres имеет CRUD на свои таблицы, NO ROLE GRANTS суперпользователя.
3. **Fail closed**: при отсутствии явного разрешения — запрет. Например, нет flag `is_active=true` -> sync пропускает.
4. **Никогда не доверять клиенту**: все ownership-проверки выполняются на сервере, никогда не на основании submitted параметров.
5. **Все секреты — через env**, никогда в git, никогда в логах.
