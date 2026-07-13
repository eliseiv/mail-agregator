# ADR-0046 — Статус-канал ящика: исчерпывающий перечень hook-точек + семантика `last_synced_at`

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-07-13 |

**Амендмент** `ADR-0043` §2 (раздел «Status-канал ящика» — перечень точек hook) и `ADR-0026` (§«Текущая реализация» + §2, семантика `last_synced_at` на PERMANENT-ошибке). Парная норма CRM — `ADR-044` §3 вариант A («на каждое изменение статуса синка»), уточняется ссылкой на этот ADR. Курс `ADR-0043`/`ADR-044` не разворачивается: интент («CRM зеркалит статус синка ящика») сохраняется, здесь он делается **полным и проверяемым**.

## Context

`ADR-0043` §2 задал status-канал (`POST {CRM_INGEST_URL}/api/mail/mailbox-status`, тело `{mail_account_id, is_active, last_synced_at, last_sync_error, consecutive_failures}`) и назвал точку hook **тремя** функциями: «`mark_sync_success`/`_disable_after_failures`/`mark_transient_error`». Парный CRM `ADR-044` §3 требует push «на **каждое изменение статуса синка**».

При доведении реализации до этих трёх точек обнаружено, что перечень **неполон** и **не проверяем**:

1. **`mark_sync_failure` не назван.** `worker/app/sync_cycle.py:699` `_record_failure` → `MailAccountsRepo.mark_sync_failure` (`backend/app/repositories/mail_accounts.py:491`) в фазе 2 инкрементит `consecutive_failures` и пишет `last_sync_error` — то есть меняет ровно те поля, что зеркалятся, — но hook'а там нет.
2. **`accounts/service.py` (re-enable по смене кредов `:719` и `set_active` `:753`) не назван**, при том что hook там уже стоит де-факто (`:725`, `:759`) — реализация шире нормы (молчаливое расхождение).
3. **Семантика `last_synced_at` в docs противоречива.** `ADR-0026` §2 объявляет её «время последнего *успешного* sync» (и на этом же допущении построено подавление спорадики `_should_suppress_transient`, `worker/app/sync_cycle.py:663-683` — докстринг явно «synced **successfully** within window»), а `ADR-0026` §«Текущая реализация», `05-modules.md` (таблица «Поведение по классам») и `03-data-model.md` предписывают писать `last_synced_at=now()` **на PERMANENT-ошибке** — что код и делает (`backend/app/repositories/mail_accounts.py:505`). Два взаимоисключающих значения одного поля.

**Уточнение по остроте (важно для обоснования, проверено по коду):** отсутствие hook'а в `mark_sync_failure` **не** даёт «зелёный кружок у сбоящего ящика». PERMANENT-ветка `sync_one_account` **безусловно** вызывает `_record_transient` в фазе 0 того же цикла (`worker/app/sync_cycle.py:589`), а `_record_transient` enqueue'ит статус (`:696`); диспетчер грузит **живой снапшот из БД** (`worker/app/crm_status_dispatch.py:78` → `CrmStatusService.push_status`, `backend/app/crm_push/service.py:297-309`), а не значение на момент enqueue. Поэтому `last_sync_error` уходит в CRM на каждом сбойном цикле, и кружок CRM (`frontend/src/components/MailboxRow.tsx:71`: `is_active && consecutive_failures === 0 && last_sync_error == null`) краснеет. Пробел в `mark_sync_failure` — **дефект нормы (неполный перечень) и источник недетерминированности** (попадёт ли фазой-2 инкремент в снапшот, зависит от гонки «диспетчерский тик vs фаза 2»), а не боевой баг.

**А вот реально «зелёный кружок у мёртвого ящика» даёт другой, не названный нормой случай — OAuth needs-consent** (см. §3 H7).

## Decision

### §1. Семантика `last_synced_at` (нормативно, единственная): **время последней УСПЕШНОЙ синхронизации**

`mail_accounts.last_synced_at` = момент последнего **успешного** цикла синка. **Ни одна** ошибочная ветка (TRANSIENT / PERMANENT / disable / needs-consent) его не обновляет. Единственный писатель — `MailAccountsRepo.mark_sync_success` (`backend/app/repositories/mail_accounts.py:418-436`).

Обоснование:

1. **От этой семантики уже зависит горячая логика.** `_should_suppress_transient` (`worker/app/sync_cycle.py:663-683`) сравнивает `last_synced_at` с окном `SYNC_TRANSIENT_SUPPRESS_MINUTES`, трактуя его как «последний успех». Пока `mark_sync_failure` бампает его на PERMANENT-сбое (`mail_accounts.py:505`), ящик, сбоящий permanent'ом и ещё не отключённый, выглядит «свежесинканным» → последующая TRANSIENT-ошибка на нём **подавляется** и не пишется в `last_sync_error`. Это тихая порча наблюдаемости.
2. **UI CRM показывает это поле как «когда последний раз синкнули»** (`frontend/src/components/MailboxRow.tsx:177`, `formatRelativeTime(mailbox.last_synced_at)` / «ещё не было»), и CRM-контракт уже так его и описывает — `CRM docs/04-api.md`: «Время последнего **успешного** синка (зеркалится status-каналом)». Ящик, у которого синк валится, обязан **стареть** в этой колонке, а не читаться как «синхронизирован минуту назад».
3. **Цена нулевая: starvation невозможен.** `list_active()` (`backend/app/repositories/mail_accounts.py:186-193`) — `WHERE is_active ORDER BY last_synced_at NULLS FIRST, id` **без LIMIT**; цикл прогоняет **всех** активных, `ORDER BY` влияет только на порядок внутри цикла, не на состав (инвариант `05-modules.md` §«Инвариант полноты выборки»). Замороженный `last_synced_at` держит сбойный ящик в голове очереди — ровно как уже by design делает TRANSIENT-ветка (`mark_transient_error` его намеренно не трогает, `mail_accounts.py:471-489`).
4. **Отдельное `last_attempt_at` не заводим** (миграция ради поля, которого никто не потребляет: ни UI CRM, ни планировщик). Если потребуется — новый ADR (см. `TD-034`).

**Требуемый фикс кода (исполнитель — `backend`):** убрать `"last_synced_at": datetime.now(UTC)` из `values` в `MailAccountsRepo.mark_sync_failure` (`backend/app/repositories/mail_accounts.py:505`). Остальные поля метода (`consecutive_failures+1`, `last_sync_error`, `updated_at`, опц. `is_active`) — без изменений. Ведётся как `TD-053` до выкатки.

### §2. Инвариант hook'а (нормативно)

- Hook = **`_enqueue_crm_status(account_id)` строго ПОСЛЕ COMMIT** транзакции, изменившей статус. Enqueue **внутри** открытой транзакции запрещён: диспетчер читает живой снапшот из БД и может отработать до коммита → в CRM уедет **до**-состояние, а нового события уже не будет (статус залипнет до следующего цикла).
- Hook — **на call-site** (worker-хелпер / сервис), **не** внутри repo-метода (repo — чистый data-access, работает внутри чужой транзакции; см. предыдущий пункт).
- Hook **best-effort**: gated `crm_status_enabled`, обёрнут `try/except` — сбой Redis/сети НИКОГДА не роняет цикл синка (`worker/app/sync_cycle.py:735-755`).
- **Дедуп не требуется**: CRM идемпотентен и сам держит «ровно один алерт на переход» (`down_alert_sent_at`, CRM `ADR-044` §3). Допустимо до 3 статус-событий на ящик за сбойный цикл (фаза 0 + фаза 2 + disable) — это штатно.

### §3. Исчерпывающий перечень hook-точек (нормативно, поэлементно проверяемо)

Перечень построен от **писателей** зеркалимых полей (`is_active` / `last_synced_at` / `last_sync_error` / `consecutive_failures`), а не от «мест обновления статуса» общими словами. Каждый писатель обязан попасть **либо** в этот перечень, **либо** в перечень §4 (не-hook) с обоснованием.

| # | Точка (call-site) | Repo-метод (писатель) | Что меняется | Hook |
| --- | --- | --- | --- | --- |
| **H1** | `worker/app/sync_cycle.py:353` (успешный цикл) | `mark_sync_success` (`mail_accounts.py:418`) | `last_synced_at=now()`, `last_sync_error=NULL`, `consecutive_failures=0` | **есть** (`sync_cycle.py:365`) |
| **H2** | `worker/app/sync_cycle.py:686` `_record_transient` — покрывает 3 call-site: `:573` (TRANSIENT не подавлен), `:589` (PERMANENT, фаза 0), `:895` (нераспознанное исключение в `gather`) | `mark_transient_error` (`mail_accounts.py:471`) | `last_sync_error` | **есть** (`sync_cycle.py:696`) |
| **H3** | `worker/app/sync_cycle.py:699` `_record_failure` (фаза 2, call-site `:940`) | `mark_sync_failure` (`mail_accounts.py:491`) | `consecutive_failures+1`, `last_sync_error` (после фикса §1 — **без** `last_synced_at`) | **ОТСУТСТВУЕТ → ДОБАВИТЬ** |
| **H4** | `worker/app/sync_cycle.py:758` `_disable_after_failures` (call-site `:950`/`:957`) | `disable_and_stamp_alert` (`mail_accounts.py:438`) | `is_active=false`, `disabled_alert_sent_at` | **есть** (после COMMIT, `sync_cycle.py:803`) |
| **H5** | `backend/app/accounts/service.py:719` — ветка `creds_changed` в `MailAccountService.update` (re-enable по смене кредов) | `update_fields` (`mail_accounts.py:405`) | `is_active=true`, `last_sync_error=NULL`, `consecutive_failures=0`, `disabled_alert_sent_at=NULL` | **есть** (`accounts/service.py:725`) — **узаконивается** |
| **H6** | `backend/app/accounts/service.py:753` `MailAccountService.set_active` (внешний `PATCH /api/external/mailboxes/{id}` → `external/write_service.py:166`) | `update_fields` (`mail_accounts.py:405`) | `is_active` (activate: + сброс `last_sync_error`/`consecutive_failures`/`disabled_alert_sent_at`) | **есть** (`accounts/service.py:759`) — **узаконивается** |
| **H7a** | `backend/app/oauth/service.py:658` — **переход** в `oauth_needs_consent=true` (Microsoft `invalid_grant`) | `mark_oauth_needs_consent` (`mail_accounts.py:380`) | **сейчас:** только `oauth_needs_consent` (зеркалимых полей НЕ трогает) → **новое:** + `last_sync_error = OAUTH_NEEDS_CONSENT_SYNC_ERROR` | **ОТСУТСТВУЕТ → ДОБАВИТЬ** (вместе с записью `last_sync_error`) |
| **H7b** | `worker/app/sync_cycle.py:613-618` — **clean-skip** ящика, у которого `oauth_needs_consent` **уже** `true` (каждый цикл синка) | новый guarded-`UPDATE` (условие ниже) | `last_sync_error = OAUTH_NEEDS_CONSENT_SYNC_ERROR` — **только если** оно ещё не равно маркеру | **ОТСУТСТВУЕТ → ДОБАВИТЬ** (hook **только при фактической записи**) |

**H3 — обоснование включения.** `mark_sync_failure` меняет два из четырёх зеркалимых полей → по норме CRM `ADR-044` §3 («каждое изменение статуса синка») обязан пушить. Сегодня рост `consecutive_failures` попадает в CRM только по гонке (диспетчер грузит живой снапшот, и фаза 2 обычно успевает закоммититься до тика) — недетерминированно. Hook делает канал предсказуемым и снимает зависимость от порядка фаз.

**H5/H6 — обоснование включения (легализация).** Обе ветки меняют `is_active` и сбрасывают `last_sync_error`/`consecutive_failures` — то есть выполняют переход `false→true` (или ручной `true→false`), от которого зависит сброс/выставление `down_alert_sent_at` в CRM (`ADR-044` §3). Без push CRM не узнал бы о ручной активации/деактивации до следующего цикла синка (а у **деактивированного** ящика цикла не будет вовсе — `list_active()` его не выбирает → статус залип бы навсегда). Реализация была права, норма отставала — фиксируем.

**H7 — обоснование включения (реальный «зелёный кружок у мёртвого ящика»).** При `invalid_grant` от Microsoft ящик помечается `oauth_needs_consent=true` (`oauth/service.py:658`), и воркер **пропускает его каждый цикл без записи ошибки** (`worker/app/sync_cycle.py:613-618`, `return None` — «clean skip»). Ни одно из зеркалимых полей не меняется → CRM навсегда видит `is_active=true`, `consecutive_failures=0`, `last_sync_error=NULL` → **кружок зелёный, а ящик не синкается вовсе**; `last_synced_at` просто замирает.

**Маркер (единственный, нормативный).** Строка-маркер — **константа** `OAUTH_NEEDS_CONSENT_SYNC_ERROR` (`backend/app/repositories/mail_accounts.py:22`):

```
oauth_needs_consent: требуется переподключение Outlook
```

Тот же формат `"<prefix>: <detail>"`, что у остальных ошибок синка (≤500 симв.). Обе точки (**H7a** и **H7b**) пишут **ровно эту константу** — никаких вторых формулировок «по месту»; сравнение на идемпотентность (H7b) идёт с ней же.

**H7a — переход.** В транзакции, помечающей `oauth_needs_consent=true`, дополнительно писать `last_sync_error = OAUTH_NEEDS_CONSENT_SYNC_ERROR`; после COMMIT — hook.

**H7b — clean-skip (обязателен; H7a в одиночку дефект НЕ закрывает).** Точка перехода ловит только **новые** переходы. Ящик, у которого `oauth_needs_consent=true` **уже стоит**, в неё **никогда больше не попадёт**: воркер короткозамыкает **до** попытки refresh (`worker/app/sync_cycle.py:613-618` — `if account.oauth_needs_consent: return None`, только `log.info`), send-путь флаг лишь читает (`backend/app/send/service.py:224`, `:479`). Такой ящик остался бы с `last_sync_error=NULL` навсегда → в CRM `is_active=true / consecutive_failures=0 / last_sync_error=null` → **кружок вечно зелёный** (формула `frontend/src/components/MailboxRow.tsx:71`) — ровно тот дефект, который H7 обязан закрыть. То, что на проде сейчас `oauth_needs_consent=true` — **0 ящиков из 30 oauth**, есть удача текущего момента, а не свойство конструкции: любой из 30 может потерять consent, и состояние достижимо в обход точки перехода (ручной `UPDATE`, восстановление из бэкапа, будущая миграция).

Норма: в clean-skip-ветке воркера, **перед** `return None`, выполнить **guarded-`UPDATE`**:

- **условие записи:** `last_sync_error IS DISTINCT FROM OAUTH_NEEDS_CONSENT_SYNC_ERROR` (единственным `UPDATE ... WHERE`, без предварительного `SELECT` — гонки не нужны);
- **hook `_enqueue_crm_status` — ТОЛЬКО при фактической записи** (обновлена ≥1 строка, `RETURNING`/`rowcount`), **после COMMIT**. Идемпотентность обязательна: при уже проставленном маркере цикл **не пишет и не пушит ничего** — иначе получаем поток push'ей каждые `SYNC_INTERVAL` на каждый мёртвый ящик;
- **`is_active` и `consecutive_failures` НЕ трогаются** (инвариант `ADR-0025` §3 шаг 5 «needs-consent не дисейблит ящик» сохраняется в полном объёме, авто-disable не провоцируется); `last_synced_at` — тоже (§1: ошибочные ветки его не пишут).

Такая конструкция **самовосстанавливающаяся**: разовый data-backfill (`UPDATE … WHERE oauth_needs_consent AND last_sync_error IS NULL` + push по затронутым id) **не требуется** — clean-skip прогоняется каждым циклом синка и приводит любой needs-consent-ящик (legacy-строку, ручной `UPDATE`, восстановление из бэкапа) в корректное состояние за **один интервал синка**, ровно один раз.

**Общее для H7a/H7b.** Расширять контракт status-канала полем `oauth_needs_consent` **не** требуется (см. Alternatives). Самовосстановление в «зелёное»: после успешного re-consent (`ADR-0045`) первый же успешный цикл вызовет `mark_sync_success` → `last_sync_error=NULL` + push (**H1**) → кружок зеленеет (задержка ≤ одного цикла синка); отдельного сброса маркера не нужно (**N3**).

### §4. Не-hook точки (нормативно, с обоснованием)

| # | Место | Почему hook НЕ нужен |
| --- | --- | --- |
| **N1** | Создание ящика: `insert_account_with_id` (`mail_accounts.py:283-284`), `insert_oauth_account_with_id` (`:341-342`) — `is_active=true`, `consecutive_failures=0` | **CRM — инициатор** создания (`ADR-044` §4 / `ADR-0045`): он вызывает агрегатор, получает `id` и сам вставляет свою строку `mail_accounts` с начальным `is_active`. Начальное состояние ему известно без push; более того, push мог бы уйти **раньше** INSERT'а CRM → неизвестный `mail_account_id` → `200` no-op (CRM `ADR-044` §3, `TD-041`). |
| **N2** | Удаление ящика: `delete` (`mail_accounts.py:413`) | CRM — инициатор (`DELETE /api/external/mailboxes/{id}`), свою строку удаляет сам. Зеркалить нечего. |
| **N3** | OAuth-токены: `update_oauth_tokens` (`mail_accounts.py:349`), сброс `oauth_needs_consent=false` при re-consent (`oauth/service.py:388`, `:653`) | **Ни одно** из 4 зеркалимых полей не меняется. Возврат ящика в «зелёное» — через первый успешный цикл (H1), ≤ одного интервала синка. |
| **N4** | Подавленный TRANSIENT (`_should_suppress_transient == true`, `sync_cycle.py:555` / `:572`) | Записи в БД **нет вовсе** → изменения статуса нет → нечего зеркалить (by construction, а не по забывчивости). |
| **N5** | Сработавший circuit-breaker (`sync_cycle.py:914-935`) | Bump/disable подавлены **намеренно** (`ADR-0026` §3), а `last_sync_error` уже записан и отправлен в фазе 0 через **H2** (`:589`) → статус в CRM актуален. |
| **N6** | `mark_sync_failure(disable=True)` (`mail_accounts.py:508-509`) | В проде недостижим: единственный call-site (`sync_cycle.py:940-942`) передаёт `disable=False`; авто-disable идёт через **H4**. Если ветка когда-либо будет задействована — она всё равно покрыта **H3** (hook стоит на обёртке `_record_failure`, а не на условии). |
| **N7** | Call-site `update_fields(`, **не пишущие зеркалимые поля**: (а) oauth-ветка `MailAccountService.update` (`accounts/service.py:619`) — пишет **только** `display_name`; (б) та же `update` при `creds_changed == false` (`:719`) — пишет хосты/`display_name`/креды **без** сброса статуса (комментарий `:706-709`: «bare display_name edit must not reset `consecutive_failures`») | Ни одно из 4 зеркалимых полей не меняется → изменения статуса синка **нет**, зеркалить нечего. Точки перечислены явно, чтобы ревьюер, идущий по §5 буквально, не упирался в неклассифицированный call-site: `update_fields` — **generic**-писатель, к H5/H6 относятся только его **статус-пишущие** ветки. |

### §5. Проверяемость (как ревьюер/исполнитель сверяет перечень поэлементно)

1. Построить множество писателей: `grep -rn "is_active\|last_synced_at\|last_sync_error\|consecutive_failures" backend/app/repositories/mail_accounts.py` (единственный модуль, пишущий эти колонки) + те call-site `update_fields(` по `backend/app/` и `worker/app/`, которые **фактически пишут зеркалимые поля** (`update_fields` — generic-писатель; его нестатусные ветки классифицированы в **N7**, разбирать их заново не нужно).
2. Каждый писатель обязан быть отнесён к **H1–H6 / H7a / H7b** (есть hook после COMMIT) **или** к **N1–N7** (обоснованное отсутствие). Писатель, не попавший ни туда, ни туда → **дефект**, требует амендмента этого ADR (не «реализация на усмотрение»).
3. Каждая точка **H1, H2, H3, H4, H5, H6, H7a, H7b** покрывается **отдельным** тест-кейсом (`qa`): факт enqueue/push после COMMIT именно на этом пути. Общий тест «диспетчер умеет POST'ить» перечень **не** закрывает. **Для H7b обязателен второй кейс — идемпотентность:** повторный цикл при уже проставленном маркере **не** пишет и **не** пушит (иначе — поток push'ей каждые `SYNC_INTERVAL` на каждый мёртвый ящик).

## Consequences

- **Требуются фиксы кода** (исполнитель `backend`, ведутся как `TD-053` до выкатки):
  1. hook в `worker/app/sync_cycle.py::_record_failure` (после COMMIT) — **H3**;
  2. убрать `last_synced_at=now()` из `MailAccountsRepo.mark_sync_failure` (`mail_accounts.py:505`) — **§1**;
  3. запись `last_sync_error = OAUTH_NEEDS_CONSENT_SYNC_ERROR` + hook на пометке `oauth_needs_consent` (`oauth/service.py:658`) — **H7a**;
  4. guarded-`UPDATE` того же маркера + hook **при фактической записи** в clean-skip-ветке воркера (`worker/app/sync_cycle.py:613-618`) — **H7b**.
  Схема БД/миграции/контракт status-канала **не меняются** (те же 5 полей тела). **Разовый data-backfill не требуется** — H7b самовосстанавливает популяцию за один цикл синка.
- Наблюдаемость: сбойный ящик теперь честно **стареет** в колонке «Синхронизация» CRM; needs-consent Outlook-ящик краснеет с внятной причиной вместо «зелёный и тихий».
- Трафик статус-канала растёт незначительно (до 3 событий на сбойный ящик за цикл вместо 1–2); CRM идемпотентен, дедуп не нужен (`ADR-0043` §2).
- `ADR-0026` §2 «`last_synced_at` = последний успешный sync» становится **единственной** нормой; противоречащая ей строка §«Текущая реализация» (и производные таблицы в `05-modules.md` / `03-data-model.md`) приведены к ней.
- Подавление спорадики (`SYNC_TRANSIENT_SUPPRESS_MINUTES`) начинает работать как задумано (окно считается от реального успеха, а не от последней permanent-попытки).

## Alternatives considered

- **Оставить перечень из трёх точек как есть.** Отклонён: норма разошлась бы с кодом (H5/H6 де-факто есть), а H3/H7 остались бы «на усмотрение исполнителя» — ровно тот класс дефекта, из-за которого статус в CRM залипал.
- **Ставить hook внутрь repo-методов `mark_*`** (одно место вместо семи). Отклонён: repo работает **внутри** открытой транзакции — enqueue до COMMIT даёт гонку «диспетчер прочитал до-состояние» с залипанием статуса до следующего цикла. Hook обязан быть после COMMIT → место ему на call-site.
- **Семантика `last_synced_at` = «время последней ПОПЫТКИ»** (оставить запись на сбое, поправить докстринги). Отклонён: (а) сломана логика подавления спорадики (окно от «попытки» бессмысленно — оно должно мерить давность **успеха**); (б) UI CRM и CRM-контракт (`04-api.md`) уже трактуют поле как «последний успешный синк» — пришлось бы менять CRM-норму и вводить отдельный признак свежести успеха; (в) сбойный ящик читался бы как «синхронизирован минуту назад».
- **Завести `last_attempt_at` отдельной колонкой** (обе семантики сразу). Отклонён: миграция ради поля, которого не потребляет ни один консьюмер (планировщик обходится `last_synced_at NULLS FIRST` без LIMIT, UI CRM показывает успех). Остаётся опцией в `TD-034`.
- **Закрыть H7 только точкой перехода** (`oauth/service.py`), без clean-skip-ветки (H7b). Отклонён: ящик, уже помеченный `oauth_needs_consent=true`, в точку перехода **никогда не вернётся** (воркер короткозамыкает до refresh, `sync_cycle.py:613-618`) → остался бы с `last_sync_error=NULL` и вечно зелёным кружком в CRM. То, что на проде такая популяция сейчас **пуста** (0 из 30 oauth-ящиков), — удача момента, а не свойство конструкции.
- **Разовый data-backfill** (`UPDATE mail_accounts SET last_sync_error=… WHERE oauth_needs_consent AND last_sync_error IS NULL` + push по затронутым id) вместо H7b. Отклонён: отдельный ручной шаг выкатки, который чинит **сегодняшнюю** популяцию и **не защищает** от будущих попаданий в состояние в обход точки перехода (ручной `UPDATE`, restore из бэкапа, миграция). H7b покрывает и legacy-строки, и новые переходы, и обходные пути — идемпотентно, без шага выкатки.
- **Добавить `oauth_needs_consent` пятым полем в тело status-канала** (вместо маркера в `last_sync_error`, H7). Отклонён: контрактное изменение на обеих сторонах (схема + миграция CRM + UI-ветка) ради состояния, которое уже полностью выражается существующим `last_sync_error` («ящик не синкается, вот причина»); кружок CRM краснеет по имеющейся формуле без единой правки фронта. Если CRM когда-либо понадобится **отличать** needs-consent от прочих ошибок (кнопка «Переподключить» прямо в строке) — тогда отдельный ADR и расширение контракта.
