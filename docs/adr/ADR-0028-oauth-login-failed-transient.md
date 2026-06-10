# ADR-0028: «Login failed» от Microsoft IMAP для OAuth-аккаунтов = TRANSIENT (контекстная классификация по `auth_type`)

- **Статус:** accepted
- **Дата:** 2026-06-10
- **Связь:** extends / уточняет [ADR-0026](./ADR-0026-sync-error-resilience.md) §1 (классификация), §2 (поведение по классам), §4 (connect-retry). НЕ supersede — таблица приоритетов ADR-0026 §1 остаётся в силе для password-аккаунтов; ADR-0028 вводит **контекстную ветку** для `auth_type='oauth_outlook'`. Опирается на [ADR-0025](./ADR-0025-outlook-oauth2.md) (XOAUTH2 refresh-flow). НЕ затрагивает [ADR-0008](./ADR-0008-sync-strategy.md).

---

## Context

### Прод-инцидент (диагностирован эмпирически)

После выкатки OAuth (ADR-0025) на проде **25 из 30** Outlook-OAuth-аккаунтов массово
деактивировались (`is_active=false`) с `last_sync_error = "auth_failed: ... Login failed"`.

**Корень (доказан):**

1. Microsoft IMAP (`outlook.office365.com`) **спорадически** отвечает на валидный XOAUTH2
   на **работающем** ящике строкой вида `LOGIN failed.` / `AUTHENTICATIONFAILED` — это
   серверная флуктуация того же класса, что уже известная `User is authenticated but not
   connected` (ADR-0026 §1 rule 3b). Это **не** реальный отказ авторизации.
2. `worker/app/error_classify.py` rule 8 (`_AUTH_SUBSTRINGS`) содержит подстроки
   `"login failed"` и `"authenticationfailed"` → класс **permanent**,
   `error_prefix="auth_failed"`, `is_explicit_permanent=True`.
3. `is_explicit_permanent=True` означает **мгновенный disable** (без порога
   `SYNC_MAX_CONSECUTIVE_FAILURES`): в `sync_cycle._run_for_accounts` фаза 2 для явного
   auth/decrypt вызывает `_disable_after_failures(...)` после **первой** ошибки.
4. `imap_fetcher._is_retryable_imap_error` исключает `"login failed"` /
   `"authenticationfailed"` из retryable (`_PERMANENT_IMAP_SUBSTRINGS`) → спорадик **не
   ретраится** даже один раз внутри цикла.
5. Circuit-breaker (ADR-0026 §3) **не спас**, потому что флаки приходили **не одновременно
   у всех** — `ratio` на цикл оставался ниже `SYNC_MASS_FAILURE_RATIO=0.5`, аккаунты
   выключались по одному в разных циклах, никогда не перешагивая порог брейкера.

**Доказательство, что это флуктуация, а не реальный отказ:** деактивированные аккаунты
реактивировали вручную (`is_active=true, consecutive_failures=0, last_sync_error=NULL`,
**токены/refresh НЕ трогали**) — и они **немедленно засинкались, получили письма**. Значит
refresh-токен, scope и access-токен были **исправны**; `Login failed` на этапе IMAP был
**временным**, а instant-disable — **ложноположительным**.

### Ключ к различению спорадического vs реального auth-отказа

В `sync_cycle._resolve_oauth_access_token` access-токен добывается через
`OutlookTokenService.get_valid_access_token(account)` **ДО** вызова `fetch_blocking`
(IMAP). Этот метод отдаёт токен по **двум** веткам:

- **refresh-ветка:** при истёкшем/отсутствующем кэше выполняется refresh — возвращается
  **только что выпущенный** access-токен (token-эндпоинт подтвердил валидность refresh);
- **cache-hit ветка:** при наличии ещё-свежего кэша возвращается кэшированный токен **без**
  обращения к token-эндпоинту. Критично: `_cached_access_token`
  (`backend/app/oauth/service.py:471–481`) отдаёт кэш **только** если
  `expires_at > now + ACCESS_TOKEN_REFRESH_BUFFER_SECONDS`; **протухший (past-expiry) токен →
  `None`**, что форсирует переход в refresh-ветку. То есть **нет пути**, по которому
  `get_valid_access_token` / `_cached_access_token` вернул бы истёкший токен;
- при `invalid_grant` — поднимается `OAuthRefreshInvalidError` → аккаунт помечается
  `oauth_needs_consent=true`, IMAP **не вызывается вообще** (clean skip, ADR-0025 §3 шаг 5).

**Следствие (инвариант, точная формулировка):** любой `access_token`, **дошедший** до
`fetch_blocking`, — это **ВАЛИДНЫЙ (не истёкший) Microsoft-токен**: либо только что
выпущенный refresh-ветка, либо ещё-свежий из кэша (cache-hit), но **никогда** протухший —
`_cached_access_token` past-expiry возвращает `None` и форсирует refresh
(`service.py:471–481`). Значит любое IMAP-`login failed` / `authenticationfailed`, полученное
**против такого валидного токена**, — это **серверная флуктуация Microsoft**, а **не**
реальный отказ авторизации. Реальный отказ авторизации у OAuth проявляется **только** как
`invalid_grant` на refresh-эндпоинте (→ `oauth_needs_consent`), **никогда** как
IMAP-`login failed`. Это закрывает единственную потенциальную дыру инварианта (cache-hit
ветку) явно: кэш не может отдать протухший токен.

Это и есть архитектурный ключ: **класс `login failed` зависит от контекста `auth_type`** —
для OAuth это transient-флуктуация, для password это реальный неверный пароль (permanent).

### Текущая реализация (на момент ADR)

- `worker/app/error_classify.py`: `classify(exc_or_text)` / `error_prefix(exc_or_text)` /
  `is_explicit_permanent(exc_or_text)` — **без контекста**, чистая функция от текста/типа.
  rule 8 `_AUTH_SUBSTRINGS = ("authenticationfailed", "invalid credentials", "login failed",
  "[alert]", "account is disabled", "account has been blocked")`.
- `worker/app/sync_cycle.py`: `_handle_sync_error(account, exc, *, detail, cycle_log)` —
  имеет `account` (значит **есть** `account.auth_type`), но передаёт в классификатор только
  `exc`. Фаза 2 `_run_for_accounts`: `explicit_permanent` → instant `_disable_after_failures`.
- `worker/app/imap_fetcher.py`: `_is_retryable_imap_error(exc)` — **без контекста**;
  `_PERMANENT_IMAP_SUBSTRINGS` исключает `login failed`/`authenticationfailed` из retry.
  `_connect_and_login(...)` знает, какой путь — `access_token is not None` (XOAUTH2) vs
  `password` (LOGIN), т.е. **контекст доступен** на месте retry-решения.
- `backend/app/oauth/service.py`: `get_valid_access_token` — refresh + `invalid_grant` →
  `OAuthRefreshInvalidError` → `mark_oauth_needs_consent`. **Не меняется этим ADR.**

---

## Decision

### Принцип

`login failed` / `authenticationfailed`, пришедшие как **IMAP-ошибка** на аккаунте
`auth_type='oauth_outlook'`, классифицируются как **TRANSIENT** (retryable, no-disable),
потому что refresh уже подтвердил валидность токена (см. «Ключ к различению»). Для
`auth_type='password'` поведение **не меняется** — `login failed` остаётся **permanent**
(реальный неверный пароль). Контекст (`auth_type`) передаётся в классификацию и в retry
**явно**.

### 1. Контекстная классификация — `auth_type` как параметр `classify()`/`error_prefix()`/`is_explicit_permanent()`

Все три публичные функции `error_classify.py` получают новый **keyword-only** параметр
`auth_type: str | None = None` (default `None` = поведение **строго как сейчас**, обратная
совместимость для всех существующих вызовов и тестов).

Вводится новый набор подстрок — **OAuth-флуктуации авторизации**:

```python
# OAuth IMAP-флуктуации: для oauth_outlook аккаунтов эти подстроки = TRANSIENT
# (refresh уже подтвердил токен ДО IMAP — см. ADR-0028). Подмножество rule 8,
# которое при auth_type='oauth_outlook' переезжает в transient-блок.
_OAUTH_IMAP_AUTH_FLAKE_SUBSTRINGS: tuple[str, ...] = (
    "login failed",
    "authenticationfailed",
)
```

**Правило 7b (новое, в transient-блоке, ПЕРЕД permanent-блоком):**

> Если `auth_type == "oauth_outlook"` **И** текст содержит подстроку из
> `_OAUTH_IMAP_AUTH_FLAKE_SUBSTRINGS` (`login failed` / `authenticationfailed`) **И** текст
> **НЕ** содержит `invalid_grant` → класс **transient**, UI-префикс **`network`**.

- `invalid_grant` **исключается** из правила 7b и продолжает матчиться rule 8 (permanent) —
  если реальный invalid_grant каким-то путём долетит как текст (нормально он не доходит:
  `OAuthRefreshInvalidError` → clean skip до IMAP). Это страхует от случая, когда
  `invalid_grant` встретится в тексте OAuth-аккаунта.
- Префикс **`network`** (а не `auth_failed`) — единый источник UI-префикса и класса
  (инвариант ADR-0026 §1): transient-флуктуация показывается пользователю как сетевая, не
  пугает «неверный пароль».
- Правило 7b в transient-блоке (проверяется **до** rule 8). first-match-wins: для
  OAuth-аккаунта `login failed` ловится 7b (transient) раньше, чем rule 8 (permanent).
- Для `auth_type='password'` (или `None`) правило 7b **не активируется** → `login failed`
  идёт обычным путём rule 8 → **permanent** `auth_failed`. **Поведение password неизменно.**

`is_explicit_permanent(exc, *, auth_type=...)` обязан вернуть **False** для OAuth-флуктуации
(она теперь transient, не permanent) — гарантируется тем же `_matches_transient(...,
auth_type=...)`-guard'ом, что уже есть в функции (transient затеняет permanent).

#### Обновлённая таблица (контекст `auth_type='oauth_outlook'`; для `password`/`None` — таблица ADR-0026 §1 без изменений)

| Приоритет | Условие | Класс | UI-префикс |
| --- | --- | --- | --- |
| 1–7 | как в ADR-0026 §1 (timeout / resolve / rate-limit / 3b conn-flake / timeout-text / network / OAuth-token-error) | transient | как в ADR-0026 §1 |
| **7b (новое)** | `auth_type=='oauth_outlook'` И подстрока `login failed` / `authenticationfailed` И НЕ `invalid_grant` | **transient** | `network` |
| 8 | `authenticationfailed` / `invalid credentials` / `login failed` / `[alert]` / `account is disabled` / `account has been blocked` / `invalid_grant` (для password/None; для oauth — только если НЕ перехвачено 7b, т.е. `invalid_grant` и `invalid credentials`/disabled/blocked) | **permanent** | `auth_failed` |
| 9 | decrypt-fail | **permanent** | `decrypt_fail` |
| 10 | fail-open | transient | `error` |

> **Замечание по `account is disabled` / `account has been blocked` у OAuth.** Эти маркеры
> **НЕ** входят в `_OAUTH_IMAP_AUTH_FLAKE_SUBSTRINGS` → для OAuth-аккаунта они остаются
> **permanent** (rule 8). Это намеренно: «ящик заблокирован/отключён администратором
> Microsoft» — реальное постоянное состояние, не флуктуация. Только `login failed` /
> `authenticationfailed` (именно их Microsoft спорадически отдаёт на валидном XOAUTH2)
> переводятся в transient. Этот выбор зафиксирован; если на проде `account is disabled`
> окажется тоже флаки — пересмотреть отдельным ADR (Q-SYNC-2).

### 2. Передача контекста в `sync_cycle`

`_handle_sync_error` уже принимает `account` → читает `account.auth_type` и передаёт во все
три вызова:

```python
auth_type = account.auth_type
cls = classify(exc, auth_type=auth_type)
prefix = error_prefix(exc, auth_type=auth_type)
...
explicit_permanent = is_explicit_permanent(exc, auth_type=auth_type)
```

Никаких отдельных веток в `sync_cycle` для OAuth не нужно — контекст инкапсулирован в
классификаторе (единый источник истины классификации сохранён). Это **минимальное**
изменение: 3 вызова получают `auth_type=account.auth_type`.

### 3. Retry «login failed» в `imap_fetcher` — gated по XOAUTH2-пути

`_is_retryable_imap_error` получает контекст через сам путь аутентификации: в
`_connect_and_login` retry вызывается, когда выбран `mailbox.xoauth2(...)`
(`access_token is not None`). Решение:

- `_is_retryable_imap_error(exc, *, oauth: bool = False)` — новый keyword-only флаг
  (default `False` = поведение как сейчас).
- Когда `oauth=True`: подстроки `login failed` / `authenticationfailed` **исключаются** из
  `_PERMANENT_IMAP_SUBSTRINGS`-проверки и **добавляются** в retryable-набор. Т.е. для
  XOAUTH2-пути IMAP-`login failed` ретраится `SYNC_CONNECT_RETRIES` раз с backoff
  `0.5/1.0/2.0` (как `authenticated but not connected`).
- Когда `oauth=False` (password-путь): набор `_PERMANENT_IMAP_SUBSTRINGS` неизменен —
  `login failed`/`authenticationfailed` **не** ретраятся (реальный неверный пароль не
  ретраим зря). **Поведение password неизменно.**

Реализация контекста — через два набора подстрок (рекомендуемый вариант):

```python
# Permanent IMAP-маркеры для PASSWORD-пути (как сейчас).
_PERMANENT_IMAP_SUBSTRINGS_PASSWORD = (
    "authenticationfailed", "login failed", "invalid credentials",
    "account is disabled", "account has been blocked",
)
# Для OAUTH-пути login failed / authenticationfailed УБРАНЫ из permanent
# (они flake — ретраим); остаются только реально-permanent маркеры.
_PERMANENT_IMAP_SUBSTRINGS_OAUTH = (
    "invalid credentials", "account is disabled", "account has been blocked",
)
# Retryable transient IMAP-подстроки для OAUTH-пути = базовый набор + flake-auth.
_RETRYABLE_IMAP_SUBSTRINGS_OAUTH = _RETRYABLE_IMAP_SUBSTRINGS + ("login failed", "authenticationfailed")
```

`_connect_and_login` передаёт `oauth=(access_token is not None)` в `_is_retryable_imap_error`.

> **Где взять `access_token` для флага.** `_connect_and_login` уже принимает `access_token`
> параметром — `oauth=(access_token is not None)` вычисляется на месте без новых аргументов
> снаружи.

### 4. НЕ instant-disable OAuth на «login failed» — следствие §1

Поскольку §1 переводит OAuth-`login failed` в **transient**:

- `classify(...) == "transient"` → `_handle_sync_error` идёт по transient-ветке: пишет
  `last_sync_error` через no-bump-метод (`mark_transient_error`), **НЕ** трогает
  `consecutive_failures`, **НЕ** трогает `is_active`. `outcome="transient"`.
- `is_explicit_permanent(...) == False` → фаза 2 `_run_for_accounts` **не** вызывает
  `_disable_after_failures` для этого аккаунта. **Instant-disable исключён by construction.**
- Реальный permanent OAuth (`invalid_grant`) обрабатывается **до** IMAP в
  `_resolve_oauth_access_token` (→ `oauth_needs_consent`, clean skip) — **этим ADR не
  затрагивается** и продолжает работать как раньше.

**Устойчивый OAuth-сбой (не флуктуация, а реально сломавшийся IMAP при валидном refresh).**
Если Microsoft реально перестанет принимать валидный токен по IMAP (редкий, но возможный
сценарий — напр. блок IP воркера), OAuth-аккаунт будет вечно получать transient `login
failed` и **никогда не задисейблится** (transient не дисейблит — осознанный fail-open
ADR-0026). Это **приемлемо и предпочтительно**: лучше держать аккаунт в синке с retry +
suppress, чем ложно выключить рабочий ящик. `last_sync_error` останется виден при затяжном
сбое (suppress снимается при устаревании `last_synced_at`, ADR-0026 §2), поэтому оператор
увидит проблему. Эскалация при затяжном массовом OAuth-сбое — переиспользует механизм
[TD-035](../100-known-tech-debt.md) (breaker-streak / persistent alert), отдельного канала
не требует.

### 5. Авто-recovery деактивированных — НЕ требуется (выбран надёжный вариант)

Из двух предложенных вариантов выбран **«вообще не деактивировать на transient»** (§4), а не
периодический recovery-job. Обоснование:

- При §1+§4 OAuth-аккаунт с флуктуацией **остаётся `is_active=true`** → попадает в каждый
  `list_active()` → первый же успешный цикл вызывает `mark_sync_success`, который сбрасывает
  `consecutive_failures=0, last_sync_error=NULL` (инвариант само-восстановления ADR-0026 §2).
  **Recovery-job становится избыточным by construction** — нечего реактивировать, аккаунт и
  не выключался.
- Отдельный recovery-job (реактивировать `is_active=false` OAuth с `auth_failed` и
  `oauth_needs_consent=false`) был бы хрупким: нужно отличать «выключен системой по
  transient-флуктуации» от «выключен вручную super-admin'ом» — без колонки `disabled_reason`
  (отложена в TD-034) это эвристика по `last_sync_error`, склонная к ложным реактивациям
  вручную-выключенных ящиков. Этот вариант **отклонён** (см. Alternatives).

**Уже-деактивированные на проде аккаунты (25 шт.)** — разовая ручная реактивация (как при
ADR-0026-инциденте): `UPDATE mail_accounts SET is_active=true, consecutive_failures=0,
last_sync_error=NULL WHERE auth_type='oauth_outlook' AND oauth_needs_consent=false AND
is_active=false AND last_sync_error LIKE '%Login failed%'`. **НЕ трогать токены.** Это
операционное действие devops (вне кода), фиксируется в runbook деплоя. После выкатки фикса
повторная массовая деактивация по этой причине невозможна.

### 6. Suppress `last_sync_error` при недавнем успехе — распространяется автоматически

`_should_suppress_transient(account.last_synced_at)` (ADR-0026 §2) применяется к **любому**
transient в `_handle_sync_error`. Поскольку §1 делает OAuth-`login failed` transient, он
**автоматически** попадает под suppress: при свежем `last_synced_at` (в пределах
`SYNC_TRANSIENT_SUPPRESS_MINUTES`) `last_sync_error` не пишется — спорадик не виден в UI.
Отдельной логики не требуется; распространение — следствие §1.

### 7. Config (`shared/config.py`)

Переиспользуются существующие env-параметры (ADR-0026 §5):

- `SYNC_CONNECT_RETRIES` (default 3) — число retry IMAP-`login failed` для OAuth (§3).
- `SYNC_TRANSIENT_SUPPRESS_MINUTES` (default 60) — suppress спорадики в UI (§6).
- `SYNC_MAX_CONSECUTIVE_FAILURES` / `SYNC_MASS_FAILURE_*` — не задействованы для OAuth-флуктуации
  (она transient, счётчик не трогается), но действуют для password как раньше.

**Новый ОБЯЗАТЕЛЬНЫЙ env-параметр — kill-switch:**

```python
SYNC_OAUTH_LOGIN_FAILED_TRANSIENT: bool = Field(default=True)
```

- **Обязательный** (не опциональный): зафиксирован в `docs/07-deployment.md §env` и в
  qa-матрице как точка отката, поэтому backend **обязан** добавить его в `shared/config.py`.
  Default `True` — фикс активен из коробки.
- При `False` правило 7b и oauth-retry **отключаются** → OAuth-`login failed` снова permanent
  (старое поведение); откат без редеплоя кода.
- **Рекомендация по месту чтения флага (чтобы классификатор остался чистой функцией без
  чтения settings):** читать в `sync_cycle._handle_sync_error` (передавать `auth_type=None`,
  если флаг выключен и `auth_type=='oauth_outlook'`) и в `_connect_and_login`
  (`oauth=… and settings.SYNC_OAUTH_LOGIN_FAILED_TRANSIENT`). Конкретную точку выбирает
  backend, но флаг **должен** существовать и управлять обеими ветками (7b + retry).

### 8. Миграция БД

**Не требуется.** Решение целиком в worker-логике + опциональный env-флаг. Существующих
полей `mail_accounts` (`auth_type`, `is_active`, `consecutive_failures`, `last_sync_error`,
`last_synced_at`, `oauth_needs_consent`) достаточно.

---

## Consequences

**Положительные:**
- Спорадический Microsoft `Login failed` на валидном XOAUTH2 больше **не выключает** рабочую
  OAuth-почту: retry сглаживает единичный флак, transient-классификация не дисейблит,
  suppress прячет спорадик из UI.
- Реальный permanent OAuth-отказ (`invalid_grant`) по-прежнему ловится **до** IMAP →
  `oauth_needs_consent` (ADR-0025). Различие «флак vs реальный» — **архитектурно строгое**
  (refresh отработал до IMAP), не эвристика.
- Password-аккаунты **полностью неизменны**: `login failed` = неверный пароль = permanent =
  instant-disable, без retry. Контекст изолирует поведение.
- Recovery-job не нужен (аккаунт не выключается) — меньше движущихся частей.
- Без миграции; обратимо через `SYNC_OAUTH_LOGIN_FAILED_TRANSIENT=false`.

**Отрицательные / компромиссы:**
- Реально сломавшийся OAuth-IMAP при валидном refresh (не флак, а устойчивый отказ) у
  OAuth-аккаунта **никогда не задисейблится** — будет вечно ретраить раз в 5 мин (как и
  любой устойчивый transient, ADR-0026 §Consequences). Приемлемо: письма не теряются,
  `last_sync_error` виден при затяжном сбое; нагрузка ограничена. Эскалация — TD-035.
- `auth_type` теперь протекает в классификатор (был чистой функцией от текста). Контракт
  единого источника классификации сохранён (контекст — явный keyword-параметр, не скрытое
  глобальное состояние), но таблица §1 стала **двухконтекстной**. Задокументировано в обоих
  источниках (ADR + 05-modules §14).
- Малое окно: между «refresh успешен» и «IMAP login» теоретически возможен отзыв токена
  Microsoft в реальном времени — тогда `login failed` будет «реальным», но мы сочтём его
  флаком и поретраим. Вероятность ничтожна (секунды), цена — лишний retry; на следующем
  цикле refresh обновит/инвалидирует токен штатно. Приемлемо.
- **Литеральность flake-набора (важно).** `_OAUTH_IMAP_AUTH_FLAKE_SUBSTRINGS` — это
  **литеральный** набор подстрок (`"login failed"`, `"authenticationfailed"`), а не regex и
  не нормализованный матч. Microsoft **варьирует** формулировки: возможны `[ALERT]`-обёртки,
  `"authentication failure"` (с пробелом, иной корень), иные текстовые варианты. Любая
  формулировка, **не содержащая** ровно одной из этих двух подстрок, **не** перехватится
  правилом 7b и упадёт в permanent rule 8 (→ `auth_failed` + instant-disable у OAuth). Текущие
  2 подстроки покрывают **наблюдённый на проде инцидент**. При появлении иных формулировок на
  проде набор `_OAUTH_IMAP_AUTH_FLAKE_SUBSTRINGS` нужно **расширять** (правка одной константы в
  `worker/app/error_classify.py`; ADR не требуется, если это та же семантика «IMAP-флак на
  валидном OAuth-токене»). Отслеживается через Q-SYNC-3.

---

## Alternatives considered

1. **Отдельная ветка для OAuth в `sync_cycle` (не трогать классификатор).** Отклонено:
   размазало бы классификацию по двум местам (классификатор + sync_cycle), нарушив инвариант
   единого источника ADR-0026 §1. Передача `auth_type` в `classify()` держит всю логику
   классов в одном модуле.

2. **Глобально убрать `login failed`/`authenticationfailed` из permanent (для всех).**
   Отклонено: сломало бы password-аккаунты — неверный пароль перестал бы дисейблиться,
   вечный retry зря (реальный permanent). Контекст `auth_type` обязателен.

3. **Периодический recovery-job для реактивации OAuth, выключенных по transient-auth.**
   Отклонено (см. §5): хрупкое отличие «выключен системой» vs «выключен вручную» без
   `disabled_reason`; §1+§4 (не выключать вовсе) решает задачу надёжнее и без нового job'а /
   колонки. `mark_sync_success`-инвариант само-восстанавливает аккаунт.

4. **Ретраить `login failed` для всех (и password).** Отклонено: для password это реальный
   неверный пароль — retry только задержит цикл и не поможет. OAuth-gated retry (§3) точечнее.

5. **Различать флак vs реальный по числу повторов (N `login failed` подряд → permanent).**
   Отклонено: вводит порог/состояние, а у нас **есть точный сигнал** (refresh отработал до
   IMAP ⇒ флак). Порог был бы менее надёжным суррогатом строгого инварианта.

6. **Доверять только circuit-breaker'у (ADR-0026 §3).** Отклонено эмпирически: флаки приходят
   **не синхронно**, per-cycle `ratio` не достигает порога — брейкер не срабатывает, аккаунты
   гаснут по одному. Нужна именно контекстная классификация, а не агрегатный брейкер.

---

## Open questions

- **Q-SYNC-1 (closed by this ADR):** трактовать ли OAuth-`login failed` как transient? —
  **Да** (refresh-инвариант доказывает флуктуацию).
- **Q-SYNC-2 (open, low):** считать ли `account is disabled` / `account has been blocked` у
  OAuth-аккаунта тоже флуктуацией? Сейчас — **нет** (остаются permanent, §1). Пересмотреть,
  только если прод покажет, что Microsoft спорадически отдаёт и их на валидном токене. Не
  блокирует фикс.
- **Q-SYNC-3 (open, low):** полнота `_OAUTH_IMAP_AUTH_FLAKE_SUBSTRINGS`. Набор литеральный
  (`"login failed"`, `"authenticationfailed"`) и покрывает наблюдённый инцидент. Microsoft
  может отдавать иные формулировки (`[ALERT]`-обёртки, `"authentication failure"` с пробелом и
  т.п.), которые **не** перехватятся 7b и упадут в permanent. Действие: при появлении на проде
  новых вариантов IMAP-флака на валидном OAuth-токене — расширить константу. Не блокирует фикс.

---

## Контракт для backend (сводка точек изменения)

- `worker/app/error_classify.py`:
  - новый набор `_OAUTH_IMAP_AUTH_FLAKE_SUBSTRINGS = ("login failed", "authenticationfailed")`;
  - `classify`, `error_prefix`, `is_explicit_permanent`, `_matches_transient`,
    `_matches_permanent` получают keyword `auth_type: str | None = None`;
  - правило 7b: в `_matches_transient` — если `auth_type=="oauth_outlook"` И есть
    flake-подстрока И НЕТ `"invalid_grant"` → True; в `error_prefix` — соответствующая ветка
    возвращает `"network"`, размещённая ПЕРЕД rule 8;
  - default `auth_type=None` сохраняет текущее поведение всех существующих вызовов/тестов.
- `worker/app/sync_cycle.py`: `_handle_sync_error` передаёт `auth_type=account.auth_type` в
  `classify` / `error_prefix` / `is_explicit_permanent`. Kill-switch (обязательный):
  подменять `auth_type` на `None`, если `SYNC_OAUTH_LOGIN_FAILED_TRANSIENT=false`. Больше
  ничего в фазах 1/2 менять не нужно — instant-disable исключается автоматически
  (transient + `explicit_permanent=False`).
- `worker/app/imap_fetcher.py`:
  - `_PERMANENT_IMAP_SUBSTRINGS` → раздвоить на `_PERMANENT_IMAP_SUBSTRINGS_PASSWORD`
    (как сейчас) и `_PERMANENT_IMAP_SUBSTRINGS_OAUTH` (без `login failed`/`authenticationfailed`);
  - `_RETRYABLE_IMAP_SUBSTRINGS_OAUTH = _RETRYABLE_IMAP_SUBSTRINGS + ("login failed", "authenticationfailed")`;
  - `_is_retryable_imap_error(exc, *, oauth: bool = False)` выбирает наборы по `oauth`;
  - `_connect_and_login` передаёт `oauth=(access_token is not None)` (опц.
    `and settings.SYNC_OAUTH_LOGIN_FAILED_TRANSIENT`).
- `shared/config.py`: **обязательно** добавить `SYNC_OAUTH_LOGIN_FAILED_TRANSIENT: bool =
  Field(default=True)` (kill-switch; default-on). Зафиксирован в `07-deployment §env` и
  qa-матрице — рассогласование недопустимо. Иных новых полей нет.
- Без миграции.
- Обновить `docs/05-modules.md` §14 (таблица + §14 «Обработка ошибок»: добавить rule 7b и
  OAuth-retry); `docs/100-known-tech-debt.md` (TD-035 переиспользуется; новых TD нет, кроме
  явной отметки про устойчивый OAuth-сбой).
