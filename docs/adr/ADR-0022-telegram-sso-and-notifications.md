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

После успешного COMMIT (т.е. message INSERTed и tags applied) `save_message` собирает результат вызова `apply_tags_to_message` — он возвращает `applied_count: int`. Если `applied_count > 0` — функция (а точнее sync-cycle wrapper) добавляет в **in-memory очередь** одной итерации `(message_id, mail_account_id)` и продолжает.

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

Для каждого message_id, попавшего в очередь, dispatcher определяет получателей:

```sql
-- Псевдо-SQL получателей одного сообщения (ID = :mid):
SELECT DISTINCT u.id AS user_id, tl.telegram_user_id, ma.group_id AS message_group_id
FROM messages m
JOIN mail_accounts ma ON ma.id = m.mail_account_id
JOIN users u
  ON (
      u.role = 'super_admin'                      -- super_admin видит всё
      OR u.group_id = ma.group_id                 -- участники группы по mail_accounts.group_id (round-10 patch)
      OR u.id = ma.user_id                        -- владелец аккаунта (на случай super_admin-owned аккаунта вне группы)
  )
JOIN telegram_links tl
  ON tl.user_id = u.id
  AND tl.dead_at IS NULL                          -- мёртвые линковки исключаются
JOIN message_tags mt ON mt.message_id = m.id
JOIN tags t ON t.id = mt.tag_id AND t.user_id = u.id   -- у получателя ЕСТЬ свой тег на этом письме
LEFT JOIN users_settings us ON us.user_id = u.id       -- см. §2.6 opt-out
WHERE m.id = :mid
  AND COALESCE(us.tg_notifications_enabled, true) = true   -- default-on
GROUP BY u.id, tl.telegram_user_id, ma.group_id;
```

Затем для каждого получателя — повторный SQL для получения **именно его тегов** на этом письме (нужно для текста уведомления):

```sql
SELECT t.id, t.name, t.color
FROM message_tags mt
JOIN tags t ON t.id = mt.tag_id
WHERE mt.message_id = :mid AND t.user_id = :recipient_uid
ORDER BY t.name;
```

**Инварианты получателей:**
- Получатель должен иметь активную (`dead_at IS NULL`) `telegram_links` запись.
- У получателя должен быть **свой собственный тег** на этом сообщении (т.к. теги per-user; ADR-0017). Если у user'а X на message Y нет ни одного `message_tags` с `tag.user_id=X` — уведомление **НЕ шлём**. Прямое следствие требования «Оповещение только о сообщениях в которых присутствует тег» — тег per-user, значит и проверка тоже per-user.
- Получатель не должен быть в opt-out (`users_settings.tg_notifications_enabled = false`; default — true).

**Замечание про super_admin и приватные ящики:** super_admin'у уведомления приходят про **все** письма с тегами (включая письма из ящиков чужих групп). Это согласуется с visibility-моделью (ADR-0019 §7.1 — super_admin видит всё). Если super_admin не хочет быть завален — он выключает opt-out для себя (`tg_notifications_enabled=false`).

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

async def dispatch_one(message_id: int) -> None:
    # 1. Загрузить recipients (SQL из §2.2)
    # 2. Загрузить контекст письма (mail_account display_name|email, from_addr, from_name) — один SQL
    # 3. Для каждого recipient:
    #    a. INSERT INTO telegram_notifications ... ON CONFLICT DO NOTHING RETURNING id
    #    b. if RETURNING empty -> continue
    #    c. Сформировать текст (см. §2.5) на тегах recipient'а
    #    d. await bot.send_notification(chat_id=tl.telegram_user_id, text=..., message_id=mid)
    #    e. UPDATE telegram_notifications SET telegram_message_id=..., sent_at=now() WHERE id=...
    #    f. при 403/400 -> mark telegram_links.dead_at + audit + НЕ delete row из telegram_notifications
    #    g. при 429 -> sleep(retry_after) и retry того же recipient'а (внутри dispatch_one, max 1 retry); если 2-й 429 — leave-in-queue: LPUSH обратно в tg_notify_queue {message_id, only_for_user_id:uid}, потеряется max 5s
    #    h. при network/5xx -> DELETE row, LPUSH обратно (full message)
```

**Ограничения:**
- `NOTIFY_BATCH_SIZE = 30` — один тик обрабатывает до 30 messages × N recipients ≈ 30–150 Bot API calls. На частоте 5 сек = ~6/sec баланс — намного ниже 30 msg/sec лимита (есть запас на retry).
- Backoff на 429: dispatcher уважает `parameters.retry_after`, спит, потом продолжает.
- `bot.send_notification` (новый метод, не путать с существующим `send_message_with_webapp_button`) — добавляется в `backend/app/telegram/bot.py`.

#### 2.5. Формат уведомления

**Markup mode:** **HTML** (а не MarkdownV2). Обоснование:
- MarkdownV2 требует escape ~15 символов (`_*[]()~\`>#+-=|{}.!`), легко ломается при наличии этих символов в `from_addr`, `subject`, `display_name`, `tag.name` (а имена тегов кириллические + могут содержать спецсимволы — см. ADR-0017 builtin `DPLA.PLA`, который содержит точку).
- HTML требует escape только трёх: `<`, `>`, `&` — стандартная `html.escape()` решает.
- В HTML mode легко выделять жирным (`<b>`) — нужно для имени почты и тегов.
- Inline-keyboard поддерживается одинаково в обоих форматах.

Шаблон (новый Jinja2 template **не нужен** — формирование текста в Python, т.к. это не HTML-страница, а Bot API HTML subset):

```python
def format_notification(
    *,
    acc_label: str,        # display_name or email
    from_label: str,       # from_name or from_addr
    tag_names: list[str],  # сортировано
) -> str:
    """
    Возвращает HTML-строку для sendMessage parse_mode=HTML.
    Все user-controlled значения экранируются через html.escape().
    """
    acc_safe = html.escape(acc_label)
    from_safe = html.escape(from_label)
    if len(tag_names) == 1:
        tag_line = f'Тег &laquo;<b>{html.escape(tag_names[0])}</b>&raquo;'
    else:
        names = ', '.join(f'&laquo;<b>{html.escape(t)}</b>&raquo;' for t in tag_names)
        tag_line = f'Теги {names}'
    return (
        f'Вы получили письмо на почту <b>{acc_safe}</b>\n'
        f'{tag_line}\n'
        f'Отправитель <b>{from_safe}</b>'
    )
```

Пример:
```
Вы получили письмо на почту <b>support@example.com</b>
Тег «<b>DPLA.PLA</b>»
Отправитель <b>sender@gmail.com</b>
```

или (множественные теги):
```
Вы получили письмо на почту <b>Apple Test 1</b>
Теги «<b>Диспут</b>», «<b>Отменить подписку</b>»
Отправитель <b>AppStoreNotices@apple.com</b>
```

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

#### 2.8. Recovery после crash worker или Redis

Если worker крашится между LPUSH (в sync_cycle) и LPOP (в dispatcher) — item теряется. Это **не критично**, потому что:
- recovery_scan (новый APScheduler job, раз в час) выполняет:
  ```sql
  SELECT m.id
  FROM messages m
  WHERE m.fetched_at > now() - interval '24 hours'
    AND EXISTS (SELECT 1 FROM message_tags mt WHERE mt.message_id = m.id)
    AND NOT EXISTS (SELECT 1 FROM telegram_notifications tn WHERE tn.message_id = m.id)
  ORDER BY m.id;
  ```
- Для каждого найденного message — LPUSH в `tg_notify_queue`.
- Окно 24 часа покрывает выходные / multi-hour outages. Старше 24 часов — намеренно не нотифицируем (письмо устарело, спам по факту).

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
      async def try_claim(message_id: int, user_id: int) -> int | None  # INSERT ... ON CONFLICT DO NOTHING RETURNING id; None если уже было
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
| Bot API 429 → backoff + retry | unit/integration | retry_after уважается; на 2-й 429 — return-to-queue |
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
- `07-deployment.md` — env vars (`TG_NOTIFY_*`, `TG_AUTH_INIT_DATA_TTL_SEC`, `TG_PENDING_COOKIE_TTL_SEC`); cleanup TD-014 (имя `TELEGRAM_BOT_TOKEN`).
- `08-frontend.md` — `tg.js` расширение для SSO call; `message_view.html` — `embed_tg` flag.
- `100-known-tech-debt.md` — закрытие TD-013 (push-notifications) + TD-014 (env var rename).
- ADR-0018 — `INDEX.md` помечает `partially superseded by ADR-0022`. Сам файл ADR-0018 не редактируется (иммутабельны).

