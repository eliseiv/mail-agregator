# ADR-0025 — OAuth2 (XOAUTH2) для личных аккаунтов Outlook

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-05-27 |
| Связан | [ADR-0005](./ADR-0005-encryption.md) (AES-256-GCM, переиспользуем шифр для refresh-токена), [ADR-0002](./ADR-0002-imap-library.md) (imap-tools), [ADR-0008](./ADR-0008-sync-strategy.md) (IMAP-стратегия не меняется), [ADR-0001](./ADR-0001-tech-stack.md). |
| Спринт | B (независим от ADR-0024 / multi-TG) |

## Context

Microsoft с сентября 2024 отключает Basic Auth (LOGIN по паролю) для личных ящиков `outlook.com`/`hotmail.com`/`live.com`. Текущая архитектура (`mail_accounts` + `MailPasswordCipher`, `imap_fetcher` через `imap-tools` `mailbox.login(user, password)`, SMTP через `aiosmtplib.login`) поддерживает только пароль/app-password. Для личных Outlook нужен **OAuth2 + SASL XOAUTH2**.

Решения пользователя (зафиксированы как вход):
- Личные ящики (`outlook.com`/`hotmail.com`/`live.com`), tenant `common` (см. §6 / историю изменений — изначально `consumers`, заменён на `common` из-за IMAP XOAUTH2 "User is authenticated but not connected").
- Consent — через **наш сайт + OctoBrowser**: кнопка «Подключить Outlook» → редирект на Microsoft authorize → пользователь открывает ссылку в нужном OctoBrowser-профиле, проходит вход/согласие → callback с `code` → обмен на токены.
- IMAP-сбор — **напрямую** с сервера (без прокси) через XOAUTH2.
- Refresh-токен — хранить зашифрованно.
- Azure App: «Accounts in any organizational directory and personal Microsoft accounts» (multitenant + personal — обязательно для tenant `common`; ранее планировалось «Personal Microsoft accounts only» под `consumers`, см. историю изменений), `client_id`, `client_secret`, `redirect_uri`, delegated scopes: `IMAP.AccessAsUser.All`, `SMTP.Send`, `offline_access`, `openid`, `email`, `profile`.

Microsoft endpoints (tenant `common` — см. историю изменений):
- authorize: `https://login.microsoftonline.com/common/oauth2/v2.0/authorize`
- token: `https://login.microsoftonline.com/common/oauth2/v2.0/token`
- IMAP: `outlook.office365.com:993` SSL, SASL XOAUTH2.
- SMTP: `smtp-mail.outlook.com:587` STARTTLS, SASL XOAUTH2.

> **Требует проверки при реализации (Q-OAUTH-3):** Microsoft периодически меняет статус IMAP/SMTP XOAUTH2 для personal accounts и список допустимых scopes для tenant. Перед e2e нужно подтвердить на реальном Azure App, что `IMAP.AccessAsUser.All` + `SMTP.Send` выдаются для personal accounts и что `imap-tools` корректно строит XOAUTH2 (см. §5).

## Decision

### 1. Хранение — расширить `mail_accounts`, без отдельной таблицы

Сравнили: (a) отдельная таблица `oauth_tokens(account_id PK, …)` vs (b) колонки в `mail_accounts`. Выбран **(b)** — расширение `mail_accounts`. Обоснование: связь 1:1 (один аккаунт — один набор токенов), отдельная таблица дала бы лишний JOIN на каждом sync без выигрыша; `mail_accounts` уже владеет credentials-полями и `MailPasswordCipher`-блобами.

Новые колонки `mail_accounts`:

| Колонка | Тип | Описание |
| --- | --- | --- |
| `auth_type` | TEXT NOT NULL DEFAULT `'password'` | `'password'` \| `'oauth_outlook'`. CHECK на эти значения. |
| `oauth_provider` | TEXT NULL | `'outlook'` (под будущих провайдеров). NULL для password-аккаунтов. |
| `oauth_refresh_token_encrypted` | BYTEA NULL | AES-256-GCM (ADR-0005), AAD=`account_id` (тот же `MailPasswordCipher.encrypt(refresh_token, account_id)`). |
| `oauth_access_token_encrypted` | BYTEA NULL | Кэш access-token (живёт ~1ч). Шифруется так же. Кэш, не источник истины — при потере перевыпускается по refresh. |
| `oauth_access_token_expires_at` | TIMESTAMPTZ NULL | Когда истекает access-token; sync проверяет до коннекта. |
| `oauth_needs_consent` | BOOLEAN NOT NULL DEFAULT false | true → refresh инвалидирован (Microsoft `invalid_grant`); требуется повторный consent. UI показывает «переподключить». worker пропускает (как `is_active=false`, но с отдельной семантикой). |
| `oauth_scopes` | TEXT NULL | Фактически выданные scopes (через пробел) — для диагностики. |
| `proxy_url` | TEXT NULL | **Зарезервировано** под per-account proxy (НЕ реализуется сейчас — TD-029). Опциональное поле, worker/тестеры его игнорируют в этом спринте. |

Для `auth_type='password'` существующие поля (`encrypted_password`, `smtp_*`) работают как раньше. Для `auth_type='oauth_outlook'`:
- `encrypted_password` становится **NULLABLE** (миграция снимает NOT NULL); у oauth-аккаунта пароля нет.
- IMAP/SMTP host/port/ssl заполняются фиксированными Outlook-значениями при создании (см. §2).
- CHECK-констрейнт: `auth_type='password' → encrypted_password IS NOT NULL`; `auth_type='oauth_outlook' → oauth_refresh_token_encrypted IS NOT NULL AND oauth_provider='outlook'`.

### 2. OAuth flow endpoints (наши)

Все — cookie-authenticated (активная сессия), CSRF где POST/redirect-инициация. Контракты — `docs/04-api-contracts.md` §9.

**`GET /api/oauth/outlook/authorize`** (cookie-auth):
1. Генерит `state` = `secrets.token_urlsafe(32)`.
2. Сохраняет в Redis `oauth_state:{state}` = JSON `{user_id, created_at}` TTL = `OUTLOOK_OAUTH_STATE_TTL_SECONDS` (default 600). state привязан к текущему `session.user_id` — callback проверит совпадение.
3. Опционально `code_verifier`/`code_challenge` (PKCE S256) — Microsoft рекомендует PKCE даже для confidential client; сохранить `code_verifier` рядом со state в Redis. (Q-OAUTH-2: PKCE обязателен? Закладываем — дёшево и безопаснее.)
4. Строит Microsoft authorize URL: `client_id`, `response_type=code`, `redirect_uri`, `scope` (= delegated scopes из env + `offline_access`), `state`, `code_challenge`, `code_challenge_method=S256`, `prompt=select_account`.
5. Возвращает `{authorize_url}` (JSON) — фронт показывает ссылку «открыть в OctoBrowser», НЕ делает auto-redirect (пользователь должен открыть в конкретном профиле). Это сознательное отклонение от обычного 302-redirect OAuth.

**`GET /api/oauth/outlook/callback`** (это и есть `redirect_uri`, зарегистрированный в Azure):
1. Принимает `code`, `state` (или `error`/`error_description`).
2. Достаёт `oauth_state:{state}` из Redis (атомарно GET+DEL). Нет/истёк → 400 `oauth_state_invalid`. Проверяет `user_id` == текущая сессия (если callback открыт в OctoBrowser без cookie сессии — см. Q-OAUTH-1).
3. POST на token endpoint: `grant_type=authorization_code`, `code`, `redirect_uri`, `client_id`, `client_secret`, `code_verifier`. Получает `access_token`, `refresh_token`, `expires_in`, `scope`.
4. Получить email аккаунта: из `id_token` (claim `email`/`preferred_username`) или Graph `GET /me`. Использовать как `mail_accounts.email`.
5. **Создать или привязать** `mail_account`: `auth_type='oauth_outlook'`, host/port Outlook (§ниже), `oauth_refresh_token_encrypted`, кэш access-token, `oauth_scopes`. Применяет визибилити/owner-резолв как обычный create (ADR-0019 §8). Если email уже добавлен этим user — обновить токены (re-consent существующего).
6. Редирект на UI («Outlook подключён»).

Фиксированные Outlook-параметры при создании: `imap_host=outlook.office365.com`, `imap_port=993`, `imap_ssl=true`, `smtp_host=smtp-mail.outlook.com`, `smtp_port=587`, `smtp_ssl=false`, `smtp_starttls=true`. (Провайдерная таблица `providers.py` для `outlook.com/hotmail.com/live.com` сейчас указывает `outlook.office365.com`/`smtp.office365.com:587` — для OAuth личных аккаунтов SMTP-хост `smtp-mail.outlook.com`; добавить отдельную ветку, не ломая password-подсказки.)

### 3. Token refresh

Единый helper `OutlookTokenService.get_valid_access_token(account)`:
1. Если `oauth_access_token_expires_at` > now + 60s буфер → расшифровать и вернуть кэш.
2. Иначе POST token endpoint `grant_type=refresh_token` + `refresh_token` (расшифрованный) + `client_id`/`client_secret`/`scope`.
3. Microsoft при refresh может вернуть **новый** refresh_token (rotation) — если вернул, перешифровать и сохранить.
4. Сохранить новый access-token + `expires_at`.
5. На `invalid_grant` (refresh отозван/протух) → `oauth_needs_consent=true`, `is_active` оставить, sync для этого аккаунта пропускать; UI показывает «переподключить Outlook». Audit `oauth_refresh_invalidated`.

Refresh выполняется **в worker** перед IMAP-коннектом и **в backend** перед SMTP-send/test. Конкуррентность: на масштабе ≤5 users параллельный refresh одного аккаунта маловероятен; для надёжности — короткий Redis-lock `oauth_refresh_lock:{account_id}` TTL 30s (best-effort, не блокирует фатально).

### 4. IMAP / SMTP XOAUTH2 — встраивание рядом с password-аккаунтами

`imap_fetcher.fetch_blocking` и `send/service.py` ветвятся по `account.auth_type`:
- `password` → текущий путь (`mailbox.login(user, password)` / `aiosmtplib` login).
- `oauth_outlook` → получить access-token через §3, построить SASL XOAUTH2.

**IMAP XOAUTH2** (`imap-tools` обёртка над `imaplib`): `imaplib.IMAP4_SSL.authenticate("XOAUTH2", lambda x: xoauth2_bytes)`. `imap-tools` `MailBox` не имеет готового `.xoauth2_login`, но даёт доступ к `mailbox.client` (`imaplib`-объект) → вызвать `client.authenticate("XOAUTH2", ...)` после конструктора, минуя `.login`. Нужен thin-helper `_oauth_login(mailbox, user, access_token)`. (Q-OAUTH-3: проверить, что версия imap-tools в lock-файле это позволяет; иначе fallback на голый `imaplib`.)

XOAUTH2 SASL string (base64): `user={email}\x01auth=Bearer {access_token}\x01\x01`.

**SMTP XOAUTH2**: `aiosmtplib` поддерживает `client.auth(...)` низкоуровнево; XOAUTH2 не входит в стандартные `login`-механизмы напрямую → отправить `AUTH XOAUTH2 {base64}` командой через `client.execute_command`, либо использовать поддержку механизма, если версия `aiosmtplib` её предоставляет. Helper `_smtp_oauth_send(...)`.

`testers.py`: добавить `imap_test_oauth` / `smtp_test_oauth` — пробуют XOAUTH2 коннект свежим access-token (для кнопки «проверить» после подключения).

**Редактирование (PATCH) oauth-аккаунтов (§4c).** Для `auth_type='oauth_outlook'` host/port/ssl/credentials фиксированы (Microsoft + XOAUTH2) и **immutable**; редактировать можно **только `display_name`** (никнейм). Форма редактирования общая с password-аккаунтами и всегда отправляет полный снимок (`email/imap_host/imap_port/imap_ssl/smtp_host/smtp_port/smtp_ssl/smtp_starttls/smtp_username` + `display_name`). Поэтому проверка «запрещённого изменения» сравнивает с текущим значением: поле, переданное **равным** текущему значению аккаунта, — это **no-op, не ошибка** и игнорируется. Запрос отклоняется (`400`, `ValidationError`) только если поле передано **и отличается** от текущего, либо передан непустой `password`/`smtp_password` (у oauth-аккаунта пароля нет). `display_name` обновляется как обычно.

### 5. Безопасность

- **Scopes** — минимально необходимые: `IMAP.AccessAsUser.All`, `SMTP.Send`, `offline_access`, `openid`, `email`, `profile`. Никаких Mail.ReadWrite/Graph-широких.
- **state** — CSRF/anti-fixation: 32-байтный random в Redis с TTL, привязка к `user_id`, одноразовый (GET+DEL).
- **PKCE S256** — закладываем (§2.3).
- **Шифрование токенов** — refresh + access через `MailPasswordCipher` (AES-256-GCM, AAD=`account_id`, ADR-0005). Тот же ключ `MAIL_ENCRYPTION_KEY`.
- **Не логировать токены** — добавить `access_token`/`refresh_token`/`code`/`client_secret` в redact-list `shared/logging.py`. token-endpoint ответы не логировать целиком.
- **client_secret** — только env, не в БД, не в коде.
- **redirect_uri** — точное совпадение с зарегистрированным в Azure (Microsoft проверяет).
- Per-account proxy — заложено поле `proxy_url`, **не реализуется** (TD-029).

### 6. Config / env

| Env | Назначение |
| --- | --- |
| `OUTLOOK_CLIENT_ID` | Azure App (Application/client) ID |
| `OUTLOOK_CLIENT_SECRET` | Azure App client secret (redact) |
| `OUTLOOK_REDIRECT_URI` | `{APP_BASE_URL}/api/oauth/outlook/callback` |
| `OUTLOOK_TENANT` | `common` (default) — личные ящики. `consumers` давал IMAP XOAUTH2 "User is authenticated but not connected"; `common` пускает и личные, и рабочие аккаунты (нам нужны личные). |
| `OUTLOOK_OAUTH_STATE_TTL_SECONDS` | default 600 |
| `OUTLOOK_OAUTH_ENABLED` | derived: true когда `OUTLOOK_CLIENT_ID` и `OUTLOOK_CLIENT_SECRET` заданы (по аналогии с `telegram_bot_enabled`). |

Endpoints собираются из `OUTLOOK_TENANT`: `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/{authorize,token}`.

### 7. Миграция `20260527_018_outlook_oauth2`

`up`:
1. `ALTER TABLE mail_accounts ADD COLUMN auth_type TEXT NOT NULL DEFAULT 'password'`.
2. `ADD COLUMN oauth_provider TEXT NULL`, `oauth_refresh_token_encrypted BYTEA NULL`, `oauth_access_token_encrypted BYTEA NULL`, `oauth_access_token_expires_at TIMESTAMPTZ NULL`, `oauth_needs_consent BOOLEAN NOT NULL DEFAULT false`, `oauth_scopes TEXT NULL`, `proxy_url TEXT NULL`.
3. `ALTER COLUMN encrypted_password DROP NOT NULL`.
4. `ADD CONSTRAINT ck_mail_accounts_auth_type CHECK (auth_type IN ('password','oauth_outlook'))`.
5. `ADD CONSTRAINT ck_mail_accounts_password_creds CHECK (auth_type <> 'password' OR encrypted_password IS NOT NULL)`.
6. `ADD CONSTRAINT ck_mail_accounts_oauth_creds CHECK (auth_type <> 'oauth_outlook' OR (oauth_refresh_token_encrypted IS NOT NULL AND oauth_provider = 'outlook'))`.

`down`: drop constraints + columns; восстановить `encrypted_password NOT NULL` (lossy если есть oauth-строки без пароля — задокументировать: down требует предварительного удаления oauth-аккаунтов).

## Consequences

**Плюсы:** личные Outlook работают без Basic Auth; рядом с password-аккаунтами без отдельной таблицы; refresh-токен зашифрован; per-account proxy заложен на будущее.

**Минусы / риски:**
- Зависимость от Microsoft (изменение scopes/XOAUTH2-статуса) — Q-OAUTH-3.
- XOAUTH2 в `imap-tools`/`aiosmtplib` не first-class → thin-helpers через нижележащий `imaplib`/SMTP-команды; хрупко к версиям библиотек.
- Refresh-token rotation: нужно сохранять новый refresh при каждом обновлении, иначе протухнет.
- consent через OctoBrowser-профиль: callback может прийти **без** cookie сессии нашего сайта (другой профиль/браузер) — Q-OAUTH-1.

**Tech debt:**
- **TD-029** — per-account proxy (`proxy_url`) заложен, не реализован. IMAP/SMTP идут напрямую.
- **TD-030** — XOAUTH2 helper'ы зависят от внутренних API `imap-tools`/`aiosmtplib`; при апгрейде библиотек проверять.

## Alternatives considered

1. **Отдельная таблица `oauth_tokens`.** Лишний JOIN, дублирование владения credentials. Отклонено (§1).
2. **Microsoft Graph API вместо IMAP/SMTP.** Уводит от существующей IMAP-pipeline (ADR-0008), требует переписать sync/send и широкие Graph-scopes. Пользователь явно просил IMAP напрямую через XOAUTH2. Отклонено.
3. **Хранить access-token только в Redis (не в БД).** Меньше PII в БД, но теряется при рестарте Redis и усложняет worker/backend шаринг. На масштабе проекта кэш в БД (зашифрован) проще. Отклонено; access-кэш в БД.
4. **Auto-redirect (302) на authorize, как классический OAuth.** Не совместимо с «открыть в нужном OctoBrowser-профиле». Отдаём `authorize_url` строкой. Принято (§2.5).

## Open questions

- **Q-OAUTH-1** — callback приходит в OctoBrowser-профиле без cookie сессии нашего сайта. Как связать callback с инициировавшим user'ом? Варианты: (a) `state` несёт `user_id` (уже так) и callback НЕ требует cookie — доверяем подписанному/Redis-хранимому state; (b) показывать пользователю `code` для ручной вставки. Предпочтительно (a): state в Redis уже привязан к user_id, cookie на callback не обязателен. Подтвердить перед реализацией.
- **Q-OAUTH-2** — PKCE обязателен для confidential client с personal accounts? Закладываем S256 (безопаснее, дёшево). Подтвердить, что Microsoft не конфликтует с `client_secret`+PKCE одновременно.
- **Q-OAUTH-3** — БЛОКЕР для e2e: подтвердить на реальном Azure App, что (1) personal accounts выдают `IMAP.AccessAsUser.All`+`SMTP.Send`, (2) IMAP/SMTP XOAUTH2 для personal accounts активны, (3) версия `imap-tools`/`aiosmtplib` в lock-файле строит XOAUTH2. Код можно написать и протестировать на моках без `client_id/secret`; e2e — после получения Azure App от пользователя.

## История изменений

- **2026-06-01 — tenant `consumers` → `common` (P1-фикс).** Личный Outlook IMAP XOAUTH2 падал на реальном e2e с ошибкой "User is authenticated but not connected": OAuth-токен принимался, но IMAP-сессия не привязывалась. Все подтверждённые рабочие конфиги personal Outlook XOAUTH2 используют tenant `common`, не `consumers`. Azure App зарегистрирован как multitenant + personal, поэтому `common` поддерживается; `common` пускает и личные, и рабочие аккаунты — нам достаточно личных. Изменения: `default OUTLOOK_TENANT = "common"` (`shared/config.py`), обновлены §Context (endpoints + Azure App audience), §6 (env-таблица), `docs/07-deployment.md`. Authorize/token URL теперь `https://login.microsoftonline.com/common/oauth2/v2.0/{authorize,token}`. Scopes, PKCE (S256), `redirect_uri`, refresh-flow не меняются — tenant влияет только на сегмент пути. Env на проде/локали может переопределить default.
