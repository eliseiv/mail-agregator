# ADR-0045 — External Outlook OAuth (headless): восстановление consent-flow для добавления Outlook-ящиков из CRM

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-07-10 |

**Амендмент** [ADR-0044](./ADR-0044-decommission-runbook.md) §7 / Phase A3 / Phase G (судьба `oauth_router`, что оставить из `oauth/`, env-чистка). Расширяет [ADR-0025](./ADR-0025-outlook-oauth2.md) (Outlook OAuth) на **headless-путь** (без session/UI) и [ADR-0039](./ADR-0039-external-write-api.md) (external write API). Парный ADR в CRM — `ADR-045`. **Закрывает `TD-052`** (headless OAuth-consent re-onboarding недоступен после сноса UI).

## Context

`ADR-0044` §7 снял session-роутер `backend/app/oauth/router.py` (человеко-обращён: `authorize` требует `CurrentUser`, `callback` создаёт ящик от session-владельца и редиректит на `/accounts`) и зафиксировал пробел `TD-052`: **завести новый Outlook-ящик по OAuth-consent или переподключить ящик в `oauth_needs_consent` headless нельзя**. `OutlookTokenService` (refresh существующих `oauth_outlook`-ящиков) остался живым в worker'е — существующие ящики синкаются, но onboarding/re-consent недоступен.

CRM (парный `ADR-044`) — единственный UI и источник истины по владению ящик↔команда (`mail_accounts.team_id` **в CRM**). Агрегатор — чистый connector: держит IMAP/SMTP-креды, AES-256-GCM (`MailPasswordCipher`), OAuth-токены (`oauth_refresh_token_encrypted`), Redis. `OUTLOOK_CLIENT_SECRET` и обмен `code`→токены **обязаны** оставаться здесь. Значит consent-flow восстанавливается **на стороне агрегатора**, но **инициируется и привязывается к команде из CRM**.

Проверено по коду (сохраняется после демонтажа):
- `backend/app/oauth/service.py::OutlookOAuthService.build_authorize_url` (state+PKCE S256, Redis `oauth_state:{state}`) и `.exchange_code` (валидация state, code→токены, resolve email из `id_token`, create/relink `mail_accounts`) — переиспользуемы. `OutlookTokenService` — не трогаем.
- `backend/app/external/router.py` — **сохраняется** (`ADR-0044` §5), auth-flow `rate-limit → key → gate → write-gate → body` (ADR-0039 §1).
- Env `OUTLOOK_CLIENT_ID`/`OUTLOOK_CLIENT_SECRET`/`OUTLOOK_REDIRECT_URI`/`OUTLOOK_TENANT`/`OUTLOOK_OAUTH_STATE_TTL_SECONDS` (`shared/config.py:317-330`); `outlook_oauth_enabled ⇔ bool(client_id and client_secret)` (`:532-539`); `outlook_authorize_endpoint`/`outlook_token_endpoint` (`:650-657`).

## Decision

### §1. `OutlookOAuthService` остаётся и адаптируется под headless (амендмент `ADR-0044` §7/A3)

- **Не удалять** `OutlookOAuthService` из `oauth/service.py` (файл и так «оставить», `ADR-0044` §4/§5). Удаляется **только** session-роутер `oauth/router.py` (верно — человеко-обращён).
- Адаптация под headless:
  - `OAuthState` (Redis-payload) несёт **`{ code_verifier, crm_state }`** вместо `{ user_id, code_verifier }`. `crm_state` — непрозрачная строка от CRM (≤512 симв.), агрегатор её **не интерпретирует**, только хранит и возвращает.
  - `build_authorize_url(crm_state)` минтит state+PKCE, кладёт `{code_verifier, crm_state}` в Redis, собирает Microsoft URL (как раньше).
  - `exchange_code(code, state)` резолвит `{code_verifier, crm_state}`, обменивает `code`, резолвит email; создаёт `mail_accounts` **owner = `crm-service`** (`seed_crm_service_user`, ADR-0039; **не** session-user). Re-consent существующего ящика (`find_by_user_email(crm-service, email)`) — обновляет токены (как раньше).
  - **`group_id` — transition-safe (важно: OAuth-роуты деплоятся РАНЬШЕ демонтажа).** В текущем коде `MailAccountsRepo.insert_oauth_account_with_id` (`repositories/mail_accounts.py:296`) требует `group_id` как **обязательный kwarg без default**, а колонка `mail_accounts.group_id` дропается только в демонтаже (`ADR-0044` §3, Фаза C). Поэтому: **до drop-миграции вызывать `group_id=None`** (ящик без группы — колонка nullable, FK `SET NULL`); опустить аргумент `group_id` / удалить его из сигнатуры репозитория — **только ПОСЛЕ** снятия колонки в демонтаже (той же лок-степ-парой `ADR-0044` §3, что снимает `MailAccount.group_id`). Так OAuth-flow работает и до, и после демонтажа без `UndefinedColumn`/`TypeError`.
  - После create/relink — вызывает CRM-уведомление (§3), возвращает `(mail_account, crm_state)`.

### §2. Новые external-эндпоинты (в сохраняемом `external/router.py`)

Под тем же `EXTERNAL_API_KEY` / `LIMIT_EXTERNAL_WRITE` / auth-flow ADR-0039 §1. Полные схемы — [04-api-contracts.md §4f-oauth](../04-api-contracts.md#4f-oauth-external-outlook-oauth-adr-0045).

- **`POST /api/external/mailboxes/oauth/authorize`** (write-gate `EXTERNAL_WRITE_ENABLED`): тело `ExternalOAuthAuthorizeRequest{ crm_state: str }`. `outlook_oauth_enabled=false` → `404 not_found` (фича скрыта, симметрично старому `_require_enabled`). Иначе → `build_authorize_url(crm_state)` → `200 ExternalOAuthAuthorizeResponse{ authorize_url: str, state: str }`. Порядок: `consume(LIMIT_EXTERNAL_WRITE, ip)` → `_authenticate` → write-gate → `_require_enabled` (404) → body → delegate.
- **`GET /api/external/mailboxes/oauth/callback`** (зарегистрированный `redirect_uri`; **без ключа/сессии** — авторизация одноразовым `state` в Redis + PKCE; CSRF-exempt по префиксу `/api/external/`): query `code`/`state`/`error`/`error_description`. `outlook_oauth_enabled=false` → `404`. `error` (consent отклонён) / нет `code`/`state` / битый state / сбой обмена → минимальная **self-contained HTML-страница ошибки** (агрегатор без Jinja/templates после демонтажа — HTML инлайн-строкой в хендлере, ящик НЕ создаётся). Успех: `exchange_code` (create owner=`crm-service`/relink) → **уведомить CRM** (§3) → минимальная HTML-страница **«Outlook подключён — вернитесь в CRM»**.
  - Rate-limit callback — по IP (`LIMIT_EXTERNAL_WRITE` или отдельный, на усмотрение реализации; Microsoft-редирект приходит без ключа). `state`-DEL атомарный (реюз `_consume_state`), одноразовость сохраняется.

### §3. Уведомление CRM о созданном/переподключённом ящике (server-to-server, HMAC)

Агрегатор в callback (после успешного create/relink, ДО первого push письма этого ящика) POST'ит CRM:
- **`POST {CRM_OAUTH_INGEST_URL} = {CRM}/api/mail/oauth/ingest`** тем же **HMAC-механизмом и секретом `CRM_PUSH_SECRET`**, что `/api/mail/ingest` (`ADR-0043` §2 / CRM `ADR-044` §3): заголовки `X-Mail-Signature: sha256=<hex>`, `X-Mail-Timestamp`, каноническая подпись `str(ts).encode("ascii") + b"." + raw_body_bytes`. Тело `{ crm_state, mail_account_id, email, display_name, is_active }`. CRM привязывает ящик к команде из `crm_state` (CRM `ADR-045` §3).
- Доставка **connect-only-ретрай** (анти-двойная-запись; CRM upsert идемпотентен по `mail_account_id`). Best-effort: сбой не откатывает уже созданный ящик — reconcile добирает (CRM `TD-047`). Не блокирует HTML-ответ оператору дольше короткого таймаута.
- Новый env агрегатора **`CRM_OAUTH_INGEST_URL`** (URL CRM-приёмника; пустой → уведомление не шлётся, endpoint фактически выключен — симметрично `crm_status_enabled`). `CRM_PUSH_SECRET` — переиспользуется.

### §4. Env и координация с демонтажём (амендмент `ADR-0044` Phase G)

- **НЕ удалять** при env-чистке (отмена пункта `ADR-0044` §7 «`redirect_uri` не нужен»): `OUTLOOK_CLIENT_ID`, `OUTLOOK_CLIENT_SECRET`, **`OUTLOOK_REDIRECT_URI`** (обновить на **`{APP_BASE_URL}/api/external/mailboxes/oauth/callback`** — одноразовая правка Azure App + env, devops), `OUTLOOK_TENANT`, `OUTLOOK_OAUTH_STATE_TTL_SECONDS`. Добавить `CRM_OAUTH_INGEST_URL`.
- **Redis сохраняется** (уже так) — `oauth_state:{state}` работает.
- **Порядок демонтажа:** новые external-OAuth-роуты добавляются в `external/router.py` в той же/более ранней фазе, что снятие `oauth/router.py` (Phase A3) — они замещают session-роутер, конфликта нет.
- **`TD-052` закрывается** этим ADR.

## Consequences

- Onboarding и re-consent Outlook-ящиков из CRM восстановлены headless: authorize-URL + обмен `code` + хранение токенов — в агрегаторе; привязка к команде — в CRM через HMAC-уведомление. `OUTLOOK_CLIENT_SECRET`/токены из агрегатора не выходят.
- `OutlookOAuthService` сохранён и адаптирован (owner=`crm-service`, без `group_id`); `oauth/router.py` (session) снят как и планировалось.
- Новых секретов нет: реюз `EXTERNAL_API_KEY` (входящий), `CRM_PUSH_SECRET` (исходящий HMAC), `OUTLOOK_*`. Один новый URL-env `CRM_OAUTH_INGEST_URL`.
- Ограничения: personal-Outlook only (`OUTLOOK_TENANT=consumers`; корпоративный O365 — CRM `TD-049`); OAuth e2e не подтверждён на реальном Azure App (`TD-031`).

## Alternatives considered

- **Оставить `TD-052` открытым (onboarding недоступен).** Отклонён: владелец добавляет Outlook-ящики (одна из двух потерянных функций); connector обязан их принимать.
- **CRM владеет callback (Microsoft редиректит на CRM).** Отклонён: требует `OUTLOOK_CLIENT_SECRET` + code-exchange + AES-GCM в CRM; нарушает «токены только в агрегаторе».
- **Хранить `crm_state`→mailbox в агрегаторе + CRM-poll `confirm`.** Отклонён: лишний round-trip и состояние; событийный HMAC-push (§3) доставляет привязку без опроса (зеркалит `/api/mail/ingest`).
- **Отдельный секрет/ключ для `/oauth/ingest`.** Отклонён: `CRM_PUSH_SECRET` уже общий для push агрегатор→CRM; расширение на этот вызов симметрично и не плодит секретов.
