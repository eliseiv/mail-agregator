# ADR-0022 — Telegram persistent SSO + push-нотификации о письмах с тегами

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-05-13 |
| Заменяет / отменён | частично отменяет/superseded'ит ADR-0018 §5 (запрет линковки) и §«Alternatives 1–2»; закрывает TD-013 |

## Context

ADR-0018 явно зафиксировал минималистскую модель Telegram-интеграции: бот — чистый launcher, без линковки, без initData-аутентификации, без push-уведомлений. На том этапе требование пользователя было «никакой линковки».

С момента принятия ADR-0018 требования эволюционировали — пользователь явно запросил две новые фичи:

1. **Persistent SSO через Telegram.** «При вводе логина и пароля в аккаунте через telegram бота, мы запоминаем id пользователя, и в следующий раз при открытии приложения логин и пароль не требуется. Если пользователь выходит из аккаунта, то мы сбрасываем напоминание.» Это явный отказ от §5 ADR-0018 («Никаких изменений в auth/session/CSRF/БД») в части БД и auth-flow — теперь нужна линковка `telegram_user_id ↔ user_id` и автоматическое создание сессии при повторном открытии WebApp.

2. **Telegram-нотификации о письмах с тегами.** «Необходимо реализовать оповещение о новых сообщениях, в которых есть тег. Оповещение приходят всем участникам группы. Грубо говоря если пользователь может видеть сообщения для этой почты, то и оповещение ему должно прийти. Оповещение только о сообщениях в которых присутствует тег.» Это закрывает TD-013 — он намеренно был отложен «до явного запроса пользователя». Запрос пришёл.

Существующий бэкенд:
- FastAPI + SQLAlchemy 2 async + PostgreSQL 16 + Redis + APScheduler worker (ADR-0001, ADR-0003).
- Webhook `/api/telegram/webhook/{secret}` (ADR-0018), двойная проверка secret.
- `Telegram.WebApp` SDK уже подключён в `base.html` (`https://telegram.org/js/telegram-web-app.js`), CSP `script-src` уже расширен на `https://telegram.org`. Класс `body.tg-app` уже выставляется `static/js/tg.js`.
- Sessions: opaque token + Redis (ADR-0004); cookie `mas_session`, `mas_csrf` double-submit (ADR-0010).
- Visibility: ADR-0019 + production-патч после раунда 10 — `MailAccountsRepo.list_account_ids_visible` возвращает список `mail_accounts.id`, видимых пользователю. Ключ — `mail_accounts.group_id` (не `users.group_id`!) — аккаунт остаётся с исходной группой при переносе владельца.
- Tags: ADR-0017, per-user; auto-tagging в `worker.sync_cycle.save_message` после COMMIT INSERT messages.
- Bot API ограничение: ~30 msg/sec на бота, на 1 chat ~1/сек; 429 + `parameters.retry_after` обязателен к обработке.

Дополнительный контекст:
- Bot Token уже в env (`BOT_TOKEN`, см. TD-014 — расхождение имени в docs vs code; в этом ADR используется каноническое имя `TELEGRAM_BOT_TOKEN`, и TD-014 закрывается синхронизацией всех документов на это имя).
- В docs (`05-modules.md` §18, `06-security.md` §1.8, `07-deployment.md` §4) уже зафиксировано `TELEGRAM_BOT_TOKEN`; код использует `BOT_TOKEN`. Закрытие TD-014: переименовать env var в коде на `TELEGRAM_BOT_TOKEN` (обновить `shared/config.py` и `.env.example`); deploy-операция — обновить prod `.env` под новый ключ.

---

## Decision

### Часть 1 — Persistent SSO через Telegram

#### 1.1. Хранение связки — отдельная таблица `telegram_links`

Сравнили два варианта:

| Критерий | (A) `users.telegram_user_id BIGINT NULL UNIQUE` | (B) `telegram_links(telegram_user_id PK, user_id FK UNIQUE, …)` |
| --- | --- | --- |
| Сложность миграции | + одна колонка, простой UNIQUE | + таблица + 2 индекса |
| Поддержка extra-полей (audit, dead_at) | Невозможно без новых колонок users | Естественно (отдельные поля таблицы) |
| Logout=сброс линковки | UPDATE users SET telegram_user_id=NULL | DELETE FROM telegram_links |
| «Перепривязка» (новый login под другим user_id с того же telegram_id) | Нужен явный SQL `UPDATE users SET tg=NULL WHERE tg=:tid` (чтобы соблюсти UNIQUE), потом UPDATE на нового — два запроса, не атомарно без явной транзакции | Один `INSERT … ON CONFLICT (telegram_user_id) DO UPDATE SET user_id=…` — атомарно |
| Audit | Нужны отдельные записи в `admin_audit` | Поле `created_at` + audit-записи в `admin_audit` |
| Dead-link tracking (пользователь заблокировал бота, 403 от Telegram) | Нужна доп. колонка `users.tg_dead_at` (засоряет users) | Поле `dead_at` в telegram_links — изоляция домена |

**Выбран вариант B — `telegram_links`** по двум причинам:
1. Связка — отдельный домен с собственным жизненным циклом (создание / отзыв / dead-mark) и не должна засорять центральную таблицу `users`.
2. Атомарная upsert по `telegram_user_id` через `INSERT … ON CONFLICT (telegram_user_id) DO UPDATE` — единственно правильный механизм для сценария «один и тот же tg-user логинится под другим внутренним user'ом» (см. §1.4).

DDL — см. секцию data model в этом ADR ниже и `03-data-model.md` (новая таблица).

#### 1.2. Эндпоинт `POST /api/telegram/auth` — single source of SSO

Создаём **отдельный** публичный эндпоинт (а не встраиваем в существующий `/api/telegram/webhook` — у того другая семантика и protector).

| | |
| --- | --- |
| Путь | `POST /api/telegram/auth` |
| Content-Type | `application/json` |
| Тело | `{"init_data": "<raw initData string from Telegram.WebApp.initData>"}` |
| Доступ | публичный |
| CSRF | exempt (нет session при first call; защита — HMAC + auth_date TTL) |
| Rate-limit | 30/min per IP + 10/min per `telegram_user_id` (после HMAC валидации). См. §1.4 «Безопасность». |
| Side effects | (a) при успехе и существующей линковке — Set-Cookie `mas_session`/`mas_csrf` для линкованного user'а; (b) при успехе без линковки — устанавливает короткоживущий cookie `mas_tg_pending` (см. §1.3 шаг 5). |

Алгоритм валидации `init_data` — стандартный Telegram WebApp HMAC:

```
1. Parse init_data как application/x-www-form-urlencoded.
2. Извлечь hash = pairs.pop("hash").
3. data_check_string = "\n".join(sorted(f"{k}={v}" for k, v in pairs.items()))
4. secret_key = HMAC_SHA256(key="WebAppData", msg=TELEGRAM_BOT_TOKEN)
5. computed_hash = hex(HMAC_SHA256(key=secret_key, msg=data_check_string))
6. constant-time compare(computed_hash, hash); fail -> 401 invalid_init_data
7. auth_date = int(pairs["auth_date"])
8. if now - auth_date > 300 (5 минут) -> 401 init_data_expired
9. user_payload = json.loads(pairs["user"])
10. telegram_user_id = int(user_payload["id"])
```

Спецификация: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

**TTL = 5 минут** для аутентификационного запроса. Это жёстче, чем рекомендованные Telegram'ом 24 часа: 5 минут гарантирует, что украденный `initData` нельзя переиспользовать на следующий день. Telegram WebApp генерирует свежий `initData` при каждом открытии, так что 5 минут — комфортное окно «открытие → POST» без отказа на медленной сети.

Конфигурация хранится в `Settings.TELEGRAM_BOT_TOKEN` (`shared/config.py`) — уже есть.

#### 1.3. SSO Flow (sequence)

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant TG as Telegram WebApp
    participant FE as Browser/WebView (postapp.store)
    participant BE as FastAPI backend
    participant DB as PostgreSQL

    Note over U,TG: Открытие WebApp через бот
    U->>TG: тапает inline-button "Open Mail Aggregator"
    TG->>FE: open WebView URL = https://postapp.store
    FE->>FE: GET / (без cookie mas_session)
    Note over FE: tg.js видит window.Telegram.WebApp
    FE->>BE: POST /api/telegram/auth {init_data}
    BE->>BE: validate HMAC + auth_date TTL 5min
    alt invalid HMAC / expired
        BE-->>FE: 401 invalid_init_data
        FE->>FE: показать обычную /login форму
    else valid
        BE->>DB: SELECT user_id FROM telegram_links WHERE telegram_user_id=:tid AND dead_at IS NULL
        alt линковка существует
            BE->>BE: создать session (как при обычном login)
            BE-->>FE: 200 {linked: true} + Set-Cookie mas_session, mas_csrf
            FE->>FE: window.location.replace("/")
            FE->>BE: GET / (с cookie) -> Inbox
        else линковки нет
            BE->>BE: создать pending-token в Redis (tg_pending:{token} TTL 15min, value={telegram_user_id})
            BE-->>FE: 200 {linked: false} + Set-Cookie mas_tg_pending=token (HttpOnly, 15min)
            FE->>FE: window.location.replace("/login") (обычный two-step)
            U->>FE: вводит username, password
            FE->>BE: POST /login, /login/password (как ADR-0016)
            BE->>BE: успешный verify password
            BE->>BE: прочитать cookie mas_tg_pending -> Redis GET -> telegram_user_id
            BE->>DB: INSERT INTO telegram_links(telegram_user_id, user_id) VALUES (:tid, :uid) ON CONFLICT (telegram_user_id) DO UPDATE SET user_id=EXCLUDED.user_id, created_at=now(), dead_at=NULL RETURNING ...
            BE->>BE: AuditWriter.log(action="telegram_link_created", target_user_id=uid, details={telegram_user_id=tid, replaced=bool})
            BE->>BE: del Redis tg_pending:{token}; clear cookie mas_tg_pending
            BE-->>FE: 303 -> Set-Cookie mas_session, mas_csrf, Location: /
        end
    end

    Note over U,BE: Logout
    U->>FE: тап "Выйти"
    FE->>BE: POST /logout (CSRF)
    BE->>DB: DELETE FROM telegram_links WHERE user_id=:uid
    BE->>BE: AuditWriter.log(action="telegram_link_revoked", target_user_id=uid)
    BE->>BE: revoke session (Redis)
    BE-->>FE: 303 Location: /login (cookies очищены)
```

**Ключевые моменты flow:**

1. `tg.js` дополняется: на DOMContentLoaded, если `Telegram.WebApp.initData` непустая И отсутствует cookie `mas_session` (определяется через GET `/api/me` → 401, либо через server-rendered hint в HTML), — делает POST `/api/telegram/auth`. Логика:

   ```text
   if (window.Telegram?.WebApp?.initData && !document.cookie.includes("mas_session=")) {
     fetch("/api/telegram/auth", {method:"POST", headers:{"Content-Type":"application/json"},
           body: JSON.stringify({init_data: Telegram.WebApp.initData})})
       .then(r => r.json().then(j => ({status: r.status, body: j})))
       .then(({status, body}) => {
          if (status === 200 && body.linked) window.location.replace("/");
          // если linked=false — backend уже выставил mas_tg_pending; редирект на /login происходит автоматически
          //   через next .then (или просто остаёмся на странице, и server-rendered login form покажется)
          // при 401 — остаёмся, пользователь видит /login
       });
   }
   ```

   Простейшая реализация — на корневой странице `/` (анонимный GET возвращает HTML `/login`-формы по существующему механизму ADR-0016, см. `04-api-contracts.md`). Если в HTML `<body data-anonymous>` — `tg.js` запускает SSO. Детали инструкции для frontend-агента в этом же ADR (Implementation plan §F).

2. **Logout сбрасывает линковку.** В `auth.AuthService.logout` добавляется (в той же транзакции, что и revoke session): `DELETE FROM telegram_links WHERE user_id=:uid`. Это явное требование пользователя — «Если пользователь выходит из аккаунта, то мы сбрасываем напоминание».

3. **Линковка создаётся только после успешного `POST /login/password`** (step-2). На `/set-password` flow (первый логин с временным паролем) — после успешного `POST /set-password` также проверяется cookie `mas_tg_pending` и создаётся линковка. Это покрывает случай «пользователь созданный super-admin'ом первый раз заходит через бот».

4. **Пользователь с уже открытой сессией в браузере не получает «волшебной» pre-fill при открытии WebApp в боте,** т.к. cookies в Telegram WebView могут не синхронизироваться 1-в-1 с системным WebView на всех платформах. Логика остаётся той же — если `mas_session` уже есть, `tg.js` не делает SSO call; если нет — пробует. На каждой свежей сессии WebApp persistent SSO срабатывает корректно.

#### 1.4. Безопасность

| Угроза | Митигация |
| --- | --- |
| Подмена `init_data` злоумышленником с украденным `TELEGRAM_BOT_TOKEN` | `init_data` не приносит auth в одиночку — только если уже есть `telegram_links` запись. Атакующий, знающий tg_user_id и bot-token, может выпустить себе сессию **только** существующего залинкованного user'а. Mitigation: bot-token строго в env + redact-list (ADR-0014). При компрометации — revoke `telegram_links` (массовый DELETE) + ротация bot-token. |
| Replay украденного `init_data` | TTL 5 минут (см. выше). Дополнительно: можно (опционально, low-priority) хранить short-set `tg_seen:{auth_date}:{hash[:8]}` в Redis с TTL=5min для anti-replay внутри окна — **НЕ реализуется** в MVP (рассматриваем как future hardening; добавлено как сравнение с industry-best-practice). |
| Подмена `telegram_user_id` в `init_data` | Невозможна — `user` поле подписано HMAC'ом. Любая мутация ломает hash. |
| Brute-force HMAC (попытка подобрать hash без bot-token) | Rate-limit `30/min per IP` отсекает; HMAC-SHA256 неразрешим без ключа. |
| Один tg-user логинится под двумя разными аккаунтами поочерёдно | `INSERT … ON CONFLICT (telegram_user_id) DO UPDATE SET user_id=EXCLUDED.user_id, created_at=now(), dead_at=NULL` — последняя успешная пара логин-пароль перезаписывает линковку. Audit-запись `telegram_link_created` с `details.replaced=true` в `admin_audit` фиксирует факт. |
| Два разных tg-user'а пытаются залинковаться к одному внутреннему user'у | Допустимо? Нет — в `telegram_links` `user_id UNIQUE` (см. DDL). Второй INSERT упадёт на UNIQUE, backend возвращает 200 с `linked=false` и audit-запись `telegram_link_collision` — UI показывает «У этого аккаунта уже есть привязанный Telegram. Сначала разлогиньтесь в первом Telegram». Простейшая семантика «один user — один tg». |
| Пользователь украл cookie `mas_tg_pending` | Cookie HttpOnly, Secure, 15 мин TTL, одноразовый (после успешного login токен deleted). Без cookie атакующий может только запустить SSO для своего собственного tg-id, что не даёт ему ничего нового. |
| DOS на `/api/telegram/auth` | Rate-limit 30/min per IP + 10/min per telegram_user_id (после успешной HMAC валидации). Slowapi уже есть (ADR-0009). |
| Audit log спам через подделку tg_user_id | Невозможна — telegram_user_id вытаскивается из verified payload, не из request body. |

**Audit events** (новые `admin_audit.action`):
- `telegram_link_created` — после успешной upsert. `target_user_id = uid`, `details = {telegram_user_id, replaced: bool}`.
- `telegram_link_revoked` — после logout. `target_user_id = uid`, `details = {telegram_user_id}`.
- `telegram_link_dead_marked` — при 403/400 от Bot API при попытке доставить notification (см. часть 2). `target_user_id = uid`, `details = {telegram_user_id, reason: '403_blocked'|'400_chat_not_found'|...}`.
- `telegram_link_collision` — при попытке привязать второй tg к уже залинкованному user'у. `target_user_id = uid`, `details = {existing_telegram_user_id, attempted_telegram_user_id}`.

#### 1.5. Очистка линковки

| Событие | Что происходит |
| --- | --- |
| `POST /logout` (любая роль) | `DELETE FROM telegram_links WHERE user_id=:uid` в той же транзакции с revoke session. Audit: `telegram_link_revoked`. |
| `POST /api/admin/users/{id}/reset` (super-admin reset password) | `DELETE FROM telegram_links WHERE user_id=:id` (логика: смена пароля = новый владелец/устройство, линковку нельзя сохранять). Audit: `telegram_link_revoked` с `details.reason='password_reset'`. |
| `DELETE /api/admin/users/{id}` | Каскад: `telegram_links.user_id REFERENCES users(id) ON DELETE CASCADE`. Audit не нужен — событие покрыто `delete_user`. |
| `PATCH /api/admin/users/{id}` смена `role`/`group_id` | Линковка **сохраняется** (это тот же user, просто перешёл в другую группу/роль). Notifications могут перестать приходить, если в новой группе у user нет тегов — это ожидаемое поведение. |
| Bot API 403 (user blocked the bot) при попытке доставить notification | UPDATE `telegram_links SET dead_at=now() WHERE user_id=:uid`. Notification dispatcher не пытается доставить, пока `dead_at IS NOT NULL`. При следующем успешном `POST /api/telegram/auth` от того же tg_user_id — линковка реактивируется (upsert обнуляет `dead_at`). Audit: `telegram_link_dead_marked`. |

---

### Часть 2 — Telegram push-уведомления о письмах с тегами

#### 2.1. Триггер — после COMMIT транзакции save_message в `worker.sync_cycle`

В `worker.sync_cycle.save_message` сейчас (ADR-0017): INSERT messages + attachments + apply tags — всё в одной транзакции; падение apply откатывает всю транзакцию. Этот контракт **сохраняется**.

После успешного COMMIT (т.е. message INSERTed и tags applied) `save_message` собирает результат вызова `apply_tags_to_message` — он возвращает `applied_count: int`.

**Round-31 (требование «уведомлять обо ВСЕХ новых письмах»):** условие постановки в очередь становится зависимым от флага `TG_NOTIFY_ALL_MESSAGES` (`shared/config.py`, default `True`):

```python
# worker/app/sync_cycle.py (~296)
if settings.TG_NOTIFY_ALL_MESSAGES or applied > 0:
    notified_message_ids.append(inserted_id)
```

- `TG_NOTIFY_ALL_MESSAGES=true` (default) — в очередь попадает **каждое** вставленное письмо (вне зависимости от того, навесился ли тег). Это реализует требование «Telegram-уведомления по всем новым письмам».
- `TG_NOTIFY_ALL_MESSAGES=false` — историческое поведение: только письма, на которые навесился ≥1 тег (`applied > 0`). Откат к старому поведению — изменением одной env-переменной **без редеплоя кода** (только рестарт worker, чтобы `get_settings()` lru-cache перечитал env).

Имя поля очереди исторически называется `notified_message_ids`; семантически теперь это «message_ids к постановке в tg_notify_queue». Переименование — косметика, отложено (не TD).

После завершения цикла обработки одного account'а (т.е. после `mailbox.logout()`, шаг 9 алгоритма sync_one_account) sync-cycle обходит in-memory очередь и кладёт каждую пару в Redis-list `tg_notify_queue`:

```text
LPUSH tg_notify_queue '{"message_id": 12345, "mail_account_id": 67}'
```

(Однопроходный LPUSH в пакете — `LPUSH key val1 val2 val3 …`).

**Почему через очередь, а не inline в save_message:**
- Bot API ~30 msg/sec — синхронные вызовы из save_message блокировали бы IMAP-цикл.
- Падение Bot API не должно валить sync_cycle (явное требование §8 «Sync_cycle не падает при ошибке нотификации»).
- Очередь даёт буфер при temporary outage Bot API.
- Очередь даёт точку для retry на 429.

**Почему именно Redis-list (`LPUSH` / `BRPOP`), а не Postgres-таблица как очередь:**
- Дешевле (нет VACUUM, нет index bloat при тысячах ежедневных INSERT/DELETE).
- Идемпотентность доставки гарантируется **отдельной** таблицей `telegram_notifications` (см. §2.3).
- Падение Redis = временная пауза доставки; после restart очередь пуста, но **не-доставленные** сообщения остаются в БД и могут быть найдены `recovery_scan` (см. §2.6).

#### 2.2. Множество получателей на сообщение

Для каждого message_id, попавшего в очередь, dispatcher определяет получателей.

**Round-12 (bug A):** ушли от «у получателя есть СВОЙ тег» (теги навешиваются auto-tagging только владельцу ящика, поэтому group-mates лидера не получали ничего). Текущая модель — «у письма есть ХОТЯ БЫ ОДИН тег (любой, любого user'а)» через `EXISTS (message_tags)`, без per-user join на `tags`.

**Round-31 (уведомлять обо всех письмах):** блок `EXISTS (message_tags)` становится **условным от флага** `TG_NOTIFY_ALL_MESSAGES`. Реализация — подстановка предиката в Python при сборке SQL (а не SQL-параметр), т.к. это структурное изменение запроса:

SQL получателей — общая часть (всегда):

```sql
SELECT DISTINCT
       u.id                AS user_id,
       tl.telegram_user_id AS telegram_user_id,
       ma.id               AS mail_account_id
FROM   messages m
JOIN   mail_accounts ma ON ma.id = m.mail_account_id
JOIN   users u
       ON (
           u.role = 'super_admin'                          -- super_admin видит всё
           OR (ma.group_id IS NOT NULL AND u.group_id = ma.group_id)  -- участники группы
           OR u.id = ma.user_id                            -- владелец аккаунта
       )
JOIN   telegram_links tl
       ON tl.user_id = u.id
       AND tl.dead_at IS NULL                              -- мёртвые линковки исключаются
       AND m.internal_date >= tl.created_at                -- round-13: только письма ПОСЛЕ линковки
LEFT JOIN users_settings us ON us.user_id = u.id           -- §2.7 opt-out
WHERE  m.id = :message_id
  AND  COALESCE(us.tg_notifications_enabled, true) = true  -- default-on
  /* <TAG_PREDICATE> */
```

Фрагмент `<TAG_PREDICATE>`:

- `TG_NOTIFY_ALL_MESSAGES = false` (историческое поведение) — добавляется:
  ```sql
  AND EXISTS (SELECT 1 FROM message_tags mt WHERE mt.message_id = m.id)
  ```
- `TG_NOTIFY_ALL_MESSAGES = true` (default) — фрагмент **не подставляется вовсе** (пустая строка). Письмо без тегов остаётся валидным получателем.

Инварианты visibility / link / opt-out (`super_admin`/`group`/`owner`, `tl.dead_at IS NULL`, `m.internal_date >= tl.created_at`, `COALESCE(us.tg_notifications_enabled,true)=true`) **сохраняются в обоих режимах** — флаг затрагивает только тег-предикат.

Затем — теги письма (для текста уведомления) загружаются **один раз на сообщение** (round-12: не per-recipient — все получатели видят один набор тегов, что соответствует visibility-модели):

```sql
SELECT t.id, t.name, t.color
FROM   message_tags mt
JOIN   tags t ON t.id = mt.tag_id
WHERE  mt.message_id = :message_id
ORDER  BY mt.tag_id;
```

При `TG_NOTIFY_ALL_MESSAGES=true` этот запрос для письма без тегов вернёт пустой список — это нормальный кейс (см. §2.5: строка тегов опциональна), уведомление всё равно шлётся.

**Инварианты получателей:**
- Активная (`dead_at IS NULL`) `telegram_links` запись.
- `m.internal_date >= tl.created_at` (round-13: не флудить историей при первой линковке).
- Не в opt-out (`users_settings.tg_notifications_enabled = false`; default true).
- Тег-предикат — только при `TG_NOTIFY_ALL_MESSAGES=false`.

**Замечание про super_admin и flood-риск:** super_admin видит **все** письма всех групп (ADR-0019 §7.1). При `TG_NOTIFY_ALL_MESSAGES=true` это означает уведомление по **каждому** входящему письму всей системы → реальный риск flood и Bot API `429`. Меры:
1. Per-chat троттлинг (см. §2.9) — жёсткий потолок msg/min на каждый chat_id.
2. Индивидуальный opt-out (`tg_notifications_enabled=false`) для super_admin, не желающего потока.
3. Глобальный откат флага в `false` без редеплоя.

#### 2.3. Идемпотентность доставки — таблица `telegram_notifications`

Сравнили два варианта:

| Критерий | (A) Postgres `telegram_notifications(message_id, user_id PK, sent_at, telegram_message_id NULL)` | (B) Redis SET-NX `tg_notif_sent:{mid}:{uid}` с TTL=30d |
| --- | --- | --- |
| Persistence | Постоянное хранение | Эфемерное — restart Redis = потеря дедупа |
| Согласованность с retention messages | CASCADE через FK `message_id` | Нет — Redis ключи остаются после удаления message |
| Возможность audit «кому когда что отправили» | Прямой SQL | Нет |
| Стоимость | +1 INSERT на message-recipient | +1 SET NX |
| Recovery после Redis crash | Дедуп сохраняется | Может задвоить нотификации (хотя 30d TTL покрывает retention 30d ADR-0011) |

**Выбран вариант A — таблица `telegram_notifications`** (Postgres) по причинам:
- сообщения хранятся 30 дней (ADR-0011); таблица каскадно очищается через `messages.id ON DELETE CASCADE`.
- recovery_scan (см. §2.6) использует SQL LEFT JOIN на эту таблицу, чтобы найти невыданные нотификации после restart Redis.
- Audit: можно посмотреть, что user X получил уведомление по message Y в момент Z.

DDL: см. §3 «Data model changes» этого ADR.

Контракт диспатчера:
1. Перед `sendMessage` к Bot API — `INSERT INTO telegram_notifications (message_id, user_id) VALUES (:mid, :uid) ON CONFLICT (message_id, user_id) DO NOTHING RETURNING id`. Если RETURNING пустой (т.е. ON CONFLICT сработал) — пропускаем доставку (уже было отправлено).
2. Если INSERT прошёл — делаем `sendMessage`.
3. После успешного `sendMessage` — UPDATE row SET `telegram_message_id = :tg_msg_id, sent_at = now()`.
4. При ошибке `sendMessage` (5xx / network) — `DELETE FROM telegram_notifications WHERE id = :id` и оставляем item в очереди для retry.
5. При 403/400 от Bot API — UPDATE `telegram_links SET dead_at=now()`, оставляем row в `telegram_notifications` (но без `telegram_message_id` — это маркер «попытались, отказались») чтобы не пытаться ещё раз; при следующих письмах для этого user'а notif будет пропускаться (т.к. `dead_at IS NOT NULL` отсеет на этапе SQL получателей §2.2). Этот row остаётся как «попытка-отказ» в audit-целях.

#### 2.4. Dispatcher — отдельный APScheduler job

Архитектурное решение: **отдельный APScheduler job в worker**, тик каждые 5 секунд, drainит Redis-list `tg_notify_queue`.

Альтернативы рассматривались:
1. **Inline в sync_cycle.** Отвергнуто: Bot API rate-limit (~30/sec) и retry на 429 заблокировали бы цикл; sync_cycle должен быть быстрым.
2. **Отдельный процесс (новый контейнер).** Отвергнуто: нарушает «принцип простоты» из README; добавляет 1 контейнер, 1 healthcheck, 1 deploy-step ради ~30 строк кода. Worker уже есть, APScheduler в нём — естественное место.
3. **FastAPI-side dispatcher (в api контейнере).** Отвергнуто: API контейнер должен быть stateless и не держать APScheduler; нагрузка на worker'е логичнее (он уже фоновой).

Контракт:

```python
# worker/app/tg_notify_dispatch.py
async def tg_notify_dispatch() -> None:
    """Каждые 5 секунд драйнит Redis tg_notify_queue.
    max_instances=1, coalesce=True — гарантирует, что два тика не выполняются параллельно.
    """
    items = await redis.lpop("tg_notify_queue", count=NOTIFY_BATCH_SIZE)  # default 30
    if not items:
        return
    for raw in items:
        item = json.loads(raw)
        await dispatch_one(message_id=item["message_id"])

async def dispatch_one_payload(raw: str) -> None:
    # 1. Загрузить recipients (SQL из §2.2)
    # 2. Загрузить Message (db.get(Message, message_id)) + mail_account (display_name|email).
    #    Объект Message УЖЕ содержит subject / body_text / body_html — отдельный запрос/метод
    #    репозитория НЕ нужен (round-34). acc_label = display_name|email; from_label = from_name|from_addr.
    #    2b. round-34: posчитать body_preview ОДИН раз на message (не на recipient):
    #        raw = body_text if body_text.strip() else html_to_plain(body_html)   # fallback через sanitize_telegram_html
    #        preview = normalize_preview(raw)  # схлопнуть whitespace+nbsp+zero-width в 1 пробел, strip,
    #                                            # обрезать до PREVIEW_LEN=120 (+'…' если длиннее), '' если пусто
    #        Срез делается в Python, НЕ в SQL.
    # 3. Загрузить теги письма ОДИН раз (round-12, §2.2). tag_names может быть [].
    #    round-31: НЕТ раннего return при пустом tag_names — продолжаем (теги опциональны, §2.5).
    # 4. Для каждого recipient:
    #    a. round-31: per-chat throttle (§2.9) — try_consume(LIMIT_TG_SEND_PER_CHAT, key=chat_id).
    #       Если False -> continue (НЕ резервируем строку, НЕ ставим флаг re-enqueue).
    #       round-32: throttled-получателя доставит recovery_scan (NOT EXISTS notif), без hot-loop.
    #    b. try_reserve(message_id, user_id) ON CONFLICT DO NOTHING -> id|None; None -> continue (дедуп).
    #    c. Сформировать текст (§2.5) на общем tag_names + subject=message.subject + body_preview.
    #    d. await send_notification(chat_id, text, message_id).
    #    e. ok        -> mark_sent(telegram_message_id, sent_at=now()).
    #    f. dead(403/400) -> mark telegram_links.dead_at + audit; строку НЕ удаляем (audit-маркер).
    #    g. retry_after  -> rollback строки; needs_retry=True.
    #    h. transient(net/5xx) -> rollback строки; needs_retry=True.
    # 5. if needs_retry: enqueue_recovery([message_id]) — ТОЛЬКО retry_after / transient.
    #    round-32: throttle НЕ инициирует enqueue_recovery (busy-loop fix, §2.9) — берёт recovery_scan.
```

**Ограничения:**
- `TG_NOTIFY_BATCH_SIZE = 30` — один тик обрабатывает до 30 messages × N recipients. На частоте 5 сек = ~6/sec по сообщениям — глобально ниже 30 msg/sec лимита Bot API.
- Backoff на 429: на `retry_after` строка откатывается (`rollback`), весь `message_id` ре-энквьюится через `enqueue_recovery` (следующий тик повторит; идемпотентность не даст задвоить).
- Per-chat throttle (round-31, §2.9; busy-loop fix round-32): неблокирующая проверка `try_consume` **до** `try_reserve` — при исчерпании per-chat лимита получатель пропускается (`continue`), строка НЕ резервируется и **немедленный re-enqueue НЕ делается**. Доставку throttled-писем берёт на себя `recovery_scan` (часовой backoff), а не hot-loop — иначе при устойчивом `inflow > cap` возникал бы busy-loop (см. §2.9).
- `bot.send_notification` — метод в `backend/app/telegram/bot.py`.

#### 2.5. Формат уведомления

**Markup mode:** **HTML** (а не MarkdownV2). Обоснование:
- MarkdownV2 требует escape ~15 символов (`_*[]()~\`>#+-=|{}.!`), легко ломается при наличии этих символов в `from_addr`, `subject`, `display_name`, `tag.name` (а имена тегов кириллические + могут содержать спецсимволы — см. ADR-0017 builtin `DPLA.PLA`, который содержит точку).
- HTML требует escape только трёх: `<`, `>`, `&` — стандартная `html.escape()` решает.
- В HTML mode легко выделять жирным (`<b>`) — нужно для имени почты и тегов.
- Inline-keyboard поддерживается одинаково в обоих форматах.

Шаблон (новый Jinja2 template **не нужен** — формирование текста в Python, т.к. это не HTML-страница, а Bot API HTML subset).

**Round-31: строка тегов — ОПЦИОНАЛЬНАЯ.** При `TG_NOTIFY_ALL_MESSAGES=true` письмо может прийти без тегов; в этом случае строка тегов **не печатается вовсе** (а не плейсхолдер «—»). Структура:
- строка «почта» — **всегда**;
- строка «теги» — **только если** `tag_names` непуст (singular «Тег» / plural «Теги»);
- строка «отправитель» — **всегда**.

Bug-fix #4: Telegram `parse_mode=HTML` **не** декодирует HTML-entities (`&laquo;`/`&raquo;`) — пользователь увидел бы их буквально. Используем реальные UTF-8 кавычки `«` `»`.

**Round-34: добавлены ТЕМА письма и ПРЕВЬЮ тела.** Уведомление теперь даёт человекочитаемый тизер до открытия письма. `format_notification` получает два новых параметра: `subject: str | None` и `body_preview: str` (уже нормализованный и обрезанный — см. ниже). Обе строки опциональны в выводе:

- строка «почта» — **всегда**;
- строка «теги» — только если `tag_names` непуст (round-31);
- строка «отправитель» — **всегда**;
- строка «Тема:» — **только если** `subject` непуст после `.strip()` (письма без темы → строка не печатается, плейсхолдер «(без темы)» в push **не** показываем — это шум; полный заголовок «(без темы)» остаётся в callback-ответе §2.6 при открытии письма);
- строка «превью тела» — **только если** `body_preview` непуст (письмо без тела → строка отсутствует).

`subject` обрезается до `SUBJECT_MAX = 150` символов (по границе + «…»). `body_preview` нормализуется и режется до `PREVIEW_LEN = 120` символов **в Python** (не в SQL) — см. §2.4. Длины — **константы модуля** `notify_format.py` (не env: ретюн не нужен, лишний env-флаг — overhead). Обе строки — user-controlled → обязательный `html.escape()` (как `acc`/`from`/`tag`).

```python
PREVIEW_LEN: Final[int] = 120
SUBJECT_MAX: Final[int] = 150

def format_notification(
    *,
    acc_label: str,        # display_name or email
    from_label: str,       # from_name or from_addr
    tag_names: list[str],  # может быть ПУСТЫМ (письмо без тегов)
    subject: str | None,   # тема письма; None/'' -> строка не печатается
    body_preview: str,     # уже нормализованное+обрезанное превью; '' -> строка не печатается
) -> str:
    """HTML-строка для sendMessage parse_mode=HTML.
    Все user-controlled значения экранируются через html.escape()."""
    acc_safe = html.escape(acc_label)
    from_safe = html.escape(from_label)
    lines = [f'Вы получили письмо на почту <b>{acc_safe}</b>']
    if tag_names:  # строка тегов опциональна
        if len(tag_names) == 1:
            lines.append(f'Тег «<b>{html.escape(tag_names[0])}</b>»')
        else:
            names = ', '.join(f'«<b>{html.escape(t)}</b>»' for t in tag_names)
            lines.append(f'Теги {names}')
    lines.append(f'Отправитель <b>{from_safe}</b>')
    subj = (subject or '').strip()
    if subj:
        if len(subj) > SUBJECT_MAX:
            subj = subj[:SUBJECT_MAX].rstrip() + '…'
        lines.append(f'Тема: <b>{html.escape(subj)}</b>')
    if body_preview:  # body_preview уже нормализован+обрезан в notify_service
        lines.append(html.escape(body_preview))
    return '\n'.join(lines)
```

**Нормализация превью (выполняется в `notify_service.dispatch_one_payload`, НЕ в SQL — см. §2.4):**
- источник — `message.body_text` (plain). Если `body_text` пуст → `strip_tags(message.body_html)` через существующий `sanitize_telegram_html()` + дополнительное снятие оставшейся разметки до plain. **Обоснование выбора `body_text`:** round-29 зафиксировал, что у Apple `body_text` и `body_html` **различаются** (UI рендерит `body_html`). Для тизера в push нужен короткий человекочитаемый текст без верстки/CSS/трекинг-пикселей — `text/plain` part письма заведомо «чище» (нет тегов, нет инлайн-стилей), поэтому даёт осмысленный teaser «из коробки». `body_html` берём только как fallback, прогоняя через тот же sanitiser, что и callback (§2.6), чтобы не протёк CSS/скрипт. Несовпадение версий некритично: push — это тизер-приманка, полный «правильный» рендер (`body_html`) пользователь видит по кнопке «Посмотреть сообщение».
- схлопнуть любой whitespace (переводы строк `\n\r`, табы, множественные пробелы, неразрывный пробел ` ` и zero-width padding) в **один** пробел; обрезать по краям;
- срезать до `PREVIEW_LEN = 120` символов; если исходник длиннее — `[:120].rstrip() + '…'`;
- если после нормализации строка пуста → передать `''` (строка превью не печатается).

Пример с темой и телом (round-34):
```
Вы получили письмо на почту <b>support@example.com</b>
Тег «<b>DPLA.PLA</b>»
Отправитель <b>sender@gmail.com</b>
Тема: <b>Ваш заказ #12345 отправлен</b>
Здравствуйте! Ваш заказ был передан в службу доставки и поступит в пункт выдачи в течение 2–3 рабочих дне…
```

Пример без темы, но с телом (строка «Тема:» отсутствует):
```
Вы получили письмо на почту <b>Apple Test 1</b>
Отправитель <b>AppStoreNotices@apple.com</b>
Your subscription will renew soon. Tap to review the details and manage your plan in the App Store sett…
```

Пример с темой, но без тела (строка превью отсутствует):
```
Вы получили письмо на почту <b>support@example.com</b>
Отправитель <b>sender@gmail.com</b>
Тема: <b>(пустое уведомление)</b>
```

Пример с тегом — без темы и без тела (структура round-31, 3 строки):
```
Вы получили письмо на почту <b>support@example.com</b>
Тег «<b>DPLA.PLA</b>»
Отправитель <b>sender@gmail.com</b>
```

Пример без тегов — `TG_NOTIFY_ALL_MESSAGES=true` (строка тегов отсутствует):
```
Вы получили письмо на почту <b>support@example.com</b>
Отправитель <b>sender@gmail.com</b>
Тема: <b>Welcome</b>
Thanks for signing up — confirm your email to get started.
```

**Edge-cases:**
- пустой `subject` (`None` или `''` после strip) → строка «Тема:» опускается, плейсхолдер в push не добавляем;
- пустое тело (`body_text` и `body_html` оба пусты / дают пустой результат после нормализации) → строка превью опускается;
- очень длинная тема (>150) → срез `[:150].rstrip()+'…'`; очень длинное тело (>120) → срез `[:120].rstrip()+'…'`;
- HTML/спецсимволы (`<`, `>`, `&`) и кавычки в `subject`/превью → `html.escape()` (subject и тело сохраняются как обычный текст, не как разметка);
- многострочный `subject` (редко, но в письмах встречаются folded-заголовки) и переводы строк в теле → схлопываются в один пробел, push остаётся компактным (4096-лимит Bot API не превышается: максимум ~150+120 видимых символов + статичный текст);
- результат `format_notification` гарантированно ≤ ~400 символов после escape → одна `sendMessage`, без chunk-логики (chunk-сплит остаётся только в callback §2.6 для полного тела).

**Следствие для dispatcher (round-31):** ранний `if not message_tags: return` в `dispatch_one_payload` (§2.4) **убирается** — при пустом списке тегов продолжаем с `tag_names=[]`. Дедуп тегов по `(name, color)` (round-21) сохраняется.

**Inline keyboard:** одна кнопка `Посмотреть сообщение`. Реализация — **WebApp button** (не обычный URL), чтобы открытие происходило внутри Telegram-WebView, а не в системном браузере (UX → пользователь остаётся в Telegram):

```python
reply_markup = {
    "inline_keyboard": [[
        {
            "text": "Посмотреть сообщение",
            "web_app": {"url": f"{TELEGRAM_WEBAPP_URL}/messages/{message_id}?embed=tg"}
        }
    ]]
}
```

Почему web_app, а не url:
- `web_app` открывает страницу как Telegram WebApp — `Telegram.WebApp.initData` доступна → срабатывает Persistent SSO (§1.3) → пользователь сразу видит письмо без логина.
- `url` открыл бы тот же URL в внешнем браузере (отдельный WebView без cookies Telegram-WebApp) — пришлось бы заново логиниться или Persistent SSO бы не сработал.

#### 2.6. Просмотр без вложений — `?embed=tg` query parameter

Архитектурное решение: **добавить query-параметр `embed=tg`** на существующий route `GET /messages/{id}` (HTML-страница, рендерится `message_view.html`). НЕ создаём новый шаблон.

Обоснование:
- Один HTML-шаблон проще поддерживать. Логика «скрыть attachments» — одна if-ветка в Jinja2.
- `body.tg-app` (уже выставляется `tg.js`) уже скрывает topbar nav и применяет тёмную тему — что хорошо в Telegram WebApp. Bottom-nav же — оставляем, чтобы пользователь мог перейти в Inbox/Tags/Logout (см. ADR-0018 + frontend §11).
- Если query `embed=tg` присутствует — backend выставляет в Jinja-контекст `embed_tg = True`. Шаблон при `embed_tg=True`:
  - Скрывает раздел `<section class="attachments">…</section>`.
  - **Не скрывает** bottom-nav (она и так есть в `tg-app` для logout кнопки).
  - Действие `mark-read` остаётся доступным.
- Параметр query — для server-side ветки; класс body.tg-app — для client-side ветки. Независимы: пользователь, открывший `/messages/{id}?embed=tg` в обычном браузере, увидит письмо без вложений (это OK — edge case, не security-issue). Class `tg-app` выставляется только когда страница реально в Telegram WebApp.

Frontend-агент изменения (см. Implementation plan §F):
- `templates/message_view.html`: обернуть секцию `attachments` в `{% if not embed_tg %} … {% endif %}`.
- Backend `messages.router` → handler `GET /messages/{id}` принимает `embed: str | None = Query(default=None)`; передаёт `embed_tg = (embed == 'tg')` в context.

#### 2.7. Opt-out — таблица `users_settings`

Создаём **отдельную** таблицу `users_settings(user_id PK, tg_notifications_enabled BOOL NOT NULL DEFAULT TRUE, …)` вместо колонки в `users`. Причины:
- users становится thin (это core identity-таблица); пользовательские preferences — отдельный домен.
- Будущие настройки (язык UI после ADR-0021, плотность списка inbox и т.п.) добавляются как столбцы той же таблицы без миграций users.
- Запрос «нет ли opt-out» — LEFT JOIN с COALESCE(default true) — простой и без `NULL`-edge-cases.

DDL: см. §3 ниже.

API: на этой итерации **только backend-endpoint** реализуется (UI отложен — out of scope MVP, см. §«Open questions» этого ADR Q-002-1):
- `PATCH /api/me/settings` body `{tg_notifications_enabled: bool}` → 200 `{tg_notifications_enabled: bool}`.
- `GET /api/me` дополнительно возвращает `tg_notifications_enabled: bool`.

#### 2.8. Recovery после crash worker или Redis (round-33: per-recipient gap)

Если worker крашится между LPUSH (в sync_cycle) и LPOP (в dispatcher) — item теряется. Кроме того, при `TG_NOTIFY_ALL_MESSAGES=true` письмо видно ≥2 получателям, и один из них может быть пропущен per-chat throttle'ом (§2.9, `continue` без `try_reserve`), в то время как другой уже доставлен. recovery должен подобрать письмо в обоих случаях.

**Почему гранулярность recovery — per-recipient, а не per-message (round-33, fix CRITICAL).** Прежний recovery-SQL отбирал message_id по `NOT EXISTS (telegram_notifications WHERE tn.message_id = m.id)` — **без `user_id`**. Это создавало дыру при частичной доставке:
- Письмо `mid` видно получателям A и B. A доставлен → строка `(mid, A)` создана. B throttled → `continue`, строка `(mid, B)` **не создана**.
- recovery: `NOT EXISTS(tn WHERE message_id=mid)` = **FALSE** (строка A существует) → recovery **не вернёт** `mid` → B теряет уведомление **навсегда** (не «через час», не «за окном 24ч» — вообще).
- Тот же механизм маскировал pre-existing хрупкость при крашах: до троттлинга атомарность транзакции dispatch (`s.begin()`, всё-или-ничего по message) скрывала проблему; throttled→`continue` с коммитом частичной доставки её активировал.

**Решение (Опция A — recovery per-recipient, без миграции):** recovery-SQL отбирает `DISTINCT m.id`, у которых **существует видимый залинкованный получатель без строки** `telegram_notifications` по `(message_id, user_id)`. Для этого recovery переиспользует **ту же recipient-логику, что §2.2** (visibility super_admin/group/owner; JOIN активных `telegram_links` с `m.internal_date >= tl.created_at`; opt-out `COALESCE(us.tg_notifications_enabled,true)=true`; условный тег-предикат под флагом `TG_NOTIFY_ALL_MESSAGES`), но привязанную к `m.id` (а не `:message_id`) и под `NOT EXISTS (tn WHERE tn.message_id=m.id AND tn.user_id=u.id)`:

```sql
SELECT DISTINCT m.id
FROM   messages m
JOIN   mail_accounts ma ON ma.id = m.mail_account_id
JOIN   users u
       ON (
           u.role = 'super_admin'                          -- super_admin видит всё
           OR (ma.group_id IS NOT NULL AND u.group_id = ma.group_id)  -- участники группы
           OR u.id = ma.user_id                            -- владелец аккаунта
       )
JOIN   telegram_links tl
       ON tl.user_id = u.id
       AND tl.dead_at IS NULL                              -- мёртвые линковки исключаются
       AND m.internal_date >= tl.created_at                -- round-13: только письма ПОСЛЕ линковки
LEFT JOIN users_settings us ON us.user_id = u.id           -- §2.7 opt-out
WHERE  m.fetched_at > :cutoff                              -- now() - TG_NOTIFY_RECOVERY_WINDOW_HOURS
  AND  COALESCE(us.tg_notifications_enabled, true) = true  -- default-on
  /* <TAG_PREDICATE> */                                    -- тот же фрагмент, что §2.2 (условен от флага)
  AND  NOT EXISTS (                                         -- per-recipient, НЕ per-message
           SELECT 1 FROM telegram_notifications tn
           WHERE  tn.message_id = m.id
             AND  tn.user_id    = u.id
       )
ORDER  BY m.id
LIMIT  :limit;
```

Фрагмент `<TAG_PREDICATE>` — **идентичен** §2.2:
- `TG_NOTIFY_ALL_MESSAGES=true` (default) — **не подставляется** (пустая строка); recovery подхватывает любое письмо с недоставленным visible получателем.
- `TG_NOTIFY_ALL_MESSAGES=false` — добавляется `AND EXISTS (SELECT 1 FROM message_tags mt WHERE mt.message_id = m.id)`.

`DISTINCT m.id` нужен, потому что у письма может быть несколько недоставленных получателей — в очередь LPUSH'им message_id **один раз** (dispatch резолвит всех получателей сам). Для каждого найденного message — LPUSH в `tg_notify_queue` (`source=recovery`).

**Эффект:**
- throttled-получатель (нет строки `(mid, B)`) → recovery находит `mid` (есть недоставленный visible получатель B) → re-enqueue → dispatch резолвит всех: A пропускается (`try_reserve` → `None`), B снова проверяет throttle. Если поток спал — доставится; если нет — `continue`, следующий recovery (через час) повторит, пока поток не спадёт **ИЛИ** письмо не выйдет за окно 24ч (TD-027).
- Чинит pre-existing частичную доставку при крашах worker (часть получателей закоммичена, часть нет).
- recovery стал **visibility-aware** — больше НЕ делает лишних enqueue писем без получателей / до привязки Telegram. Это устраняет TD-025 (см. `docs/100-known-tech-debt.md` — TD-025 закрыт).

**Это НЕ busy-loop:** recovery идёт раз в час (`TG_NOTIFY_RECOVERY_INTERVAL_SECONDS=3600`), а не каждые 5 сек. Backlog при устойчивом throttle рассасывается ~`capacity`/чат/час (см. анализ в §2.9). Недоставка за окном 24ч при вечном флуде — осознанный компромисс (TD-027), причина — **устойчивый throttle**, а не «маскированная частичная доставка».

**Производительность.** Новый recovery-SQL раз в час делает JOIN recipient-логики (`mail_accounts`, `users`, `telegram_links`, `users_settings`) по письмам в окне `fetched_at > :cutoff` с `LIMIT :limit` (`TG_NOTIFY_RECOVERY_BATCH_SIZE`). Это приемлемо: (а) частота — 1/час, не hot-path; (б) `messages.fetched_at` индексируется (используется retention-cleanup ADR-0011); (в) `LIMIT` ограничивает worst-case строк; (г) `telegram_notifications (message_id, user_id)` имеет UNIQUE-индекс — `NOT EXISTS`-подзапрос идёт по индексу. На масштабе ≤5 users и retention 30д объём `messages` в окне 24ч мал. Если на проде вырастет — добавить partial-индекс `messages(fetched_at) WHERE …` (follow-up, не требуется сейчас).

- Окно 24 часа покрывает выходные / multi-hour outages. Старше — намеренно не нотифицируем.

#### 2.9. Per-chat троттлинг (round-31, защита от flood / Bot API 429)

**Проблема.** С `TG_NOTIFY_ALL_MESSAGES=true` super_admin (видит все письма всей системы) и активные группы могут получать десятки писем в минуту. Telegram Bot API ограничивает доставку в один chat (ориентир ~1 msg/sec на чат, burst-терпимость небольшая); превышение → `429 retry_after`. Нужен **upstream-троттлинг per chat_id**, чтобы не упереться в Bot API rate-limit и не флудить пользователя.

**Решение.** Перед каждым `sendMessage` конкретному получателю — неблокирующая проверка token-/window-bucket в Redis по ключу `chat_id`. Переиспользуем существующий `backend/app/rate_limit.py` (тот же fixed-window INCR/EXPIRE-механизм, что и для HTTP-роутов), добавив **неблокирующий** helper.

Новый helper в `backend/app/rate_limit.py` (не raising, в отличие от `consume`):

```python
async def try_consume(limit: Limit, key: str) -> bool:
    """Неблокирующая проверка fixed-window лимита.
    Возвращает True, если в текущем окне ещё есть бюджет (счётчик инкрементирован),
    False — если лимит исчерпан. НЕ бросает RateLimitedError.
    Реализация идентична consume(): INCR + EXPIRE(nx); over capacity -> False.
    Пустой key -> True (fail-open, как и consume логирует и не блокирует)."""
```

Новый предопределённый лимит:

```python
# capacity берётся из settings.TG_SEND_PER_CHAT_PER_MINUTE на consume-time
# (как LIMIT_WEBHOOK_TEST), window_seconds=60.
LIMIT_TG_SEND_PER_CHAT = Limit(name="tg_send", capacity=20, window_seconds=60)
```

Ключ в Redis: `rl:tg_send:<chat_id>` (формируется механизмом `rate_limit` из `name` + переданного `key=str(telegram_user_id)`).

**Где встраивается.** В `dispatch_one_payload` (`backend/app/telegram/notify_service.py`), **до** `try_reserve` и до `send_notification`:

```text
for recipient in recipients:                       # dispatch_one_payload
    # per-chat throttle ПЕРЕД try_reserve
    if not await try_consume(LIMIT_TG_SEND_PER_CHAT(capacity=settings.TG_SEND_PER_CHAT_PER_MINUTE),
                             key=str(recipient.telegram_user_id)):
        # throttled: НЕ резервируем строку, НЕ делаем hot re-enqueue.
        # Строка (message_id, user_id) НЕ появляется в telegram_notifications,
        # поэтому recovery_scan (раз в час, §2.8) подберёт это письмо в пределах
        # окна TG_NOTIFY_RECOVERY_WINDOW_HOURS=24 — естественный backoff ~1ч.
        continue                                    # этого получателя пропускаем СЕЙЧАС
    outcome = await self._dispatch_one_recipient(...)
    ...
if needs_retry:                                     # ТОЛЬКО retry_after / transient (см. §2.4)
    await self.enqueue_recovery([payload.message_id])
# throttled-получатели НЕ инициируют немедленный enqueue_recovery — их забирает recovery_scan.
```

**Ключевое решение (round-32, fix busy-loop):** throttled-получатель НЕ запускает немедленный hot re-enqueue в `tg_notify_queue`. Раньше throttle выставлял флаг `throttled` и в конце `dispatch_one_payload` целый `message_id` ре-энквьюился через `enqueue_recovery`. Это создавало **busy-loop**: на пути dispatch (`LPOP` → `dispatch_one_payload`) нет проверки возраста/счётчика попыток, поэтому при `TG_NOTIFY_ALL_MESSAGES=true` и активном super_admin (видит ВСЕ письма), если входящий поток в его chat стабильно превышает `TG_SEND_PER_CHAT_PER_MINUTE` (20/мин), throttle срабатывал бы каждый тик, и каждое неотправленное письмо ре-энквьюилось бы бесконечно → (а) неограниченный рост `tg_notify_queue`, (б) busy-loop с recipient-SQL каждые 5 сек (амплификация нагрузки на пике), (в) деградация порядка. При **постоянном** превышении окно троттла не «спадает», поэтому полагаться на «следующий тик повторит после спада окна» нельзя.

**Механизм throttled → continue → recovery (round-33: per-recipient):**
1. Throttled-получатель: `continue` без `try_reserve` → строка `(message_id, user_id)` в `telegram_notifications` **не создаётся**.
2. В конце `dispatch_one_payload` re-enqueue инициируется **только** для `needs_retry` (retry_after / transient), НЕ для throttle.
3. `recovery_scan` (APScheduler, раз в час, §2.8) отбирает письма, у которых есть **видимый залинкованный получатель без строки** `telegram_notifications` по `(message_id, user_id)` (per-recipient `NOT EXISTS`, recipient-логика §2.2), в пределах окна `TG_NOTIFY_RECOVERY_WINDOW_HOURS=24`. Для throttled-получателя B строка `(mid, B)` не зарезервирована → recovery подхватывает письмо **даже если другой получатель A уже доставлен** (строка `(mid, A)` существует). Это и есть исправление round-33: прежний per-message `NOT EXISTS (tn WHERE message_id=m.id)` при частичной доставке возвращал FALSE из-за строки A и терял B навсегда (см. §2.8).
4. `recovery_scan` LPUSH'ит `message_id` обратно в `tg_notify_queue` (один раз на письмо, `DISTINCT m.id`); dispatch снова резолвит **всех** получателей. Уже доставленные пропускаются (`try_reserve` встретит существующую строку → вернёт `None`). Throttled-получатель B снова проверяется на throttle — если поток всё ещё высокий, опять `continue`, и так до тех пор, пока поток не спадёт **ИЛИ** письмо не выйдет за окно 24ч.

**Анализ устойчивого backlog (inflow > per-chat cap):**
- recovery каждый час даёт ~`TG_SEND_PER_CHAT_PER_MINUTE × 60` ≈ capacity попыток на чат за час: при каждом часовом проходе часть накопленных писем проходит throttle (пока бюджет окна не исчерпан), остальные снова `continue`. Backlog рассасывается **постепенно**, без busy-loop и без амплификации (один проход в час вместо тика каждые 5 сек).
- Это естественный backoff ~1ч вместо 5-секундного busy-loop. Рост `tg_notify_queue` ограничен размером одного recovery-batch (`TG_NOTIFY_RECOVERY_BATCH_SIZE`), а не неограниченным hot re-enqueue.
- **Осознанный компромисс:** при **устойчивом** `inflow > per-chat cap` письмо может выйти за окно `TG_NOTIFY_RECOVERY_WINDOW_HOURS=24` раньше, чем recovery успеет его доставить (recovery перестаёт подбирать письма старше окна). Такое письмо останется **недоставленным** в Telegram. Это приемлемо при устойчивом флуде: альтернатива (бесконечный busy-loop + неограниченный рост очереди) хуже. Зафиксировано как `docs/100-known-tech-debt.md` TD-027 (severity low, условие адресации — увеличить cap/окно или приоритетная очередь). Письмо при этом остаётся в БД и видимо в UI — теряется только TG-нотификация, не само письмо.

**Решение по `retry_after` (Telegram 429): немедленный re-enqueue СОХРАНЯЕТСЯ.** Обоснование различия:
- `retry_after` (Bot API 429) — **кратковременная, не структурная** ситуация: Telegram сам сообщает, через сколько секунд можно повторить (`parameters.retry_after`, обычно единицы секунд). Это не предсказуемый постоянный per-chat предел, а единичный всплеск; повтор на следующем тике почти наверняка пройдёт. Busy-loop здесь не возникает, потому что условие самоустраняется за секунды (а не сохраняется, как при `inflow > cap`).
- `throttle` — **наш собственный предсказуемый per-chat лимит**: при устойчивом inflow он срабатывает детерминированно каждый тик, что и порождает busy-loop. Поэтому throttle обязан идти через recovery (часовой backoff), а НЕ через hot-loop.
- Вывод: `needs_retry` (retry_after / transient) → немедленный `enqueue_recovery` (как раньше); `throttle` → `continue` + ожидание recovery_scan. Разные пути для разной природы задержки.

Прочие детали:
- Проверка **до** `try_reserve` — чтобы НЕ занять `(message_id, user_id)` строку, которую затем пришлось бы откатывать. Throttle = «сейчас не отправляем», строку резервировать рано; для recovery критично, чтобы строки `(message_id, user_id)` НЕ было (иначе per-recipient `NOT EXISTS` §2.8 не сработает для этого получателя и письмо не будет подобрано для него).
- Идемпотентность через `telegram_notifications` UNIQUE `(message_id, user_id)` + `try_reserve` гарантирует, что на повторных recovery-проходах уже доставленные получатели пропускаются (их строка существует), без двойной доставки. recovery-SQL (§2.8) теперь **per-recipient** (`NOT EXISTS tn WHERE message_id=m.id AND user_id=u.id`), поэтому частично-доставленное письмо корректно подбирается ровно для недоставленных получателей.
- Lru-cached `settings` читается один раз; capacity подставляется на consume-time (как `WEBHOOK_TEST_LIMIT`), без редеплоя кода при смене env.

**Граница ответственности троттлинга:** это **per-chat** лимит. Глобальный bot-лимit (~30 msg/sec на всех чатах сразу) этим механизмом НЕ покрывается — он смягчён `TG_NOTIFY_BATCH_SIZE=30` за тик в 5 сек (~6/сек) и backoff на `429`. Глобальный явный throttle — follow-up (`docs/100-known-tech-debt.md` TD-026).

**Миграций нет.** Троттлинг полностью в Redis; схема БД не меняется. Идемпотентность доставки (§2.3) сохраняется без изменений.

---

### 3. Изменения в data model (новые таблицы + новые `admin_audit.action`)

```sql
-- 1. telegram_links — связка Telegram-аккаунта с внутренним user'ом (Часть 1)
CREATE TABLE telegram_links (
    telegram_user_id BIGINT PRIMARY KEY,                                      -- из Telegram User.id (Bot API)
    user_id          BIGINT NOT NULL UNIQUE                                   -- один internal user — один telegram (см. §1.4)
                     REFERENCES users(id) ON DELETE CASCADE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    dead_at          TIMESTAMPTZ NULL                                          -- !=NULL = доставка невозможна (403/400 от Bot API)
);

CREATE INDEX telegram_links_user_id_idx ON telegram_links(user_id);            -- логин-flow lookup по user_id
-- (PK на telegram_user_id обслуживает обратный lookup при SSO)

-- 2. telegram_notifications — реестр доставленных уведомлений (Часть 2, идемпотентность)
CREATE TABLE telegram_notifications (
    id                   BIGSERIAL PRIMARY KEY,
    message_id           BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id              BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sent_at              TIMESTAMPTZ NULL,                                     -- NULL = строка вставлена, send_message ещё/уже не выполнен
    telegram_message_id  BIGINT NULL,                                          -- message_id в Telegram chat (для будущего edit/delete)
    CONSTRAINT telegram_notifications_unique UNIQUE (message_id, user_id)
);

CREATE INDEX telegram_notifications_message_id_idx ON telegram_notifications(message_id);
CREATE INDEX telegram_notifications_user_id_idx ON telegram_notifications(user_id);

-- 3. users_settings — пользовательские preferences (Часть 2, opt-out)
CREATE TABLE users_settings (
    user_id                    BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    tg_notifications_enabled   BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Триггер BEFORE UPDATE: NEW.updated_at = now()

-- 4. admin_audit.action — расширение enum-строк (без DDL — это TEXT с CHECK на app-уровне):
-- Новые значения: 'telegram_link_created', 'telegram_link_revoked',
--                 'telegram_link_dead_marked', 'telegram_link_collision'.
```

Каскады:
- `DELETE FROM users WHERE id=:uid` → каскадно удалит `telegram_links`, `telegram_notifications` (по user_id), `users_settings`.
- `DELETE FROM messages WHERE id=:mid` (retention cleanup, ADR-0011) → каскадно удалит `telegram_notifications` (по message_id).

Объёмы:
- `telegram_links`: ≤ 5 строк (5 пользователей сервиса × ≤1 tg-аккаунт).
- `telegram_notifications`: оценка — ≤ 5 users × ≤ 100 ящиков × ~5 писем с тегами/день × 30 дней retention = ~75 000 строк max. С ON DELETE CASCADE автоочистка вместе с messages.
- `users_settings`: ≤ 5 строк.

---

### 4. Изменения в API contracts

#### Новый: `POST /api/telegram/auth` (см. §1.2 выше)

| | |
| --- | --- |
| Доступ | публичный |
| CSRF | exempt |
| Rate-limit | 30/min per IP + 10/min per telegram_user_id |
| Запрос | JSON `{init_data: str}` |
| 200 (linked) | Set-Cookie `mas_session`+`mas_csrf`, body `{linked: true, redirect: "/"}` |
| 200 (unlinked) | Set-Cookie `mas_tg_pending` (HttpOnly, 15min), body `{linked: false, redirect: "/login"}` |
| 401 | `invalid_init_data` (HMAC mismatch) |
| 401 | `init_data_expired` (auth_date > 5 min) |
| 429 | `rate_limited` (+ Retry-After) |

#### Новый: `PATCH /api/me/settings`

| | |
| --- | --- |
| Доступ | user-сессия |
| CSRF | yes |
| Запрос | JSON `{tg_notifications_enabled: bool}` |
| 200 | `{tg_notifications_enabled: bool}` |

#### Изменение: `GET /api/me` — добавляется поле `tg_notifications_enabled: bool` (default `true` если строки в `users_settings` нет).

#### Изменение: `POST /logout` (контракт не меняется, но дополнительно DELETE telegram_links — описано в §1.5).

#### Изменение: `POST /api/admin/users/{id}/reset` (контракт не меняется, но дополнительно DELETE telegram_links — описано в §1.5).

#### Изменение: `GET /messages/{id}` HTML route — принимает `embed: str | None = Query(default=None)`. Если `embed == 'tg'` — context flag `embed_tg = True` → шаблон скрывает attachments.

---

### 5. Изменения в безопасности

| Что | Где |
| --- | --- |
| HMAC валидация `init_data` | `06-security.md` новая секция 1.9 — STRIDE для Telegram-SSO. |
| Rate-limit `/api/telegram/auth` | `06-security.md` §7 — добавить строку. |
| Redact-list уже покрывает `TELEGRAM_BOT_TOKEN` | без изменений (ADR-0014). |
| `mas_tg_pending` cookie | HttpOnly + Secure + 15 мин + одноразовая. Описать в §1.2/§5 `06-security.md`. |
| Новые audit-events | `06-security.md` §8 — добавить `telegram_link_*` action'ы. |

CSP `frame-ancestors 'none'` остаётся неизменной — Telegram WebView не вкладывает страницу в iframe (открывает через native WebView), как описано в `06-security.md` §1.8 и сохраняется здесь.

---

## Consequences

### Positive

- Закрывается явный пользовательский запрос: persistent SSO + push-нотификации.
- Линковка изолирована в отдельной таблице — `users` остаётся thin.
- Идемпотентность доставки гарантируется БД (Postgres UNIQUE), не Redis.
- Bot API rate-limit (~30 msg/sec) обрабатывается отдельным dispatcher'ом, не блокирует sync_cycle.
- Notification text формируется в Python (`html.escape`), новый Jinja2-шаблон не нужен.
- Просмотр в WebApp — переиспользование `message_view.html` через `?embed=tg`, экономит ~150 строк нового кода.
- Opt-out в отдельной таблице → расширяемая для будущих preferences.

### Negative / risks

- **Breaking change для существующих сессий?** Нет — линковка опциональна; пользователи, не использующие бот, не затронуты. Существующая ADR-0018 launcher-функциональность сохраняется без регрессий.
- **HMAC validation сложнее, чем cookie auth** — больше surface area для bug'ов (sortedness, encoding); MUST покрываться unit-тестами с эталонными векторами из Telegram спецификации.
- **Push spam risk**: пользователь с активным фильтром «sender_contains @» может получать десятки нотификаций в день. Митigation: opt-out flag default true (но user сможет выключить). UI для toggle отложен в §«Open questions» Q-002-1.
- **Закрывает TD-013** (push-уведомления) и **TD-014** (имя env var) — оба требуют синхронизации docs.
- **ADR-0018 частично теряет силу.** Раздел «Никаких изменений в auth/session/CSRF/БД» в §5 ADR-0018 теперь устаревший. ADR-0018 в самом ADR-0018 не редактируется (иммутабельны), но в `INDEX.md` его статус помечается `partially superseded by ADR-0022`. Раздел «Tech debt registry → TD-013» ADR-0018 закрывается. Раздел «Alternatives 1, 2» ADR-0018 (которые отвергали initData-auth и линковку) — теперь являются принятыми решениями ADR-0022; это нормальная эволюция архитектуры.
- **Race в SSO sequence**: пользователь после `POST /api/telegram/auth` (linked=false) переходит на /login, но открывает в другой вкладке /login напрямую и логинится — линковка может не создаться (cookie `mas_tg_pending` может быть в первой вкладке). Допустимо: пользователь увидит в следующий раз форму login — повторное открытие бот-кнопки начнёт SSO заново.
- **Cookie `mas_session` НЕ может быть проверена через `document.cookie` в JS** (она HttpOnly). Альтернатива в `tg.js`: проверять через GET `/api/me` 200 vs 401, или через HTML-маркер `<body data-anonymous>` в server-rendered HTML. Frontend-агент выбирает второе как простейшее.

### Migration plan

1. **Миграция `004_telegram_sso_and_notifications.py`** (Alembic):
   - `CREATE TABLE telegram_links` (с FK + индексы).
   - `CREATE TABLE telegram_notifications` (с FK + UNIQUE + индексы).
   - `CREATE TABLE users_settings` (с FK + trigger updated_at).
   - Никакой data-миграции — все таблицы стартуют пустыми.
2. **TD-014 cleanup** (одновременно в этом же спринте):
   - `shared/config.py`: rename `BOT_TOKEN` → `TELEGRAM_BOT_TOKEN`. Поддержать оба имени временно через `Settings(env_prefix=..., aliases=...)` или явный fallback. После deploy и подтверждения работы — удалить старое имя в следующем минорном.
   - `.env.example`: переключить на `TELEGRAM_BOT_TOKEN`.
   - `07-deployment.md`: уже использует `TELEGRAM_BOT_TOKEN`, проверить.
3. **Backend implementation** (см. §«Implementation plan» ниже).
4. **Frontend implementation** (см. §«Implementation plan» ниже).
5. **DevOps**: env var переименование на prod (или alias) синхронно с deploy.

---

## Alternatives considered

1. **Не разделять SSO и Notifications на два этапа, реализовать сразу в одном спринте.** ✅ Принято — задачи связаны через `telegram_links` (без линковки нет notification target). Разделять смысла нет.

2. **Хранить линковку в `users.telegram_user_id`** (вариант A в §1.1). Отвергнуто — обоснование см. §1.1.

3. **Inline notification в `worker.sync_cycle.save_message`** (без Redis-очереди). Отвергнуто — обоснование §2.1.

4. **Использовать MarkdownV2 вместо HTML.** Отвергнуто — обоснование §2.5.

5. **Создать отдельный шаблон `_tg_view.html`** для просмотра без вложений. Отвергнуто — `?embed=tg` flag на существующий `message_view.html` проще и DRY. См. §2.6.

6. **Хранить opt-out флагом в `users.tg_notifications_enabled`**. Отвергнуто — отдельная таблица `users_settings` лучше расширяема (будущие preferences).

7. **TTL initData 24 часа** (Telegram default). Отвергнуто — 5 минут жёстче и адекватнее для одноразового auth-запроса. См. §1.2.

8. **`url`-кнопка вместо `web_app`-кнопки в inline keyboard.** Отвергнуто — `url` открыл бы вне Telegram, ломая Persistent SSO. См. §2.5.

9. **Создать новый процесс/контейнер для notification dispatcher.** Отвергнуто — лишний deploy-overhead. APScheduler-job в существующем worker достаточен. См. §2.4.

10. **Не делать opt-out в MVP — пользователь захочет отключить, но это второстепенно.** Отвергнуто — must-have. Бот без opt-out быстро превратится в спамер; критическое требование UX.

---

## Implementation plan

### A. Backend (FastAPI)

**Миграции:**
- `backend/migrations/versions/004_telegram_sso_and_notifications.py` — DDL из §3.

**Новые модули:**
- `backend/app/models/telegram_link.py` — ORM `TelegramLink`.
- `backend/app/models/telegram_notification.py` — ORM `TelegramNotification`.
- `backend/app/models/user_settings.py` — ORM `UserSettings`.
- `backend/app/repositories/telegram_links.py`:
  ```python
  class TelegramLinksRepo:
      async def get_by_telegram_user_id(tid: int) -> TelegramLink | None  # WHERE dead_at IS NULL
      async def upsert(telegram_user_id: int, user_id: int) -> tuple[TelegramLink, bool]  # returns (row, replaced_bool)
      async def revoke_for_user(user_id: int) -> None  # DELETE WHERE user_id=:uid
      async def mark_dead(user_id: int, reason: str) -> None  # UPDATE SET dead_at=now()
  ```
- `backend/app/repositories/telegram_notifications.py`:
  ```python
  class TelegramNotificationsRepo:
      async def try_reserve(message_id: int, user_id: int) -> int | None  # INSERT ... ON CONFLICT DO NOTHING RETURNING id; None если уже было
      async def mark_sent(notif_id: int, telegram_message_id: int) -> None
      async def rollback(notif_id: int) -> None  # DELETE WHERE id=...
      async def list_recipients_for_message(message_id: int) -> list[NotifyRecipient]  # SQL из §2.2
  ```
- `backend/app/repositories/user_settings.py`:
  ```python
  class UserSettingsRepo:
      async def get(user_id: int) -> UserSettings | None
      async def upsert(user_id: int, *, tg_notifications_enabled: bool) -> UserSettings
  ```
- `backend/app/telegram/auth_service.py`:
  ```python
  class TelegramAuthService:
      async def validate_init_data(init_data: str) -> ValidatedTelegramUser  # HMAC + timestamp, raises InvalidInitData
      async def try_sso(init_data: str, ip: str, ua: str) -> SSOResult  # (linked_session, csrf) | (pending_token,)
      async def link_pending(pending_token: str, user_id: int) -> None  # вызывается из auth.login/set_password
      async def revoke_for_user(user_id: int) -> None  # вызывается из auth.logout
  ```
- `backend/app/telegram/router.py` — добавить route `POST /api/telegram/auth`. Webhook остаётся (ADR-0018).

**Изменения в существующих модулях:**
- `backend/app/auth/service.py`:
  - `login(...)` после успешного verify password — если есть cookie `mas_tg_pending` → `tg_auth_service.link_pending(token, user.id)`.
  - `complete_set_password(...)` — то же.
  - `logout(...)` — `tg_auth_service.revoke_for_user(user.id)` + audit.
- `backend/app/admin/service.py`:
  - `reset_password(...)` — также `tg_auth_service.revoke_for_user` + audit.
- `backend/app/telegram/bot.py` — добавить:
  ```python
  async def send_notification(chat_id: int, text_html: str, message_id: int) -> int | None:
      # POST sendMessage parse_mode=HTML + inline_keyboard с web_app кнопкой
      # returns telegram_message_id или None при non-retriable error (403/400 mark-dead)
      # raises RetryAfter для 429, TelegramTransient для 5xx
  ```
- `backend/app/messages/router.py` — handler `GET /messages/{id}` принимает `embed: str | None`, передаёт `embed_tg = (embed == 'tg')` в context.
- `backend/app/auth/router.py` — handler `GET /api/me` дополняет JSON полем `tg_notifications_enabled`.
- `backend/app/auth/router.py` (или новый `users/router.py`) — `PATCH /api/me/settings`.
- `backend/app/audit/service.py` — добавить новые actions в `AuditAction` enum/Literal.

**Pydantic schemas:**
- `backend/app/telegram/schemas.py`:
  ```python
  class TelegramAuthRequest(BaseModel):
      init_data: str = Field(min_length=1, max_length=4096)
  class TelegramAuthResponse(BaseModel):
      linked: bool
      redirect: str
  class ValidatedTelegramUser(BaseModel):
      telegram_user_id: int
      first_name: str | None = None
      username: str | None = None
  ```

### B. Worker (APScheduler)

- `worker/app/tg_notify_dispatch.py` — новый файл с `tg_notify_dispatch` (см. §2.4 псевдокод).
- `worker/app/tg_notify_recovery.py` — новый файл с `tg_notify_recovery_scan` (см. §2.8). Расписание: каждый час.
- `worker/app/sync_cycle.py`:
  - В `save_message` после успешного INSERT messages + apply_tags (т.е. после COMMIT) — `await redis.lpush("tg_notify_queue", json.dumps({"message_id": new_id}))` если `applied_count > 0`.
  - Альтернативно (более чисто): `sync_one_account` собирает list `notified_message_ids` и в конце делает batch LPUSH. Backend-агент выбирает.
- `worker/app/main.py` — зарегистрировать новые jobs в scheduler:
  - `tg_notify_dispatch` — каждые 5 секунд, `max_instances=1, coalesce=True`.
  - `tg_notify_recovery_scan` — каждый час, `max_instances=1, coalesce=True`.

### C. Shared

- `shared/config.py`:
  - Rename `BOT_TOKEN` → `TELEGRAM_BOT_TOKEN` (с алиасом для обратной совместимости в течение 1 спринта; TD-014).
  - Добавить новый env:
    - `TG_NOTIFY_BATCH_SIZE` (default 30).
    - `TG_NOTIFY_DISPATCH_INTERVAL_SEC` (default 5).
    - `TG_NOTIFY_RECOVERY_WINDOW_HOURS` (default 24).
    - `TG_AUTH_INIT_DATA_TTL_SEC` (default 300).
    - `TG_PENDING_COOKIE_TTL_SEC` (default 900 = 15 мин).
  - round-31 (notify-all + троттлинг):
    - `TG_NOTIFY_ALL_MESSAGES: bool = True` — уведомлять обо всех письмах (False = только tagged). Откат без редеплоя.
    - `TG_SEND_PER_CHAT_PER_MINUTE: int = Field(default=20, ge=1, le=60)` — per-chat троттлинг (§2.9).

### D. Database migrations

- Файл `004_telegram_sso_and_notifications.py` (Alembic) — DDL из §3 ADR.

### E. Notification dispatcher contract

```python
# backend/app/telegram/bot.py
async def send_notification(*, chat_id: int, text_html: str, message_id: int) -> SendNotificationResult:
    """
    Returns:
        SendNotificationResult(
            kind: Literal['ok', 'dead', 'retry_after', 'transient'],
            telegram_message_id: int | None,
            retry_after_sec: int | None,
        )
    """
```

### F. Frontend

- `backend/app/static/js/tg.js` — расширить:
  ```javascript
  // На DOMContentLoaded:
  // (существующая логика темы)
  // НОВОЕ: если document.body.dataset.anonymous === '1' (server-rendered marker) И есть initData:
  //   POST /api/telegram/auth {init_data}
  //   при 200 linked=true -> window.location.replace('/')
  //   при 200 linked=false -> backend уже выставил cookie mas_tg_pending; страница останется на /login (server уже отдал login HTML)
  //   при 401 -> страница останется на /login
  ```
- `backend/app/templates/base.html` — `<body class="..." {% if not session %}data-anonymous="1"{% endif %}>`.
- `backend/app/templates/message_view.html` — обернуть `<section class="attachments">` в `{% if not embed_tg %}…{% endif %}`. Поведение mark-read остаётся.

### G. Tests (must-have для QA)

| Сценарий | Уровень | Ожидание |
| --- | --- | --- |
| HMAC валидный `init_data` → 200 linked=true → cookie выставлен | integration | session создан, audit `telegram_link_created` появилась |
| HMAC валидный, но `auth_date` > 5 минут | integration | 401 `init_data_expired` |
| HMAC искажённый (изменён `user`) | integration | 401 `invalid_init_data` |
| `init_data` пустой / отсутствует поле hash | unit | ValidationError |
| 200 linked=false → пользователь логинится → audit `telegram_link_created` с `replaced=false` | integration | telegram_links запись есть |
| Повторный SSO для уже залинкованного user'а | integration | 200 linked=true (без повторного login) |
| Один tg_user_id, два разных login'а подряд (под разными user'ами) | integration | telegram_links upsert обновил user_id; audit `replaced=true` |
| `POST /logout` → telegram_links строка удалена | integration | DELETE сработал; audit `telegram_link_revoked` |
| `POST /api/admin/users/{id}/reset` → linkage сброшена | integration | DELETE + audit |
| `DELETE /api/admin/users/{id}` → linkage каскадно удалена | integration | row отсутствует |
| Message с tags → recipient получает notification | integration | telegram_notifications row + Bot API mock получил sendMessage HTML |
| Recipient без своих тегов на письме | integration | telegram_notifications row НЕ создан |
| Recipient без telegram_links | integration | row НЕ создан |
| Recipient с opt-out (`tg_notifications_enabled=false`) | integration | row НЕ создан |
| Идемпотентность: повторный sync_cycle того же message_id | integration | second notification call → ON CONFLICT, не дублирует Bot API call |
| Bot API 403 → telegram_links.dead_at выставлен | integration | UPDATE + audit `telegram_link_dead_marked`; следующие письма НЕ шлются |
| Bot API 429 → backoff + retry | unit/integration | retry_after уважается; на 2-й 429 — return-to-queue (немедленный re-enqueue, §2.9) |
| Per-chat throttle сработал (try_consume=False) | integration | получатель пропущен; `telegram_notifications` строка НЕ создана; `enqueue_recovery` НЕ вызван для throttled-письма (нет hot re-enqueue, §2.9) |
| Recovery после throttle: throttled-письмо подобрано recovery_scan | integration | recovery_scan находит письмо (per-recipient `NOT EXISTS tn WHERE message_id=m.id AND user_id=u.id`), LPUSH в очередь; на повторном dispatch при освободившемся окне notification доставлена; busy-loop отсутствует (нет re-enqueue каждые 5с) |
| **Частичная доставка (round-33):** письмо видно A и B; A доставлен (строка `(mid,A)` есть), B throttled (строки `(mid,B)` нет) | integration | recovery_scan **возвращает** `mid` (есть visible получатель B без строки) — НЕ теряет B из-за существующей строки A; на повторном dispatch A пропущен (`try_reserve`→None), B доставлен при освободившемся окне |
| **recovery visibility-aware (round-33, TD-025):** письмо без visible залинкованных получателей (нет линковки / opt-out / до `tl.created_at`) | integration | recovery_scan НЕ возвращает `mid` (recipient-JOIN пуст) — нет холостого enqueue |
| Bot API 5xx / network | integration | DELETE telegram_notifications row + LPUSH back to queue; retry на следующем тике |
| sync_cycle не падает при ошибке dispatcher'а | integration | exception в LPUSH ловится, sync_cycle завершает цикл |
| recovery_scan находит upiazz'енное notification | integration | LPUSH добавляет в очередь, dispatcher доставляет |
| `embed=tg` query → attachments секция отсутствует в HTML | unit (template render) | regex / parsing check |
| `tg.js`: cookie `mas_session` присутствует → SSO call НЕ делается | unit (JS) | mock fetch не вызван |

---

## Open questions

| ID | Где задан | Кратко | Статус |
| --- | --- | --- | --- |
| Q-001-1 | этот ADR §1.4 | Нужно ли реализовать anti-replay set (`tg_seen:{auth_date}:{hash[:8]}` в Redis TTL=5min) сверх TTL? Текущее решение: НЕ реализуем в MVP (TTL достаточно для consumer scenario, ~5 ваших пользователей). | open — отложено в `100-known-tech-debt.md` как TD-018 (если откроется реальный риск). |
| Q-002-1 | этот ADR §2.7 | UI toggle для opt-out — отдельная Settings-страница или галка в `/admin` (только super_admin может выключить чужие)? | open — frontend-агенту решить в следующем sprint. Backend endpoint `PATCH /api/me/settings` готов. |
| Q-003-1 | этот ADR §1.3 | Persistent SSO в setup-password flow: что если пользователь открывает бот, попадает на `/set-password` (первый раз), вводит пароль — линковаться ли в этот же момент? Решено: **да**, в `complete_set_password` тоже проверяется `mas_tg_pending` cookie. | closed by this ADR — реализовать. |

---

## Cross-references

- `03-data-model.md` — три новые таблицы (`telegram_links`, `telegram_notifications`, `users_settings`); четыре новые `admin_audit.action`.
- `04-api-contracts.md` — новые `POST /api/telegram/auth`, `PATCH /api/me/settings`; изменения `GET /api/me`, `POST /logout`, `POST /api/admin/users/{id}/reset`, `GET /messages/{id}`.
- `05-modules.md` — расширение модуля 18 (`telegram`) на SSO + dispatcher + bot.send_notification; новый sub-модуль `repositories/telegram_*`, `repositories/user_settings`; изменения в `auth`, `admin`, `worker.sync_cycle`.
- `06-security.md` — новая секция 1.9 (STRIDE для Telegram SSO); §7 — rate-limit `/api/telegram/auth`; §8 — новые audit-actions.
- `07-deployment.md` — env vars (`TG_NOTIFY_*`, `TG_NOTIFY_ALL_MESSAGES`, `TG_SEND_PER_CHAT_PER_MINUTE`, `TG_AUTH_INIT_DATA_TTL_SEC`, `TG_PENDING_COOKIE_TTL_SEC`); cleanup TD-014 (имя `TELEGRAM_BOT_TOKEN`).
- `08-frontend.md` — `tg.js` расширение для SSO call; `message_view.html` — `embed_tg` flag.
- `100-known-tech-debt.md` — закрытие TD-013 (push-notifications) + TD-014 (env var rename); round-31 — TD-025 (recovery без visibility-джойна), TD-026 (нет глобального bot-throttle); round-32 — TD-027 (риск недоставки TG-нотификации за окном 24ч при устойчивом per-chat флуде); round-33 — TD-025 **закрыт** (recovery стал per-recipient + visibility-aware), TD-027 переформулирован (причина — устойчивый throttle).
- ADR-0018 — `INDEX.md` помечает `partially superseded by ADR-0022`. Сам файл ADR-0018 не редактируется (иммутабельны).

---

## История изменений

| round | Дата | Изменение |
| --- | --- | --- |
| round-12 (bug A) | — | Recipient-SQL и теги перешли с per-user (`t.user_id=u.id`) на «у письма есть любой тег» (`EXISTS message_tags`) + теги грузятся один раз на письмо (`list_tags_for_message`). Group-mates лидера снова получают уведомления. |
| round-13 | — | Добавлен `m.internal_date >= tl.created_at` — не флудить историей при первой линковке. |
| round-21 | — | Дедуп тегов в тексте по `(name, color)`. |
| round-31 | 2026-05-26 | (1) Уведомления по ВСЕМ новым письмам под флагом `TG_NOTIFY_ALL_MESSAGES` (default true; §2.1/§2.2/§2.8 — тег-предикат стал условным). (2) Строка тегов в тексте опциональна, плейсхолдер «—» убран, ранний `if not message_tags: return` убран (§2.5). (3) Per-chat троттлинг `TG_SEND_PER_CHAT_PER_MINUTE` через неблокирующий `rate_limit.try_consume` перед `try_reserve`, re-enqueue целого message_id (§2.9). Миграций нет; идемпотентность (§2.3) без изменений. TD-025/TD-026 заведены как follow-up. |
| round-32 | 2026-05-26 | **Busy-loop fix (§2.9).** Throttled-получатель больше НЕ инициирует немедленный hot re-enqueue в `tg_notify_queue` (`enqueue_recovery`) — он просто `continue` (строка `telegram_notifications` не резервируется), а доставку берёт штатный `recovery_scan` (раз в час, окно 24ч) через `NOT EXISTS telegram_notifications`. Это убирает busy-loop при устойчивом `inflow > per-chat cap` (раньше письмо ре-энквьюилось каждые 5с бесконечно → неограниченный рост очереди + амплификация recipient-SQL). `retry_after`/`transient` (429/5xx) по-прежнему идут через немедленный `enqueue_recovery` (кратковременная, не структурная задержка — busy-loop не возникает). Обновлены §2.4 (контракт dispatch), §2.9. Заведён TD-027 (риск недоставки TG-нотификации за окном 24ч при устойчивом флуде — осознанный компромисс, само письмо не теряется). Миграций нет. |
| round-33 | 2026-05-26 | **CRITICAL fix: recovery per-recipient (§2.8/§2.9).** Прежний `recovery_scan` отбирал письма по per-**message** `NOT EXISTS (telegram_notifications WHERE message_id=m.id)`. При частичной доставке (письмо видно A и B; A доставлен → строка `(mid,A)` есть; B throttled → строки `(mid,B)` нет) `NOT EXISTS` возвращал FALSE из-за строки A → `mid` не подбирался → B терял уведомление **навсегда**. Это делало путь throttled→recovery (§2.9) недостижимым для частично-доставленных писем и активировало pre-existing хрупкость частичной доставки при крашах. Исправлено (Опция A, без миграции): recovery-SQL теперь переиспользует recipient-логику §2.2 (visibility/link/opt-out/`internal_date` + условный тег-предикат) и отбирает `DISTINCT m.id`, у которых есть видимый получатель без строки `(message_id,user_id)` (`NOT EXISTS tn WHERE message_id=m.id AND user_id=u.id`). Побочно: recovery стал visibility-aware → **TD-025 закрыт**; TD-027 переформулирован (причина недоставки за 24ч — устойчивый throttle, а не маскированная частичная доставка). Идемпотентность (`try_reserve`) и hot-path `retry_after`/`transient` не тронуты. Обновлены §2.8 (новый recovery-SQL), §2.9 (механизм throttled→recovery), `05-modules.md` §14.1. Миграций нет. |

