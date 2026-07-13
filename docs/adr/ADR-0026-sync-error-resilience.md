# ADR-0026: Отказоустойчивость синхронизации почт (transient vs permanent + circuit-breaker)

- **Статус:** accepted
- **Дата:** 2026-05-28
- **Связь:** extends [ADR-0008](./ADR-0008-sync-strategy.md) (стратегия синхронизации), уточняет error-handling из [ADR-0013](./ADR-0013-concurrency-model.md). НЕ supersede.
- **Уточнён** [ADR-0028](./ADR-0028-oauth-login-failed-transient.md) (2026-06-10): для `auth_type='oauth_outlook'` IMAP-`login failed`/`authenticationfailed` = **transient** (rule 7b, контекст по `auth_type`), не permanent — refresh подтверждает токен ДО IMAP. Таблица §1 ниже остаётся в силе для password-аккаунтов; OAuth-ветка — в ADR-0028 §1.
- **Амендирован** [ADR-0046](./ADR-0046-mailbox-status-hook-points.md) (2026-07-13): `last_synced_at` = «время последнего **успешного** sync» **нормативно и без исключений** — запись `last_synced_at=now()` в PERMANENT-ветке (§2 «PERMANENT» и §«Текущая реализация» ниже) **отменена**. Остальные положения ADR-0026 (классификация, счётчик, circuit-breaker, подавление спорадики) — в силе.

---

## Context

### Прод-инцидент (подтверждён диагностикой)

Сервер/сеть были недоступны ~30 минут. За это время worker не смог зарезолвить IMAP-хосты
**у всех** почтовых аккаунтов. Последствия:

1. `assert_public_host(...)` (SSRF-guard, см. `06-security.md` sec. 4) вызывает `socket.getaddrinfo`.
   При недоступном DNS он бросает `InvalidHostError("Could not resolve host")`.
2. Worker трактовал это как ошибку аккаунта → `_record_failure(..., disable=True)` → **мгновенный**
   `is_active=false` (для invalid_host disable срабатывал сразу, даже не дожидаясь порога 3).
3. У части аккаунтов параллельно приходила IMAP-ошибка `Too many simultaneous connections`,
   которую сервер отдаёт как `LOGIN NO`. Текущая эвристика (`"LOGIN" in type(exc).__name__.upper()`)
   помечала её как auth-fail → тоже мгновенный disable.
4. Итог: **81 из 85** `mail_accounts` получили `is_active=false`.

Связь восстановилась (хосты резолвятся, провайдер принимает соединения), но отключённые аккаунты
**сами не возобновлялись** — `sync_cycle` читает только `WHERE is_active=true`, а выключенные туда
не попадают. Потребовалась ручная реактивация
(`UPDATE mail_accounts SET is_active=true, consecutive_failures=0, last_sync_error=NULL`),
после которой **84 из 85** восстановились мгновенно (один — реально протухшие настройки).

Кратковременный инфраструктурный сбой необратимо вывел из строя весь сбор почты. Это баг
отказоустойчивости, а не ожидаемое поведение.

### Два корня

1. **Корень A — отсутствие различия transient/permanent.** Worker не отличает временные сбои
   (вина сети / сервера / провайдера: DNS, таймаут, connection reset, «слишком много соединений»)
   от постоянных (вина настроек: неверный пароль, заблокированный аккаунт). Любая ошибка ведёт к
   инкременту счётчика и в итоге к авто-disable. Но временный сбой пройдёт сам — отключать аккаунт
   из-за него нельзя.

2. **Корень B — классификация по UI-префиксу, а не по сути.** `Too many simultaneous connections`
   приходит как `LOGIN NO` и потому выглядит как auth-fail, хотя это **transient** rate-limit
   провайдера. Привязка «класса ошибки» к тексту/типу логин-ответа делает временный сбой
   «постоянным» и приводит к ложному disable.

### Текущая реализация (на момент ADR)

- `worker/app/sync_cycle.py`: классификация ошибок — **инлайн** внутри `sync_one_account`
  (отдельной функции `_classify_error` нет). Константы `_AUTH_FAIL_PREFIX="auth_failed"`,
  `_DISABLE_AFTER_FAILS=3` захардкожены в модуле (в `shared/config.py` порога нет).
- `_record_failure(account_id, *, error, disable) -> int`, `_disable_after_failures(...)`.
- `MailAccountsRepo.mark_sync_failure(account_id, *, error, disable) -> int` — bump
  `consecutive_failures`, пишет `last_sync_error`, `last_synced_at=now()`, опц. `is_active=false`.
  > **Амендмент [ADR-0046](./ADR-0046-mailbox-status-hook-points.md) §1:** запись `last_synced_at=now()`
  > на PERMANENT-ошибке **отменена** — поле нормативно значит «время последней **успешной**
  > синхронизации» (как и объявлено в §2 ниже), ошибочные ветки его не трогают. До выкатки фикса
  > (`backend/app/repositories/mail_accounts.py:505`) код ещё пишет его на сбое — ведётся как `TD-053`.
- `MailAccountsRepo.mark_sync_success(...)` — **уже** сбрасывает `consecutive_failures=0` и
  `last_sync_error=NULL` (инвариант само-восстановления на стороне success уже есть, требуется лишь
  чтобы аккаунт не был отключён до этого success).
- `imap_fetcher.fetch_blocking(...)` — sync, под `asyncio.to_thread`; коннект/резолв/логин внутри.

---

## Decision

### 1. Классификация: TRANSIENT vs PERMANENT

Вводим **единый модуль классификации** `worker/app/error_classify.py` с одной таблицей подстрок
(lower-case), которая обслуживает обе функции:

```python
def classify(exc_or_text) -> Literal["transient", "permanent"]
def error_prefix(exc_or_text) -> str   # UI-текст: "invalid_host" | "auth_failed" | "timeout" | "network" | "error"
```

**Инвариант единого источника:** `classify()` и `error_prefix()` используют **одну и ту же**
таблицу подстрок. UI-префикс (что показываем пользователю) и класс (что делаем со счётчиком)
вычисляются из общего набора правил → они **никогда не расходятся**. Это закрывает корень B на
уровне дизайна: даже если UI-префикс получился `auth_failed`, класс может быть `transient`.

#### Таблица классификации и приоритетов (контракт для backend — реализовать 1-в-1)

> **Single source of truth для классификации — ЭТА таблица (ADR-0026 §1).** `05-modules.md` §14
> ("Обработка ошибок (per-account)") содержит **побитово идентичную** копию (те же подстроки, типы,
> порядок, классы, UI-префиксы) и явно ссылается сюда. При любом изменении правил правятся **оба**
> документа в одном коммите; расхождение между ними — баг документации.

Сопоставление выполняется по нормализованному (lower-case) тексту:
`text = f"{type(exc).__name__}: {exc}".lower()` плюс проверка `isinstance(exc, …)` для точных типов.

Порядок проверки строго сверху вниз — **первое совпадение выигрывает**. Transient-блок (правила
**1–7**) проверяется **целиком до** permanent-блока (правила **8–9**) — это намеренно (корень B):
любой transient-маркер выигрывает у любого auth/permanent-маркера в том же тексте.

| Приоритет | Условие (тип или подстрока в lower-case тексте) | Класс | UI-префикс |
| --- | --- | --- | --- |
| 1 | `isinstance(exc, (socket.timeout, TimeoutError, asyncio.TimeoutError))` | transient | `timeout` |
| 2 | подстрока `could not resolve` / `name or service not known` / `temporary failure in name resolution` / `nodename nor servname` ; либо `isinstance(exc, socket.gaierror)` | transient | `invalid_host` |
| 3 | подстрока `too many` / `simultaneous` / `try again` / `temporarily` / `unavailable` / `inuse` / `system error` / `rate` / `throttl` | transient | `auth_failed` если текст также содержит auth-маркер, иначе `network` |
| 3b | подстрока `authenticated but not connected` / `not connected` (спорадик IMAP, ADR-0026 update; Microsoft personal Outlook `User is authenticated but not connected` на валидный XOAUTH2 при работающем ящике — серверная флуктуация, не auth-fail). Проверяется ДО permanent-блока; содержит `authenticated`, но НЕ `authenticationfailed`, поэтому никогда не матчит rule 8. | transient | `network` |
| 4 | подстрока `timed out` / `timeout` | transient | `timeout` |
| 5 | `isinstance(exc, (ConnectionError, ssl.SSLError))` ; либо подстрока `connection refused` / `connection reset` / `broken pipe` / `network is unreachable` / `no route to host` / `ssl` | transient | `network` |
| 6 | `isinstance(exc, OSError)` с сетевым `errno` (`ECONNREFUSED, ECONNRESET, ETIMEDOUT, EHOSTUNREACH, ENETUNREACH, EPIPE`) | transient | `network` |
| 7 | OAuth: httpx `5xx` / `429` / network-исключение httpx (см. `oauth.service.OAuthError` с соответствующим `code`). Worker оборачивает в `oauth_token_error: <code>`; подстроки rule 7: `5xx` / `429` / `token_network` / `network` / `timeout` / `unexpected` / `oauth_exchange_failed`. `oauth_exchange_failed` — это `OAuthError.code` для ЛЮБОГО non-200 от Microsoft, кроме `invalid_grant` (реальный провайдерский 5xx/429/non-JSON на refresh-пути). `invalid_grant` сюда НЕ попадает (поднимается как `OAuthRefreshInvalidError` → clean skip; если текст всё же встретится — ловится rule 8, permanent) — поэтому голый маркер-префикс `oauth_token_error` в подстроки rule 7 НЕ добавляется. | transient | `oauth_token_error` |
| 7b | **(ADR-0028)** `auth_type=='oauth_outlook'` И подстрока `login failed` / `authenticationfailed` И НЕ `invalid_grant` — спорадик Microsoft на валидном XOAUTH2 (refresh подтверждён ДО IMAP). Только для OAuth-контекста; для password/None не активируется. | transient | `network` |
| 8 | подстрока `authenticationfailed` / `invalid credentials` / `login failed` / `[alert]` / `account is disabled` / `account has been blocked` ; либо oauth `invalid_grant`. **Для `oauth_outlook` `login failed`/`authenticationfailed` перехватываются rule 7b (ADR-0028) — сюда у OAuth доходят только `invalid_grant` / `invalid credentials` / disabled / blocked.** | **permanent** | `auth_failed` |
| 9 | decrypt-fail (`InvalidTag`, `AssertionError` при `decrypt_mail_password`) | **permanent** | `decrypt_fail` |
| 10 | всё остальное (нераспознанное — **в т.ч. программные исключения** `TypeError`/`KeyError`/`AttributeError`/`ValueError`, не входящие в network/IMAP/OAuth-наборы выше) | **transient** (fail-open) | `error` |

**Ключевой кейс корня B:** `"auth_failed: too many simultaneous connections"` — совпадает по
приоритету 3 (`too many`) → класс **transient**, хотя UI-префикс остаётся `auth_failed`.
Permanent-блок (правила 8–9) проверяется **только если ни одно transient-правило 1–7 не
сработало**.

**Логирование по приоритету 10 (MINOR-2 — fail-open не должен прятать наши баги):** программные
исключения, попавшие в правило 10 (т.е. **не** распознанные ни одним из правил 1–9), логируются на
уровне **ERROR с traceback** (`log.error("sync_account_unexpected_error", exc_info=True, …)`), а не
INFO/WARNING. Класс остаётся `transient` (аккаунт не дисейблим из-за нашего бага), но ERROR-лог
обязателен — это сигнал для алертинга, что классификатор/код встретил неожиданный путь. Сетевые/IMAP
ошибки (правила 1–9) логируются WARNING как сейчас.

#### Правило fail-open (приоритет 10) — обоснование

Нераспознанная ошибка считается **transient**. Асимметрия цены ошибки классификатора:

- Ложный **permanent** → аккаунт зря отключается навсегда (до ручного вмешательства) — это и есть
  баг инцидента. **Дорого.**
- Ложный **transient** → аккаунт повторит попытку в следующем цикле и запишет `last_sync_error`
  (видно в UI). Максимум — лишняя нагрузка раз в `SYNC_INTERVAL_MINUTES`. **Дёшево.**

Поэтому при неоднозначности выбираем доступность (fail-open), а не «защиту» отключением.

### 2. Поведение по классам

**TRANSIENT:**
- Записать `last_sync_error` (через новый repo-метод, без bump) — пользователь видит проблему,
  **с учётом подавления спорадики (см. ниже)**.
- **НЕ** инкрементить `consecutive_failures`. **НЕ** менять `is_active`.
- **НЕ** обновлять `last_synced_at` (оно семантически = «время последнего *успешного* sync»,
  см. `03-data-model.md`; transient оставляет его как «последний успех» для корректного отображения
  в UI). Зафиксировано как решение. **[ADR-0046](./ADR-0046-mailbox-status-hook-points.md) §1
  распространяет это правило на ВСЕ ошибочные ветки** (включая PERMANENT и needs-consent): единственный
  писатель `last_synced_at` — `mark_sync_success`.
- Следующий цикл повторит. Когда сбой пройдёт — `mark_sync_success` сам обнулит `last_sync_error`.

**Подавление спорадического transient в UI (update, дата 2026-06-01).** Контекст: Microsoft personal
Outlook IMAP спорадически (≈2 из 9 циклов, не подряд) отдаёт `imaplib.IMAP4.error: User is
authenticated but not connected` на валидный XOAUTH2 при **работающем** ящике — это серверная
флуктуация Microsoft, не наша ошибка. Единичный спорадический сбой долетал в `last_sync_error` и
пугал пользователя при исправном ящике.

Решение: при transient-классе `last_sync_error` пишется **только если** последний **успешный** sync
(`last_synced_at`) был раньше окна `SYNC_TRANSIENT_SUPPRESS_MINUTES` (новый config, default 60 мин)
**или** `last_synced_at IS NULL`. Если последний успех **свежий** (в пределах окна) — transient-ошибка
**подавляется** (НЕ пишется в `last_sync_error`): спорадик не виден в UI, аккаунт продолжает, следующий
цикл повторит/успешен. Логика — `worker/app/sync_cycle.py::_should_suppress_transient(last_synced_at)`.

- `consecutive_failures` для transient **не трогается** при подавлении (как и без него) — инвариант §2
  сохранён.
- `last_synced_at` остаётся семантикой «последний успех»; подавление **читает** его, но не меняет.
- `SYNC_TRANSIENT_SUPPRESS_MINUTES=0` отключает подавление (каждый transient пишется — поведение до
  update).
- Подавление логируется в `sync_account_transient` полем `last_sync_error_suppressed=true|false` —
  наблюдаемость спорадики в structured-логах сохраняется даже когда UI её не показывает.
- **Защита от «застрявшего» синка:** если ящик реально перестал синкаться, `last_synced_at` устаревает
  (>окна) → подавление выключается → `last_sync_error` снова виден в UI. Подавляется только спорадик
  на фоне свежего успеха, не затяжной сбой.

**Инвариант полноты выборки (нет starvation планировщика, MAJOR-1):** тот факт, что transient **не
двигает** `last_synced_at`, **безопасен**, потому что `list_active()` **не лимитирует** выборку. По
факту кода (`backend/app/repositories/mail_accounts.py::list_active`):

```python
select(MailAccount).where(is_active.is_(True))
    .order_by(last_synced_at.asc().nulls_first(), id)   # БЕЗ LIMIT
```

`sync_cycle` грузит **всех** активных аккаунтов и `_run_for_accounts` прогоняет **весь** список под
`asyncio.Semaphore(MAX_CONCURRENT_IMAP)` через `asyncio.gather`. `ORDER BY last_synced_at NULLS
FIRST` влияет **только на порядок обработки внутри цикла** (кто стартует первым при насыщенном
семафоре), а **не на состав** обрабатываемых аккаунтов. Поэтому аккаунт с устойчивой
transient-ошибкой (вечно недоступный self-hosted IMAP, вечный «too many connections») со «старым»
или `NULL` `last_synced_at` будет стабильно стоять в **голове** очереди каждый цикл, но это **не
вытесняет** здоровые ящики — они всё равно синхронизируются в том же цикле (просто позже по
порядку). Голодание невозможно **by construction**.

- **Решение по starvation:** отдельное поле `last_attempt_at` и миграция **НЕ требуются**. Семантика
  `last_synced_at` = «последний успех» сохраняется (нужна UI и `03-data-model.md`); полнота выборки
  обеспечена отсутствием LIMIT. Если в будущем `list_active()` станет LIMIT-ить выборку (top-N за
  цикл при росте N аккаунтов) — это станет **breaking change** для данного инварианта и потребует
  отдельного ADR с введением `last_attempt_at` (обновляемого на ЛЮБУЮ попытку, включая transient) и
  миграции. На текущем масштабе (≤500 аккаунтов, см. §3 ADR-0008) выборка без LIMIT приемлема.

**PERMANENT:**
- Инкремент `consecutive_failures` + запись `last_sync_error`.
  ~~+ `last_synced_at=now()`~~ — **отменено [ADR-0046](./ADR-0046-mailbox-status-hook-points.md) §1**:
  `last_synced_at` = «время последнего **успешного** sync», ошибочные ветки его не пишут (иначе окно
  `SYNC_TRANSIENT_SUPPRESS_MINUTES` мерит давность permanent-попытки, а не успеха, и сбойный ящик
  выглядит «свежесинканным»). До выкатки фикса `mark_sync_failure` — `TD-053`.
- Авто-disable при `consecutive_failures >= SYNC_MAX_CONSECUTIVE_FAILURES` (новый config, default 3),
  **с учётом circuit-breaker (см. §3)**.
- Явные `auth_failed` (приоритет 8) и `decrypt_fail` (приоритет 9) — это достоверно настройки/данные.
  Для них допустим **мгновенный disable** (как сейчас, `disable=True` без ожидания порога), но
  **только** через тот же двухфазный путь §3 (подпадают под circuit-breaker — массовый «протух
  пароль у всех разом» невозможен, значит при массовости это всё равно общий сбой). То есть
  «мгновенный» = «порог не нужен», но disable всё равно подавляется брейкером.
- **Гарантия:** `too many simultaneous connections` (transient по §1) **никогда** не попадает в
  permanent-путь и не может вызвать ни bump, ни disable.

**RECOVERY (инвариант само-восстановления):**
`mark_sync_success` уже выполняет `consecutive_failures=0, last_sync_error=NULL, last_synced_at=now()`.
Так как transient **не отключает** аккаунт, аккаунт остаётся `is_active=true` → попадает в следующий
`list_active()` → при восстановлении сети первый же успешный цикл сам обнуляет состояние.
**Это и закрывает корень «сами не возобновлялись»** — без отдельного «re-enable» job'а.

### 3. Circuit-breaker (защита от массового disable)

Если в **одном цикле** доля permanent-падений велика — это почти наверняка общий сбой
(а не «у всех 81 ящика одновременно протух пароль»). В этом случае подавляем disable.

**Условие срабатывания (на цикл):**
`total >= SYNC_MASS_FAILURE_MIN` **И** `permanent_failures / total >= SYNC_MASS_FAILURE_RATIO`,
где `total` — число аккаунтов, обработанных в этом запуске `_run_for_accounts`.

**Что подавляется при срабатывании:** И инкремент `consecutive_failures`, И `is_active=false`.
Подавлять только disable недостаточно — иначе на следующем цикле все разом перешагнут порог и
disable лишь отложится на один цикл. Поэтому при срабатывании брейкера permanent-аккаунты в этом
цикле **вообще не трогают счётчик** (только `last_sync_error` пишется — он безопасен и информативен).

**Механизм — двухфазный `_run_for_accounts` (контракт реализации):**

`sync_one_account` больше **не применяет** bump/disable для permanent-ошибок прямо в момент ошибки.
Вместо этого он **возвращает классификацию** результата:

```
AccountSyncOutcome = Literal["ok", "transient", "permanent"]
sync_one_account(...) -> tuple[int, int, AccountSyncOutcome]   # (new, conflicts, outcome)
```

- **Фаза 0 (внутри `sync_one_account`, немедленно):** на любой ошибке вычисляется
  `cls = classify(exc)` и `prefix = error_prefix(exc)`; **сразу** пишется `last_sync_error`
  (transient — через no-bump метод; permanent — `last_sync_error` тоже пишется, но bump/disable
  откладывается). На success — обычный `mark_sync_success`. Возвращается `outcome`.
- **Фаза 1 (в `_run_for_accounts`, после `asyncio.gather`):** собрать `outcome` всех аккаунтов,
  посчитать `total` и `permanent_failures`. Вычислить `breaker_tripped` по условию выше.
- **Фаза 2 (в `_run_for_accounts`):** если `breaker_tripped` — **ничего** не делать со счётчиками
  (лог `sync_breaker_tripped` с `total`/`permanent`/`ratio` + audit-событие `sync_mass_failure_suppressed`).
  Иначе для каждого permanent-аккаунта: `mark_sync_failure(..., disable=False)` (bump);
  если вернувшийся `consecutive_failures >= SYNC_MAX_CONSECUTIVE_FAILURES` **или** это явный
  auth/decrypt (мгновенный) → `_disable_after_failures(...)` (disable + audit `account_auto_disabled`).

`last_sync_error` для permanent пишется в фазе 0 (сразу), а bump/disable — в фазе 2 (после
решения брейкера). Это разделяет «информирование» (безопасно всегда) и «наказание» (под контролем
брейкера).

**Наблюдаемость подавлённых permanent (MAJOR-3):** при сработавшем брейкере реально протухшие
пароли (permanent) **не дисейблятся** в этом цикле — но оператор обязан их видеть. Поэтому:

- `last_sync_error` пишется для **ВСЕХ** ошибок, **включая подавлённые permanent** — это происходит
  в фазе 0 (до решения брейкера), значит подавление disable никогда не «прячет» причину: в UI у
  каждого аккаунта виден его конкретный `last_sync_error` (например `auth_failed: …`), даже если
  брейкер сработал и аккаунт остался `Active`.
- Audit-событие `sync_mass_failure_suppressed` (severity **warning**) пишется **один раз на цикл**
  при срабатывании брейкера, с `details = {total, permanent_failures, transient_failures, ratio,
  threshold_ratio, threshold_min}` — оператор видит масштаб и причину подавления. `actor_user_id` —
  системный super-admin (как в `_disable_after_failures`).
- Лог-событие `sync_breaker_tripped` (уровень WARNING) дублирует те же counts в structured-логи для
  алертинга.

**Эскалация при затяжном массовом permanent (escape-hatch) — отложено в TD-035.** Сценарий: брейкер
срабатывает N циклов подряд (вечный массовый permanent). Это может означать не «общий инфра-сбой», а
что провайдер заблокировал IP воркера / массово отозвал токены — тогда аккаунты вечно долбят
провайдера, а disable вечно подавляется. Нужна эскалация: при `>= SYNC_BREAKER_CONSECUTIVE_ALERT`
(будущий config, предлагаемый default 6 ≈ 30 мин при 5-мин цикле) подряд сработавших брейкер-циклах —
повышенный audit/alert `sync_breaker_persistent` (severity error), чтобы оператор вмешался вручную.
Требует хранения счётчика подряд-срабатываний (Redis-ключ `sync:breaker_streak`, без миграции).
Отложено в [TD-035](../100-known-tech-debt.md) — не блокирует текущий фикс (он уже устраняет
исходный инцидент), но фиксируется как известный пробел наблюдаемости.

**Edge-cases:**
- `total < SYNC_MASS_FAILURE_MIN` (по умолчанию <5) → брейкер выключен, обычная логика порога.
  Это покрывает `force_sync_dispatch` (обычно 1 аккаунт): тот же двухфазный механизм, но `total=1<5`
  → брейкер не мешает, единичный force ведёт себя как раньше.
- Ровно `ratio == SYNC_MASS_FAILURE_RATIO` → срабатывает (условие `>=`).
- Все permanent при `total>=MIN` → `ratio=1.0 >= 0.5` → брейкер срабатывает (именно сценарий инцидента).
- Смешанный цикл (часть transient, часть permanent): `ratio` считается от `total` (всех
  обработанных), transient в числитель не входят. Реальный «у одного протух пароль» при 85 живых:
  `1/85 ≈ 0.012 < 0.5` → брейкер не мешает, аккаунт нормально дисейблится по порогу.

### 4. DNS / connect retry в `imap_fetcher`

В `fetch_blocking` оборачиваем **открытие соединения + login/xoauth2** в retry:
`SYNC_CONNECT_RETRIES` (default **3**, поднят с 2 в update) повторов с backoff `0.5s`, `1.0s`, `2.0s`
на:
- **мгновенные** ошибки резолва/коннекта: `socket.gaierror` / `ConnectionError` (incl.
  `ConnectionRefusedError`) / сетевых `OSError` (`ECONNREFUSED`/`EHOSTUNREACH`/`ENETUNREACH`) —
  helper `_is_retryable_connect_error`; и
- **спорадические transient IMAP-ошибки** (update): `imaplib.IMAP4.error` / `imaplib.IMAP4.abort`,
  текст которых содержит `authenticated but not connected` / `not connected` / `try again` /
  `temporarily` / `too many` — helper `_is_retryable_imap_error`. Канонический кейс — Microsoft personal
  Outlook IMAP `User is authenticated but not connected` на валидный XOAUTH2 при работающем ящике
  (серверная флуктуация, 2-3я попытка проходит). Оба helper'а объединены через `or` в retry-условии
  `_connect_and_login`; между попытками — best-effort `logout()`.

**НЕ ретраим реальные auth-фейлы даже когда они приходят как `IMAP4.error`:** `_is_retryable_imap_error`
сначала исключает permanent-маркеры (`authenticationfailed` / `login failed` / `invalid credentials` /
`account is disabled` / `account has been blocked`) и только потом проверяет transient-подстроки. Так
`AUTHENTICATIONFAILED` никогда не матчит широкое `not connected` и propagate'ится как permanent.

> **Уточнение [ADR-0028](./ADR-0028-oauth-login-failed-transient.md):** для **XOAUTH2-пути**
> (`access_token is not None`, `auth_type='oauth_outlook'`) `login failed` / `authenticationfailed`
> **ретраятся** (`_is_retryable_imap_error(exc, oauth=True)` исключает их из permanent-набора и
> добавляет в retryable) — это спорадик Microsoft на валидном токене, не реальный auth-fail. Для
> **password-пути** поведение этого абзаца неизменно (login failed = неверный пароль = не ретраим).

Единичный DNS-глюк / спорадик не должен становиться ошибкой вообще.

- **НЕ ретраим `socket.timeout` / `TimeoutError` (MINOR-1).** Таймаут — это уже истёкшее время
  ожидания (≈`IMAP_TIMEOUT_SECONDS`); ретрай умножил бы его (`(retries+1) × timeout`), растянув цикл
  в разы и съев бюджет `SYNC_INTERVAL_MINUTES`. Таймаут проходит обычным путём (transient по правилу
  1/4, повторится в следующем цикле через 5 мин), но **внутри одного цикла не ретраится**.
- **НЕ ретраим** auth-fail (приоритет 8) и любые permanent — повтор бессмысленен и лишь задержит цикл.
- Бюджет времени приемлем: при `MAX_CONCURRENT_IMAP=10` лишние ≤1.5s на падающий аккаунт (только
  быстрые connect/DNS-ошибки, не таймауты) не ломают цикл (порядки величин ниже
  `SYNC_INTERVAL_MINUTES=5`).
- **Защита от наложения циклов:** APScheduler-job `sync_cycle` настроен `max_instances=1,
  coalesce=True` (как `force_sync_dispatch` и остальные worker-jobs, см. `worker/app/main.py` /
  `05-modules.md` §14). Если retry-окна растянут цикл дольше `SYNC_INTERVAL_MINUTES`, следующий тик
  **не запустится параллельно** (coalesce схлопнёт пропущенные тики в один). Если фактическая
  конфигурация job'а `sync_cycle` окажется без `max_instances=1` — это [TD-036](../100-known-tech-debt.md).
- Retry в `imap_fetcher` — это **первая** линия (сгладить единичный глюк). Классификация + transient
  no-disable в `sync_cycle` — **вторая** линия (если сбой длится дольше retry-окна). Circuit-breaker —
  **третья** (массовый сбой). Три независимых уровня защиты.

### 5. Config (`shared/config.py`)

Добавить (с `Field`-валидацией), заменив хардкод `_DISABLE_AFTER_FAILS`:

| Имя | Тип | Default | Границы |
| --- | --- | --- | --- |
| `SYNC_MAX_CONSECUTIVE_FAILURES` | `int` | `3` | `ge=1, le=20` |
| `SYNC_MASS_FAILURE_RATIO` | `float` | `0.5` | `ge=0.0, le=1.0` |
| `SYNC_MASS_FAILURE_MIN` | `int` | `5` | `ge=1, le=10000` |
| `SYNC_CONNECT_RETRIES` | `int` | `3` *(update: было 2)* | `ge=0, le=10` |
| `SYNC_TRANSIENT_SUPPRESS_MINUTES` | `int` | `60` *(update)* | `ge=0, le=10080` |

`SYNC_CONNECT_RETRIES=0` отключает retry (поднят с 2 до 3 в update — больше попыток для спорадической
Microsoft-флуктуации, backoff `0.5/1.0/2.0`); `SYNC_TRANSIENT_SUPPRESS_MINUTES=0` отключает подавление
спорадического transient в UI (см. §2). `SYNC_MASS_FAILURE_RATIO` ничего сверх границ валидировать не
требует. Документируются в `07-deployment.md` sec. 4.

### 6. Миграция БД

**Не требуется.** Существующих полей `mail_accounts` (`is_active`, `consecutive_failures`,
`last_sync_error`, `last_synced_at`) достаточно:
- transient просто **не трогает** `consecutive_failures` → отдельный `transient_failures` не нужен;
- причина disable уже логируется в audit `account_auto_disabled.details.reason` → отдельная колонка
  `disabled_reason` для MVP избыточна.

Дополнительная наблюдаемость (`transient_failures`, `disabled_reason`) отложена в tech-debt
(см. TD-034) — её добавление потребовало бы миграции без функциональной необходимости сейчас.
Escape-hatch при затяжном массовом permanent (TD-035) и подтверждение `max_instances=1` для
`sync_cycle` (TD-036) тоже не требуют миграции (Redis-ключ / конфиг job'а).

---

## Consequences

**Положительные:**
- Кратковременный инфраструктурный сбой (DNS/сеть/«too many connections») больше не отключает
  аккаунты — они само-восстанавливаются следующим успешным циклом.
- Корень B устранён архитектурно: класс ≠ UI-префикс, единая таблица гарантирует согласованность.
- Circuit-breaker не даёт «эффекту домино» отключить всё разом даже при длительном общем сбое.
- Три независимых уровня защиты (retry → transient-no-disable → breaker).
- Без миграции — деплой = выкатка образа + новые env (с дефолтами безопасно работает и без них).

**Отрицательные / компромиссы:**
- Реально протухший пароль при общем сбое (брейкер сработал) **не** будет задисейблен в этом цикле —
  отложится до цикла, где сбой локализован. Приемлемо: лучше задержать disable, чем массово
  отключить живые ящики.
- Постоянный transient-сбой у одного аккаунта (например, перманентно недоступный self-hosted IMAP)
  никогда не задисейблится — будет вечно долбить раз в 5 минут и писать `last_sync_error`. Это
  осознанный выбор fail-open; нагрузка ограничена (1 коннект / 5 мин / аккаунт). При необходимости —
  пер-провайдерный throttling (TD-032).
- Двухфазный `_run_for_accounts` усложняет цикл (нужно собрать outcomes до применения disable).
  Оправдано: без этого circuit-breaker не реализуем корректно (нужно знать долю до решения).

---

## Alternatives considered

1. **Отдельный «re-enable» job** (раз в N минут включать обратно аккаунты с transient-ошибкой).
   Отклонено: усложняет (нужно отличать «отключён системой по сбою» от «отключён вручную/по auth»),
   требует `disabled_reason`-колонку. `mark_sync_success`-инвариант + «transient не отключает» решают
   ту же задачу проще и без новой колонки.

2. **Колонка `disabled_reason` + `transient_failures`** для точной наблюдаемости.
   Отклонено для MVP: требует миграции, функционально не нужно (transient не инкрементит счётчик;
   причина disable есть в audit). Отложено в TD-034.

3. **Экспоненциальный per-account backoff** (увеличивать интервал для падающих аккаунтов).
   Отклонено: усложняет планировщик, требует хранить `next_attempt_at`. На текущем масштабе
   (≤500 аккаунтов, 5-мин цикл) фиксированный интервал + transient-no-disable достаточны.

4. **Привязка класса к UI-префиксу** (как было: auth-fail → permanent).
   Отклонено — это и есть корень B инцидента.

5. **Per-provider throttling / INUSE-aware concurrency** (ограничивать число соединений к одному
   провайдеру, чтобы не ловить «too many simultaneous connections»).
   Отложено в TD-032: лечит причину части transient, но это отдельная фича с состоянием per-host;
   текущий фикс (классификация «too many» как transient + retry) устраняет вредное последствие.

6. **DNS-фолбэк/кэш на уровне worker-контейнера** (resolver в docker-compose, e.g. dnsmasq).
   Отложено в TD-033: инфраструктурное смягчение единичных DNS-глюков; ортогонально коду.
   `SYNC_CONNECT_RETRIES` уже сглаживает единичный глюк на уровне приложения.

---

## Контракт для backend (сводка точек изменения)

- `worker/app/error_classify.py` (**новый**): `classify()` + `error_prefix()` на единой таблице
  подстрок (§1). Переиспользует существующие подстроки из инлайн-логики `sync_cycle`.
- `worker/app/sync_cycle.py`: убрать инлайн-классификацию и `_DISABLE_AFTER_FAILS`; `sync_one_account`
  возвращает `outcome`; bump/disable вынести в фазу 2 `_run_for_accounts` (§2, §3).
- `worker/app/imap_fetcher.py`: retry открытия+login на gaierror/connection (§4).
- `backend/app/repositories/mail_accounts.py`: новый метод записи `last_sync_error` без bump для
  transient — `mark_transient_error(account_id, *, error) -> None` (НЕ трогает `consecutive_failures`,
  `is_active`, `last_synced_at`).
- `shared/config.py`: 4 новых поля (§5).
- Без миграции (§6).
