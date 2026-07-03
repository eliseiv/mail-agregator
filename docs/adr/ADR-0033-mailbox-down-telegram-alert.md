# ADR-0033: Telegram-оповещение о нерабочей (авто-отключённой) почте

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-07-03 |
| Связь с другими ADR | **Расширяет** [ADR-0022](./ADR-0022-telegram-sso-and-notifications.md) §2 (Telegram push-инфраструктура: `telegram_links`, `send_notification`, диспатчер, opt-out `users_settings`). **Триггерится** событием auto-disable из [ADR-0026](./ADR-0026-sync-error-resilience.md) §2/§3 (permanent-ошибка → `is_active=false`) и [ADR-0028](./ADR-0028-oauth-login-failed-transient.md) (контекстная классификация OAuth). Видимость получателей — по модели членств [ADR-0030](./ADR-0030-multi-group-membership.md) (`user_groups`), консистентно с [ADR-0019](./ADR-0019-groups-and-roles.md) §7. Доставка — **основным** ботом ADR-0022; push-боты [ADR-0027](./ADR-0027-push-team-bots.md) **не** участвуют. |

---

## Context

По ADR-0026 §2/§3 worker при **permanent**-ошибке синхронизации авто-отключает почтовый ящик: `mail_accounts.is_active := false` (в `worker.sync_cycle._disable_after_failures`) + audit-строка `account_auto_disabled`. Отключение происходит либо мгновенно (explicit auth/decrypt — rule 8/9 `error_classify`), либо по достижении порога `SYNC_MAX_CONSECUTIVE_FAILURES` подряд идущих permanent-ошибок, и **только** если не сработал circuit-breaker (защита от массового disable при общей инфра-аварии).

Проблема (продукт-запрос): после авто-отключения ящик **молча** перестаёт синхронизироваться. Пользователь/команда узнают об этом, только если зайдут в UI и заметят `last_sync_error` + неактивный ящик. На практике (96 прод-ящиков, ≤5 операторов) это приводит к тому, что сломавшийся ящик (протухший пароль, смена настроек провайдера) может простаивать сутками. TD-035 фиксирует смежный пробел «нет proactive-алерта», но касается затяжного circuit-breaker/OAuth-transient; здесь речь о конкретном, чётком событии — **переходе ящика в disabled**.

Требование пользователя: при авто-отключении ящика прислать **одно** Telegram-уведомление получателям, которые и так видят этот ящик, с указанием почты и причины, чтобы оператор пошёл и починил пароль/настройки.

Инфраструктура для этого уже есть (ADR-0022 §2): привязки `telegram_links`, функция `send_notification` основного бота, per-chat throttle, opt-out через `users_settings.tg_notifications_enabled`, паттерн «Redis-очередь + APScheduler-диспатчер». Не хватает: (а) события-триггера в точке disable, (б) идемпотентности «ровно один алерт на переход», (в) резолва получателей по ящику (а не по письму).

---

## Decision

### 1. Триггер — единственная точка перехода в disabled (`_disable_after_failures`)

Алерт инициируется **исключительно** в `worker.sync_cycle._disable_after_failures` — единственном месте, где активный ящик авто-отключается воркером (ADR-0026 §3). Это покрывает оба permanent-сценария (explicit auth/decrypt instant-disable и порог `N_consecutive_failures`) и **не** срабатывает, когда:

- **circuit-breaker сработал** — disable подавлён, ящик остаётся `is_active=true`, реального перехода нет → алерта нет (корректно: это вероятная общая авария, а не поломка конкретного ящика);
- **transient-ошибка** — не дисейблит (ADR-0026 §2), перехода нет;
- **OAuth needs-consent** (`invalid_grant`) — ящик **не** переводится в `is_active=false` (ADR-0025 §3 step 5: `is_active` не трогается, worker просто пропускает), отдельный «переподключить Outlook» UX — вне scope этого ADR;
- **ручное отключение пользователем** (`PATCH /api/mail-accounts/{id}` с `is_active=false` / прочие ручные пути) — это осознанное действие оператора, алерт не нужен и **не** шлётся (см. §2 — guard стоит только в worker-пути).

### 2. Идемпотентность «ровно один алерт на переход» — колонка `disabled_alert_sent_at`

Вводится новый столбец `mail_accounts.disabled_alert_sent_at TIMESTAMPTZ NULL` (Alembic-миграция `20260703_020`, §5). Семантика:

- `NULL` — по ящику нет неотработанного алерта об отключении (нормальное состояние активного ящика).
- `!= NULL` — алерт по текущему disabled-состоянию **уже поставлен в очередь** (штамп момента enqueue).

**Жизненный цикл (state machine):**

| Переход | Кто | Действие с `disabled_alert_sent_at` |
| --- | --- | --- |
| активный → авто-disabled (worker) | `_disable_after_failures` | **guarded set** `= now()` в **той же транзакции**, что `is_active=false` + audit; enqueue алерта **только если** штамп реально проставился (был `NULL`). |
| disabled → активный (re-enable пользователем) | `MailAccountService.update` (ветка `creds_changed`, где уже `is_active=true`, `last_sync_error=None`, `consecutive_failures=0`) | **reset** `= NULL`. |
| успешный sync | `mark_sync_success` | не трогает (ящик и так активен; штамп уже `NULL`). |

**Guard против двойного алерта.** UPDATE в `_disable_after_failures` ставит штамп атомарно и только при чистом переходе:

```sql
UPDATE mail_accounts
SET    is_active = false,
       disabled_alert_sent_at = now(),
       updated_at = now()
WHERE  id = :id
  AND  disabled_alert_sent_at IS NULL   -- guard: enqueue строго на переход NULL → now()
RETURNING id;
```

Алерт LPUSH'ится в очередь **только если** `RETURNING` вернул строку (штамп перешёл `NULL → now()`). Так гарантируется «ровно один алерт на переход»: повторный disable того же ящика в том же disabled-состоянии (теоретическая гонка двух циклов) штамп не перепроставит и алерт не задублирует. Повторный алерт возможен **только** после явного re-enable (сброс в `NULL`) и нового отключения — это и есть требуемое «снова включили и снова отключили».

> `_disable_after_failures` вызывается лишь для аккаунтов из `list_active()` (т.е. `is_active=true`), у которых при последнем включении штамп сброшен в `NULL`, — поэтому в штатном потоке это всегда чистый переход. Guard `WHERE ... IS NULL` — защита от гонки, а не основной механизм.

### 3. Получатели — новый метод `list_recipients_for_mailbox(mail_account_id)`

В `TelegramNotificationsRepo` добавляется метод-близнец `list_recipients_for_message` (ADR-0022 §2.2), но резолвящий получателей **по ящику**, а не по письму. Тот же предикат видимости (super_admin ИЛИ членство в команде ящика через `user_groups` по ADR-0030 ИЛИ владелец) + активная `telegram_links` (`dead_at IS NULL`) + opt-out `users_settings.tg_notifications_enabled`, но **без** per-message-предикатов:

```sql
SELECT DISTINCT
       u.id                AS user_id,
       tl.telegram_user_id AS telegram_user_id
FROM   mail_accounts ma
JOIN   users u
       ON (
           u.role = 'super_admin'
           OR (ma.group_id IS NOT NULL AND EXISTS (
                  SELECT 1 FROM user_groups ug
                  WHERE  ug.user_id = u.id
                    AND  ug.group_id = ma.group_id
              ))
           OR u.id = ma.user_id
       )
JOIN   telegram_links tl
       ON tl.user_id = u.id
       AND tl.dead_at IS NULL
LEFT JOIN users_settings us ON us.user_id = u.id
WHERE  ma.id = :mail_account_id
  AND  COALESCE(us.tg_notifications_enabled, true) = true;
```

Отличия от §2.2 (осознанные):
- **Нет** `m.internal_date >= tl.created_at` (first-link backfill guard) — у алерта нет письма/времени; отключение почты это **текущее** операционное событие, и его должен получить любой **сейчас** привязанный получатель, независимо от того, когда он привязал Telegram.
- **Нет** тег-предиката `EXISTS(message_tags)` / флага `TG_NOTIFY_ALL_MESSAGES` — теги относятся к письмам, не к состоянию ящика.

Метод возвращает `list[(user_id, telegram_user_id)]` (backend может переиспользовать `NotifyRecipient`, положив `mail_account_id` из входа). Дедуп чатов по `telegram_user_id` — на стороне диспатчера (§4), как в §2 (super_admin+owner overlap).

### 4. Доставка — очередь `mailbox_alert_queue` + диспатчер `mailbox_alert_dispatch` (основной бот)

Переиспользуется паттерн ADR-0022 §2.1/§2.4 (Redis-очередь + APScheduler-job), но **без** таблицы-реестра доставок: идемпотентность обеспечивается на уровне **ящика** штампом `disabled_alert_sent_at` (§2), а не per-message-строкой `telegram_notifications`. Это осознанно проще (модель ADR-0027 fire-and-forget) и достаточно, т.к. алерт — операционный сигнал, а не гарантированная доставка контента.

**Enqueue (worker, `_disable_after_failures`).** После COMMIT транзакции disable (§2), если штамп проставился и `MAILBOX_DOWN_ALERT_ENABLED=true`:

```
redis.lpush("mailbox_alert_queue", {"v":1, "mail_account_id": <id>, "reason": <reason>})
```

`reason` — та же стабильная строка, что пишется в audit `account_auto_disabled.details.reason` (ADR-0026 §3): `auth_failed` | `decrypt_fail` | `N_consecutive_failures`. LPUSH обёрнут в `try/except` с логом — сбой Redis **никогда** не должен ронять sync-цикл (та же изоляция, что `tg_notify` enqueue, ADR-0022 §2.1). Штамп уже в БД, поэтому потерянный при Redis-outage enqueue не приведёт к повтору (fire-and-forget, TD-042).

**Dispatch (worker, новый APScheduler-job `mailbox_alert_dispatch`).** По интервалу `MAILBOX_ALERT_DISPATCH_INTERVAL_SECONDS` (default 5), `max_instances=1, coalesce=True`:

```
1. items = redis.lpop("mailbox_alert_queue", count=MAILBOX_ALERT_BATCH_SIZE)  # default 30
2. for raw in items:
     p = parse(raw); if malformed: log + skip
     account = mail_accounts.get_by_id(p.mail_account_id); if None: log + skip (ящик удалён)
     recipients = list_recipients_for_mailbox(p.mail_account_id)   # §3
     dedup recipients by telegram_user_id                          # per-chat dedup в пределах одного алерта
     text = format_mailbox_down(acc_label, p.reason)               # §4-текст
     for chat_id in recipients:
        send_notification(chat_id, text_html=text, message_id=None)  # основной бот, best-effort
        # 403/400 → mark_link_dead (переиспользуем SSO mark_link_dead, ADR-0024 §2/§7)
        # 429/5xx/network → лог + дроп (НЕ ре-энквьюим — fire-and-forget, TD-042)
```

Диспатчер регистрируется в `worker/app/main.py` рядом с `tg_notify_dispatch`/`push_notify_dispatch`, через тот же `_safe_*`-wrapper (unhandled-исключение логируется, не валит scheduler). Job активен только при `MAILBOX_DOWN_ALERT_ENABLED=true`.

**Per-chat dedup** — в пределах одного алерта: `SELECT DISTINCT` уже даёт по строке на `(user_id, telegram_user_id)`, но пользователь с несколькими привязками (ADR-0024) или видимый и как owner, и как super_admin даёт несколько строк → коллапсируем по `telegram_user_id`, каждый чат получает алерт один раз. Кросс-цикловая идемпотентность — штамп `disabled_alert_sent_at` (повторного enqueue для того же перехода не бывает).

### 5. Формат уведомления

Одна `sendMessage` основным ботом, `parse_mode=HTML`:

```
⚠️ Почта <b>{acc_label}</b> не работает: {reason_ru}. Синхронизация приостановлена — проверьте пароль/настройки.
```

- `acc_label` = `account.display_name or account.email` (та же конвенция, что `acc_label` в §2.5 ADR-0022; для ящика без ника — сам email). Экранируется `html.escape()`.
- `{reason_ru}` — человекочитаемая RU-фраза, **детерминированно** маппится из стабильного `reason` (НЕ из сырого `last_sync_error`, чтобы не тащить в чат хостовые детали):
  - `auth_failed` → «ошибка авторизации (неверный пароль или логин)»;
  - `decrypt_fail` → «ошибка расшифровки сохранённых учётных данных»;
  - `N_consecutive_failures` (напр. `3_consecutive_failures`) → «почтовый сервер недоступен (N неудачных попыток подряд)».
- Кнопки «Посмотреть сообщение» нет (алерт не про письмо) — `message_id=None` в `send_notification`.

### 6. Data model

Единственное изменение схемы — новый nullable-столбец (§2). Полный DDL — `03-data-model.md` таблица `mail_accounts`; миграция — §5 ниже. Новых таблиц нет. `admin_audit` не расширяется (событие disable уже пишет `account_auto_disabled`, отдельного audit-действия для алерта не вводим — доставка best-effort, аудит-след — в логах диспатчера).

### 7. Alembic-миграция `20260703_020`

```
revision = "20260703_020"; down_revision = "20260623_019"
upgrade():   ALTER TABLE mail_accounts ADD COLUMN disabled_alert_sent_at TIMESTAMPTZ NULL;
downgrade(): ALTER TABLE mail_accounts DROP COLUMN disabled_alert_sent_at;
```

Backfill **не требуется**: на момент миграции все ящики получают `NULL` = «нет неотработанного алерта». Уже-отключённые (до фичи) ящики останутся `NULL` и **не** сгенерируют ретроактивный алерт (корректно — фича проактивна с момента внедрения; повторный алерт для них возможен только через re-enable → повторный disable). Forward-only (`07-deployment.md` migration policy).

### 8. Config (`shared/config.py` / env)

| Env | Default | Назначение |
| --- | --- | --- |
| `MAILBOX_DOWN_ALERT_ENABLED` | `true` | Kill-switch фичи. `false` → worker не enqueue'ит и не регистрирует job. Штамп при этом всё равно ставится в disable-транзакции (не ломает идемпотентность при последующем включении фичи). |
| `MAILBOX_ALERT_DISPATCH_INTERVAL_SECONDS` | `5` | Интервал `mailbox_alert_dispatch`. |
| `MAILBOX_ALERT_BATCH_SIZE` | `30` | `LPOP count` за тик. |

Доставка идёт **основным** ботом — переиспользуется `TELEGRAM_BOT_TOKEN` (ADR-0022); новых секретов нет.

---

## Consequences

### Положительные
- **Проактивный сигнал** об отключении конкретного ящика — оператор узнаёт сразу, идёт чинить пароль/настройки, не сканируя UI.
- **Ровно один алерт на переход** — штамп `disabled_alert_sent_at` исключает спам каждый цикл; повтор только на честном re-enable→disable.
- **Консистентность видимости с UI и с уведомлениями о письмах** — тот же предикат `user_groups`/owner/super_admin (ADR-0030), opt-out тот же (`users_settings`). Никто не получит алерт о ящике, которого не видит.
- **Переиспользование инфраструктуры** ADR-0022 (очередь+диспатчер, `send_notification`, `mark_link_dead`, throttle-паттерн) — минимум нового кода, один новый столбец, одна миграция, ноль новых таблиц/эндпоинтов.
- **Изоляция от sync-цикла** — enqueue в `try/except`; сбой Redis/Bot API не влияет на синхронизацию.

### Отрицательные / компромиссы
- **Fire-and-forget (нет recovery/re-enqueue)** — при падении worker между COMMIT disable и dispatch, либо при `429/5xx/network` алерт теряется без повтора (штамп уже стоит). Осознанный trade-off (как ADR-0027 push-боты): алерт — операционный сигнал, ящик всё равно видно отключённым в UI (`is_active=false`, `last_sync_error`). Зафиксировано как **TD-042**.
- **Штамп при `MAILBOX_DOWN_ALERT_ENABLED=false`** ставится, но алерт не шлётся — если позже включить фичу, уже-отключённые-в-этот-период ящики алерт не получат (штамп != NULL). Приемлемо: фича проактивна, историю не досылаем.
- **Reason — грубая гранулярность** — три класса причин (`auth_failed`/`decrypt_fail`/`N_consecutive_failures`); точный текст ошибки в чат не тащим (безопасность, §5 / `06-security.md`). Оператор видит детали в UI.

---

## Alternatives considered

1. **Слать алерт из generic-пути `update_fields(is_active=false)` / на любой disable.** Отвергнуто. Поймало бы и ручное отключение пользователем (осознанное действие — алерт не нужен), и требовало бы различать причину в общем репо-методе. Триггер строго в worker-пути `_disable_after_failures` — единственная точка авто-disable.

2. **Идемпотентность через строку в `telegram_notifications` (как для писем).** Отвергнуто. Ключ `telegram_notifications` — `(message_id, telegram_user_id)`; у алерта нет `message_id`. Пришлось бы либо расширять таблицу nullable-`message_id` + синтетический ключ, либо заводить отдельную таблицу `mailbox_alert_deliveries`. Штамп на уровне ящика (`disabled_alert_sent_at`) проще и точно отражает требование «один на переход» (переход — свойство ящика, не пары ящик-чат).

3. **Слать инлайн из `_disable_after_failures` (без очереди/диспатчера).** Отвергнуто. Bot API-вызов в фазе 2 `_run_for_accounts` блокировал бы цикл и подставлял бы sync под сетевые таймаузы Telegram. Очередь+диспатчер (существующий паттерн) изолирует доставку.

4. **Доставлять push-ботами ADR-0027.** Отвергнуто. Push-боты broadcast'ят по `account.group_id` в **статические** `ADMIN_TELEGRAM_IDS` и игнорируют членства (ADR-0030 §6, `06-security.md` §1.9). Требование — «та же видимость, что у уведомлений о письмах», т.е. персональные привязки `telegram_links` под visibility-scope. Это ровно канал **основного** бота ADR-0022.

5. **Recovery-scan для недоставленных алертов (по образцу ADR-0022 §2.8).** Отвергнуто для MVP. Потребовало бы реестра доставок (см. альтернативу 2). Для операционного сигнала fire-and-forget достаточно; при жалобах на пропуски — TD-042 (лёгкий трекинг + recovery, отдельный ADR).

---

## Open questions

Нет. Триггер (§1), идемпотентность и жизненный цикл штампа (§2), SQL получателей (§3), механизм доставки и dedup (§4), текст (§5), миграция (§7) и config (§8) зафиксированы. Компромисс отсутствия recovery — TD-042.

## Cross-references
- ADR-0022 §2 — Telegram push-инфраструктура (очередь, диспатчер, `send_notification`, opt-out, throttle).
- ADR-0026 §2/§3 — auto-disable (permanent-классификация, circuit-breaker, `_disable_after_failures`).
- ADR-0028 — контекстная классификация OAuth `login failed` (влияет на то, какие OAuth-сбои доходят до disable).
- ADR-0030 — членства `user_groups` (предикат видимости получателей).
- `05-modules.md` §14.3 — worker `mailbox_alert_dispatch`; §14 — точка enqueue; §18 — метод `list_recipients_for_mailbox`.
- `03-data-model.md` `mail_accounts.disabled_alert_sent_at`.
- `06-security.md` §1.9 — угрозы канала алерта (visibility, no-secret-leak).
- `100-known-tech-debt.md` TD-042.
