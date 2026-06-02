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
| Side effects | (a) при успехе и существующей линковке — Set-Cookie `mas_session`/`mas_csrf` для линкованного user'а; (b) при успехе без линковки **и без активной сессии** — устанавливает короткоживущий cookie `mas_tg_pending` (см. §1.3 шаг 5); (c) **round-38 (self-heal):** при успехе и наличии активной сессии (`mas_session`) — idempotent upsert `telegram_links(tg → current_session.user_id)`, **без** создания второй сессии и **без** `mas_tg_pending` (см. §1.6). |

> **round-38 — расширение семантики эндпоинта.** Изначально (round-13) эндпоинт обслуживал только анонимный вход. С round-38 он дополнительно обслуживает **самовосстановление привязки** для уже залогиненного пользователя (ветка (c) выше, §1.6). Решение «расширить существующий эндпоинт, а не вводить новый `POST /api/telegram/ensure-link`» обосновано в §1.6. `POST /api/telegram/links` (ADR-0024 §4 — явное добавление TG из настроек) **остаётся** отдельным: у него другой UX-контракт (CSRF cookie-form, surفacing 409 `tg_link_owned_by_other`/`tg_link_limit` в UI), тогда как self-heal — «тихий» idempotent best-effort на каждом открытии WebApp.

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
            Note over BE,DB: _link(): если привязка УЖЕ ЖИВАЯ на того же user (dead_at IS NULL) → NO-OP (created_at не сдвигается, §1.6 edge-3); иначе upsert ниже
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

**round-38 (self-heal) — изменение шага 1 ниже:** условие `tg.js` больше **не** требует отсутствия сессии. Если `Telegram.WebApp.initData` непустая — POST `/api/telegram/auth` делается **всегда** (и для анонима, и для залогиненного). Backend по наличию `mas_session` сам выбирает ветку: аноним → SSO (как было); залогинен → self-heal upsert привязки (§1.6). Это устраняет баг «залогинен в WebApp, но привязка не пересоздаётся → уведомления не идут». Исторический текст шага 1 (anonymous-only gate) ниже сохранён как описание прежнего поведения; актуальная логика — в §1.6 и в обновлённом блоке кода `tg.js`.

**Ключевые моменты flow:**

1. ~~`tg.js` дополняется: на DOMContentLoaded, если `Telegram.WebApp.initData` непустая И отсутствует cookie `mas_session`~~ (round-38: **gate `isAnonymous` снят** — POST делается при любой непустой `initData`; определение «аноним vs залогинен» перенесено на backend). Прежняя формулировка: на DOMContentLoaded, если `Telegram.WebApp.initData` непустая И отсутствует cookie `mas_session` (определяется через GET `/api/me` → 401, либо через server-rendered hint в HTML), — делает POST `/api/telegram/auth`. Логика (round-38, обновлена):

   ```text
   // round-38: gate `isAnonymous` СНЯТ. POST делается при любой непустой initData,
   // в т.ч. для уже залогиненного пользователя — backend по mas_session выберет ветку.
   // __masTgSsoTried — guard от повторных вызовов (HMR / повторный DOMContentLoaded),
   // выставляется ДО fetch.
   if (window.Telegram?.WebApp?.initData && !window.__masTgSsoTried) {
     window.__masTgSsoTried = true;
     fetch("/api/telegram/auth", {method:"POST", headers:{"Content-Type":"application/json"},
           credentials:"same-origin",
           body: JSON.stringify({init_data: Telegram.WebApp.initData})})
       .then(r => r.json().then(j => ({status: r.status, body: j})).catch(() => ({status: r.status, body: null})))
       .then(({status, body}) => {
          if (status !== 200 || !body) return;       // 401/429/5xx → тихо остаёмся на странице
          if (body.linked === true && body.redirect) {
            // Аноним → backend выпустил mas_session; перезагружаемся в приложение.
            window.location.replace(body.redirect);
          }
          // body.linked === false:
          //   - аноним без привязки: backend выставил mas_tg_pending → остаёмся на /login.
          //   - залогинен (self-heal, §1.6): backend сделал upsert привязки и вернул
          //     {linked:false, healed:true} БЕЗ redirect и БЕЗ mas_tg_pending → НЕ перезагружаемся,
          //     пользователь остаётся на текущей странице (привязка восстановлена молча).
       });
   }
   ```

   **Замечание (round-38):** для залогиненного пользователя backend возвращает `{linked:false, healed:true}` **без** `redirect` — фронт по контракту перезагружается только при `linked===true && redirect`, поэтому self-heal не вызывает нежелательного reload. `mas_tg_pending` для залогиненной ветки **не** выставляется (см. §1.6 и §1.2 side-effect (c)).

   Простейшая реализация — на корневой странице `/` (анонимный GET возвращает HTML `/login`-формы по существующему механизму ADR-0016, см. `04-api-contracts.md`). Если в HTML `<body data-anonymous>` — `tg.js` запускает SSO. Детали инструкции для frontend-агента в этом же ADR (Implementation plan §F).

2. **Logout сбрасывает линковку.** В `auth.AuthService.logout` добавляется (в той же транзакции, что и revoke session): `DELETE FROM telegram_links WHERE user_id=:uid`. Это явное требование пользователя — «Если пользователь выходит из аккаунта, то мы сбрасываем напоминание».

3. **Линковка создаётся только после успешного `POST /login/password`** (step-2). На `/set-password` flow (первый логин с временным паролем) — после успешного `POST /set-password` также проверяется cookie `mas_tg_pending` и создаётся линковка. Это покрывает случай «пользователь созданный super-admin'ом первый раз заходит через бот».

4. **(round-38, пересмотрено) Пользователь с уже открытой сессией** теперь **тоже** триггерит POST `/api/telegram/auth` — но не для смены сессии, а для **self-heal привязки** (§1.6). Прежняя формулировка («если `mas_session` уже есть, `tg.js` не делает SSO call») **отменена**: именно она и была причиной бага — после случайного/любого logout привязка не пересоздавалась, потому что при следующем заходе пользователь уже был залогинен и SSO-call не делался. С round-38 self-heal upsert выполняется при **каждом** открытии WebApp с валидной `initData`, поэтому привязка «самовосстанавливается». Замечание о возможной несинхронизации cookies между Telegram WebView и системным WebView остаётся в силе, но более не блокирует восстановление привязки.

#### 1.4. Безопасность

| Угроза | Митигация |
| --- | --- |
| Подмена `init_data` злоумышленником с украденным `TELEGRAM_BOT_TOKEN` | `init_data` не приносит auth в одиночку — только если уже есть `telegram_links` запись. Атакующий, знающий tg_user_id и bot-token, может выпустить себе сессию **только** существующего залинкованного user'а. Mitigation: bot-token строго в env + redact-list (ADR-0014). При компрометации — revoke `telegram_links` (массовый DELETE) + ротация bot-token. |
| Replay украденного `init_data` | TTL 5 минут (см. выше). Дополнительно: можно (опционально, low-priority) хранить short-set `tg_seen:{auth_date}:{hash[:8]}` в Redis с TTL=5min для anti-replay внутри окна — **НЕ реализуется** в MVP (рассматриваем как future hardening; добавлено как сравнение с industry-best-practice). |
| Подмена `telegram_user_id` в `init_data` | Невозможна — `user` поле подписано HMAC'ом. Любая мутация ломает hash. |
| Brute-force HMAC (попытка подобрать hash без bot-token) | Rate-limit `30/min per IP` отсекает; HMAC-SHA256 неразрешим без ключа. |
| Один tg-user логинится под двумя разными аккаунтами поочерёдно | `INSERT … ON CONFLICT (telegram_user_id) DO UPDATE SET user_id=EXCLUDED.user_id, created_at=now(), dead_at=NULL` — последняя успешная пара логин-пароль перезаписывает линковку (rebound). Audit-запись `telegram_link_rebound` фиксирует факт. (round-38 critical-fix: повтор того же логина при **уже-живой** привязке того же user — NO-OP, `created_at` не сдвигается; см. §1.6 edge-3.) |
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

#### 1.6. Self-heal привязки при открытии WebApp залогиненным пользователем (round-38)

##### Постановка проблемы (баг)

`telegram_links` создаётся только в двух точках: (а) login-flow (pending-cookie после ввода пароля, §1.3) и (б) явное `POST /api/telegram/links` из настроек (ADR-0024 §4). При этом:

- `logout` (`revoke_for_user(reason="logout")` → `delete_all_by_user_id`) удаляет **все** привязки user'а (ADR-0024 §5) — это **сохраняется как есть** (явный выбор пользователя «Выйти» рвёт привязку без подтверждения).
- Frontend `tg.js` (до round-38) делал `POST /api/telegram/auth` **только** при `data-anonymous="1"` (нет валидной `mas_session`).

Следствие-баг: пользователь, у которого `mas_session` есть, но `telegram_links` отсутствует (например, был удалён прошлым logout, или истёк по любой причине), открывает WebApp в Telegram, видит себя залогиненным — но SSO-call **не** делается, привязка **не** пересоздаётся, и push-уведомления **не приходят**.

##### Требование пользователя (дословно)

«При логине в webapp сохранять id телеграм аккаунта и слать ему уведомления, до тех пор пока пользователь сам не выйдет из webapp аккаунта, нажав на кнопку Выйти». Семантика «Выйти» = logout рвёт привязку (без подтверждения) — **не меняется**. Цель: при **каждом** открытии WebApp внутри Telegram (есть валидная `initData`) для текущего пользователя привязка создаётся/обновляется (upsert), даже если пользователь уже залогинен. Тогда после любого logout следующее открытие WebApp в Telegram автоматически вернёт привязку и уведомления.

##### Решение — расширить `POST /api/telegram/auth` (а не вводить новый endpoint)

Рассмотрены два варианта:

| Критерий | (A) расширить `POST /api/telegram/auth` веткой «есть сессия → upsert» | (B) новый `POST /api/telegram/ensure-link` |
| --- | --- | --- |
| Кол-во endpoint'ов / поверхность атаки | без новых; одна точка валидации `initData` | +1 публичный endpoint, дубль HMAC-валидации |
| Frontend | один fetch на той же странице; ветвление по ответу | два fetch'а (auth для анонима, ensure-link для залогиненного) → фронту нужно знать, залогинен ли он (а `mas_session` HttpOnly — пришлось бы тянуть `data-anonymous`) |
| Семантика | «доказать владение TG + привязать к текущему контексту»: анонимный контекст → SSO, залогиненный контекст → self-heal. Единый смысл, ветвление по серверной сессии | расщепление одного смысла на два endpoint'а |
| CSRF | `/api/telegram/auth` уже CSRF-exempt (защита — HMAC `initData`); self-heal остаётся exempt по той же причине | новый endpoint тоже придётся делать CSRF-exempt → дубль исключения |
| Rate-limit | переиспользуются `LIMIT_TG_AUTH_IP` (30/min IP) + `LIMIT_TG_AUTH_USER` (10/min tg_user_id) | новые бакеты |

**Выбран вариант A.** `POST /api/telegram/auth` становится единой точкой «доказательство владения Telegram-аккаунтом (HMAC initData) → привязка к текущему контексту»; ветвление — по наличию валидной `mas_session`, которое определяет **backend** (не frontend). Это и устраняет первопричину бага: фронт не должен решать, что делать, на основе HttpOnly-cookie, которую он не видит.

`POST /api/telegram/links` (ADR-0024 §4) **остаётся** отдельным и не дублирует self-heal: его задача — явное «добавить ещё один TG из настроек» с CSRF-cookie-form контрактом и видимыми в UI ошибками `tg_link_owned_by_other` / `tg_link_limit`. Self-heal же — «тихий best-effort» при каждом открытии WebApp.

##### Алгоритм backend (`POST /api/telegram/auth`, round-38)

```
1. rate-limit per IP (LIMIT_TG_AUTH_IP).
2. parse + HMAC-validate init_data (как раньше; 401 invalid_init_data / init_data_expired при провале).
3. rate-limit per telegram_user_id (LIMIT_TG_AUTH_USER, после HMAC).
4. РАЗРЕШИТЬ текущую сессию: прочитать mas_session (request.state.session, если SessionMiddleware его поставил).
   - НЕТ валидной сессии  → СУЩЕСТВУЮЩАЯ логика (ветки linked / unlinked, §1.3). Без изменений.
   - ЕСТЬ валидная сессия (current_user_id) → ВЕТКА SELF-HEAL:
       a. idempotent привязка telegram_links(telegram_user_id → current_user_id) через _link(...)
          с rebind-разрешением (см. edge «rebound»). ВАЖНО (critical-fix, edge-3):
          для уже-живой привязки того же user (user_id=current AND dead_at IS NULL) — ПОЛНЫЙ NO-OP
          (строка не трогается, created_at не сдвигается, audit не пишется). created_at=now()
          ставится ТОЛЬКО при реальном восстановлении: INSERT новой / реактивация dead / rebound.
       b. НЕ создавать вторую сессию; НЕ трогать mas_session/mas_csrf cookies.
       c. НЕ выставлять mas_tg_pending.
       d. вернуть 200 {"linked": false, "healed": true}  (без redirect — фронт не перезагружается).
```

Ветка self-heal вызывает выделенный метод сервиса `TelegramSSOService.self_heal_link(telegram_user_id, user_id=current_user_id, ip, ua)` (реальный класс — `backend/app/telegram/sso_service.py::TelegramSSOService`). По логике связывания это **тот же** `_link(...)`, что и `link_pending`, **с `allow_rebind_from_other=True`** (обоснование — edge «rebound» ниже), `via="self_heal"`. Метод никогда не поднимает исключение наружу (при лимите — audit + no-op), потому что self-heal — best-effort и не должен ломать открытие WebApp.

**Critical-fix (round-38, edge-3):** общий `_link(...)` для ветки «привязка уже есть на текущего user» делает upsert **условно**: если строка живая (`dead_at IS NULL`) — это **полный NO-OP** (без upsert, без audit); upsert (с `created_at=now()`, `dead_at=NULL`) выполняется **только** при реактивации `dead`. Это правило действует для **всех трёх** entry-point'ов (`link_pending`/`login_flow`, `link_session_add`/`session_add`, `self_heal_link`/`self_heal`) единообразно — см. edge-3 и §1 (anti-defect: безусловный сдвиг `created_at` терял письма из окна между upsert'ами).

##### Sequence (round-38)

```mermaid
sequenceDiagram
    autonumber
    participant U as User (залогинен)
    participant TG as Telegram WebApp
    participant FE as Browser/WebView
    participant BE as FastAPI backend
    participant DB as PostgreSQL

    U->>TG: открывает WebApp (бот) — есть валидная initData
    TG->>FE: open WebView URL (с cookie mas_session)
    FE->>FE: tg.js видит initData, __masTgSsoTried=false
    FE->>BE: POST /api/telegram/auth {init_data}  (cookie mas_session отправлен)
    BE->>BE: rate-limit IP; HMAC-validate initData; rate-limit tg_user_id
    BE->>BE: resolve mas_session -> current_user_id (валидна)
    Note over BE: ВЕТКА SELF-HEAL (есть сессия)
    BE->>DB: get_by_telegram_user_id(tid)
    alt линк отсутствует
        BE->>DB: COUNT(active) < TG_MAX_LINKS_PER_USER ? upsert(tid -> current_user_id) : no-op
        BE->>BE: audit telegram_link_created (via=self_heal) | telegram_link_limit_reached
    else линк на ТЕКУЩЕГО user, dead_at IS NULL (живой)
        Note over BE,DB: NO-OP — строку НЕ трогаем (created_at сохраняется), audit НЕ пишем
    else линк на ТЕКУЩЕГО user, dead_at IS NOT NULL (реактивация)
        BE->>DB: upsert (dead_at=NULL, created_at=now())
        BE->>BE: audit telegram_link_created replaced=true (via=self_heal)
    else линк указывает на ДРУГОГО user
        BE->>DB: upsert (rebound -> current_user_id)
        BE->>BE: audit telegram_link_rebound (via=self_heal)
    end
    BE-->>FE: 200 {"linked": false, "healed": true}  (НЕТ Set-Cookie, НЕТ redirect)
    FE->>FE: linked!==true -> НЕ перезагружается; пользователь остаётся на странице
    Note over U,DB: привязка восстановлена; следующее письмо -> push приходит
```

##### Edge cases (round-38)

1. **`telegram_user_id` из initData уже привязан к ДРУГОМУ user'у (rebound).** initData подписан bot-token и доказывает, что текущий браузер открыт **внутри этого Telegram-аккаунта**; пользователь явно залогинен под своим internal-аккаунтом. Логично привязать TG к текущему залогиненному user'у → **rebound разрешён** (`allow_rebind_from_other=True`, как в login-flow `link_pending`). Upsert по PK `telegram_user_id` атомарно переносит привязку. Audit: `telegram_link_rebound` с `details={telegram_user_id, previous_user_id, via:"self_heal"}`. Безопасность: rebound невозможен без валидной initData этого TG (доказательство владения) **И** валидной сессии целевого user'а — оба фактора обязательны.

2. **Лимит `TG_MAX_LINKS_PER_USER` (default 10).** Self-heal идемпотентен по PK `telegram_user_id`: повторное открытие **того же** TG дубля не создаёт (для уже-живой привязки — NO-OP, см. edge-3; счётчик не растёт). Лимит срабатывает только когда у user уже N≥limit **разных** живых TG, а initData принадлежит **(N+1)-му** новому TG. Тогда: audit `telegram_link_limit_reached`, привязка **не** создаётся, endpoint всё равно возвращает `200 {"linked":false,"healed":true}` (self-heal — best-effort, не ошибка для пользователя; UI открывается нормально). Это «soft limit» — отказ тихий, без 409 (в отличие от явного `POST /api/telegram/links`, где 409 нужен для UI настроек).

3. **`created_at` — обновляется ТОЛЬКО при реальном восстановлении; для уже-живой привязки того же user → NO-OP (round-38, critical-fix).**

   Recipient-SQL и recovery фильтруют `m.internal_date >= tl.created_at` (§2.2 / §2.6, round-13: не флудить историей при первой линковке). Self-heal выполняется при **каждом** открытии WebApp (gate `data-anonymous` снят, §1.2). Поэтому **безусловный** `created_at=now()` был бы регрессией доставки: для нормально работающего активного пользователя `created_at` сдвигался бы вперёд на каждом заходе, и письма, пришедшие **в интервале между двумя открытиями WebApp**, навсегда выпадали бы из push (их `internal_date < tl.created_at` после сдвига). Это недопустимо.

   **Правило (применяется единообразно ко ВСЕМ точкам upsert — `login_flow`, `session_add`, `self_heal`, см. §1 и таблицу ниже):** `created_at` (а также `dead_at`, `user_id`) перезаписывается **только** когда привязка реально меняет состояние:

   | Состояние существующей строки (по PK `telegram_user_id`) | Действие | `created_at` | audit |
   | --- | --- | --- | --- |
   | строки нет | INSERT | `=now()` | `telegram_link_created` (`replaced=false`) |
   | есть, `user_id = current`, `dead_at IS NULL` (живая, тот же user) | **NO-OP** — строка не трогается | **не меняется** | **нет** (no-op не пишет audit — устраняет спам `replaced=true`) |
   | есть, `user_id = current`, `dead_at IS NOT NULL` (dead, реактивация) | UPDATE: `dead_at=NULL`, `created_at=now()` | `=now()` | `telegram_link_created` (`replaced=true`) |
   | есть, `user_id ≠ current` (rebound) | UPDATE: `user_id=current`, `dead_at=NULL`, `created_at=now()` | `=now()` | `telegram_link_rebound` |

   **Семантика для self-heal:**
   - *Реактивация / rebound / новая привязка* → `created_at=now()`: push получает **только** письма, пришедшие ПОСЛЕ восстановления; накопленные в окне «dead/нет привязки … повторный заход» push'ом не дублируются (они доступны в Inbox). Корректно — self-heal реактивирует «вперёд».
   - *Живая привязка того же user* → **полный NO-OP**: `created_at` сохраняется, окно доставки не сдвигается, письма из интервала между заходами **не теряются**. Это и есть исправление critical-дефекта.

   **Корректность правила для всех трёх callers:** INSERT первичной привязки обязан ставить `created_at=now()` (база окна доставки); реактивация dead и rebound обязаны ставить `created_at=now()` (новый владелец/возобновление не должны получить чужую/старую историю); живая привязка того же user **не должна** трогаться ни в одном из flow — в `login_flow`/`session_add` повторный upsert живой строки точно так же бессмысленно сдвигал бы окно. Поэтому no-op-для-живой-строки корректен **единообразно** и не ломает ни один существующий сценарий.

   **Реализация (точный SQL для backend).** `repo.upsert(...)` остаётся примитивом для трёх «пишущих» веток (INSERT / реактивация / rebound) — он по-прежнему делает `ON CONFLICT (telegram_user_id) DO UPDATE SET user_id=EXCLUDED.user_id, created_at=now(), dead_at=NULL`. Ветвление «писать или no-op» выносится в сервисный слой `_link(...)` на основе уже выполняемого `SELECT` (`get_by_telegram_user_id`), что даёт точный контроль и сохраняет audit-семантику:

   ```python
   existing = await repo.get_by_telegram_user_id(telegram_user_id)

   # rebound: привязка на ДРУГОГО user'а
   if existing is not None and existing.user_id != user_id:
       if not allow_rebind_from_other: raise TelegramLinkOwnedByOtherError
       await repo.upsert(telegram_user_id, user_id)         # created_at=now(), dead_at=NULL, user_id=current
       audit telegram_link_rebound; return

   # тот же user
   if existing is not None and existing.user_id == user_id:
       if existing.dead_at is None:
           return                                            # NO-OP: живая привязка — НЕ трогаем строку, НЕ пишем audit
       await repo.upsert(telegram_user_id, user_id)          # реактивация dead: created_at=now(), dead_at=NULL
       audit telegram_link_created (replaced=true); return

   # привязки нет → soft-limit, затем INSERT
   ...
   await repo.upsert(telegram_user_id, user_id)              # created_at=now() (INSERT)
   audit telegram_link_created (replaced=false)
   ```

   Эквивалентный однооператорный вариант (если предпочтительна атомарность без предварительного SELECT) — условный `ON CONFLICT … DO UPDATE … WHERE telegram_links.dead_at IS NOT NULL OR telegram_links.user_id <> EXCLUDED.user_id` (PostgreSQL: предикат в `WHERE` индекс-конфликта пропускает UPDATE для живой строки того же user → строка не меняется, `created_at` сохраняется). Backend-агент выбирает между двумя формами; обе обязаны давать NO-OP для (`user_id = current`, `dead_at IS NULL`). Семантику ответа endpoint'а это не меняет: см. правку edge-2/ответа ниже (`healed:true` и для no-op, и для реального восстановления — пользователю это неотличимо и не должно отличаться).

4. **`dead_at` при upsert сбрасывается в `NULL` (реактивация).** Если привязка была помечена `dead_at` (Bot API 403 — пользователь когда-то блокировал бота, потом разблокировал и снова открыл WebApp) — self-heal upsert обнуляет `dead_at`, чат снова становится получателем. Корректно.

5. **initData валидна, но `current_user_id` указывает на удалённый/отсутствующий user.** Не может случиться в self-heal ветке: сессия резолвится из Redis в живого user'а (middleware при отсутствии user'а уже разлогинивает, см. admin-модуль edge). Если всё же сессия «висит» (race) — upsert FK `user_id → users(id)` упадёт; ошибка проглатывается self-heal'ом (best-effort), endpoint вернёт `200 healed:false` нельзя — см. ниже: при внутренней ошибке self-heal возвращаем `200 {"linked":false,"healed":false}` и логируем `telegram_self_heal_failed`; фронт по контракту не перезагружается. (Практически — крайне редкий race; не блокирует UX.)

##### Безопасность (round-38)

| Угроза | Митигация |
| --- | --- |
| Привязка чужого TG к своей сессии без доказательства владения | Невозможна: self-heal требует **И** валидную `initData` этого TG (HMAC bot-token доказывает, что браузер открыт внутри данного Telegram-аккаунта) **И** валидную `mas_session`. Нельзя «принести» чужую initData без bot-token. |
| Привязка своей сессии к чужому TG (атакующий хочет получать чужие уведомления) | Получателем уведомлений становится **тот** chat (`telegram_user_id`), чья initData подписана — т.е. атакующий привяжет к своей сессии **свой собственный** TG. Ничего нового не получает. |
| Replay украденной initData | TTL 5 минут (как в SSO §1.2/§1.4) применяется и к self-heal — та же `verify_init_data`. |
| CSRF | self-heal остаётся в CSRF-exempt списке `/api/telegram/auth` — защита строится на HMAC initData, не на CSRF-токене; при этом self-heal **не** выполняет привилегированных мутаций над сессией (не создаёт/не удаляет сессию), только upsert привязки текущего user'а к доказанному TG. |
| DOS / rate-limit | `LIMIT_TG_AUTH_IP` (30/min IP) + `LIMIT_TG_AUTH_USER` (10/min tg_user_id) — переиспользуются (self-heal проходит те же бакеты, что и SSO). |
| Спам audit-записей | **Устранено в round-38 (edge-3 critical-fix):** для уже-живой привязки того же user self-heal — полный NO-OP, audit `telegram_link_created replaced=true` **не** пишется. Audit пишется только при реальном изменении состояния (INSERT новой / реактивация dead / rebound) — это редкие события, объём минимален. |

##### Что НЕ меняется

- logout / reset-password по-прежнему рвут **все** привязки (`revoke_for_user`); это явный выбор пользователя «Выйти».
- SSO-ветка для анонима (§1.3) — без изменений.
- `POST /api/telegram/links` (ADR-0024 §4) — без изменений.
- DDL `telegram_links`, миграции — **без изменений** (self-heal переиспользует существующий `repo.upsert` как write-примитив; новое — только условие NO-OP в сервисном `_link(...)`, без схемных изменений).

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

При `TG_NOTIFY_ALL_MESSAGES=true` этот запрос для письма без тегов вернёт пустой список — это нормальный кейс (см. §2.5: строка `#️⃣:` тогда показывает «Не сортировано»), уведомление всё равно шлётся.

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
    #                                            # обрезать до PREVIEW_LEN=100 (round-36; +'…' если длиннее), '' если пусто
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

Bug-fix #4: Telegram `parse_mode=HTML` **не** декодирует HTML-entities (`&laquo;`/`&raquo;`) — пользователь увидел бы их буквально. Используем реальные UTF-8 символы (кавычки `«` `»`, emoji).

**Round-36: НОВЫЙ формат уведомления (emoji-заголовки, обязательные строки тег/тема).** Формат переработан под продуктовое требование: уведомление о новом письме теперь читается как карточка с emoji-метками. Историческая часть:
- round-31 ОПЦИОНАЛЬНАЯ строка тегов («не печатается, если тегов нет») → **заменена** на **всегда-присутствующую** строку `#️⃣:` с fallback **«Не сортировано»**;
- round-34 превью тела **120 → 100** символов (`PREVIEW_LEN`);
- строка «Отправитель» → **«Клиент:»**;
- строка «Тема:» теперь **всегда** присутствует, при пустой теме fallback **«(без темы)»** (раньше — round-34 — строка опускалась).

**Структура (round-36) — 6 строк, 2 из них — пустые-разделители:**

1. `🆔: <ник почты>` — **всегда**. Ник = `display_name` аккаунта, при пустом/`NULL` `display_name` → `email`. Источник — `acc_label = account.display_name or account.email` (резолвится в `notify_service.dispatch_one_payload`, см. ниже; правок recipient-SQL **не требуется** — `MailAccount.display_name` уже загружен `mail_accounts.get_by_id`).
2. `#️⃣: <теги>` — **всегда**. Если у письма есть теги — все логические теги через `, ` (после дедупа по `(name, color)`, round-21). Если тегов нет — **«Не сортировано»**. Выбор «все теги через запятую» (а не один): у письма может быть несколько тегов (auto-tagging применяет несколько `tags`-строк), сокрытие части тегов вводило бы пользователя в заблуждение; запятая компактнее, чем строка-на-тег.
3. **пустая строка** — разделитель (по ТЗ).
4. `Клиент: <отправитель>` — **всегда**. `from_label = from_name or from_addr` (без изменений relative round-34, переименован лейбл «Отправитель» → «Клиент:»).
5. `Тема: <тема>` — **всегда**. `subject` после нормализации; при пустой теме → **«(без темы)»** (согласовано с callback §2.6, где тот же плейсхолдер используется при открытии письма). Обрезка до `SUBJECT_MAX = 150` символов (по границе + «…»).
6. **пустая строка** — разделитель (по ТЗ).
7. `<превью тела>` — **только если** `body_preview` непуст (письмо без тела → строка и предшествующий пустой разделитель опускаются, чтобы не оставлять «хвост» из пустой строки). Нормализуется и режется до `PREVIEW_LEN = 100` символов **в Python** (не в SQL) — см. §2.4.

Длины (`PREVIEW_LEN = 100`, `SUBJECT_MAX = 150`) — **константы модуля** `notify_format.py` (не env: ретюн не нужен, лишний env-флаг — overhead).

**HTML-форматирование (решение round-36):** значения (`ник`, `теги`, `клиент`, `тема`) выделяются `<b>` для читаемости (консистентно с прежним стилем и контрастно к emoji-меткам). Emoji-метки (`🆔`, `#️⃣`) — plain UTF-8 (не bold). Превью тела — plain (как и раньше). **ВСЕ** user-controlled значения (ник/email, имена тегов, `from`, `subject`, превью) экранируются `html.escape()` — обязательная защита от инъекции в `parse_mode=HTML`. Плейсхолдеры «Не сортировано» / «(без темы)» — статический текст, в escape не нуждаются (но проходят через него вместе со значением — это безопасно).

```python
PREVIEW_LEN: Final[int] = 100   # round-36: было 120 (round-34)
SUBJECT_MAX: Final[int] = 150

_NO_TAG: Final[str] = 'Не сортировано'   # round-36: fallback строки #️⃣
_NO_SUBJECT: Final[str] = '(без темы)'   # round-36: fallback строки Тема:

def format_notification(
    *,
    acc_label: str,        # display_name or email (ник почты)
    from_label: str,       # from_name or from_addr (клиент)
    tag_names: list[str],  # может быть ПУСТЫМ -> "Не сортировано"
    subject: str | None,   # тема письма; None/'' -> "(без темы)"
    body_preview: str,     # уже нормализованное+обрезанное превью; '' -> строка не печатается
) -> str:
    """HTML-строка для sendMessage parse_mode=HTML (round-36).
    Все user-controlled значения экранируются через html.escape()."""
    acc_safe = html.escape(acc_label)
    from_safe = html.escape(from_label)
    # #️⃣: все теги через запятую (дедуп по (name,color) сделан в notify_service),
    # либо "Не сортировано", если тегов нет.
    if tag_names:
        tags_safe = ', '.join(html.escape(t) for t in tag_names)
    else:
        tags_safe = html.escape(_NO_TAG)
    # Тема: всегда; пустая -> "(без темы)"; иначе нормализация + срез SUBJECT_MAX.
    subj = _WHITESPACE_RUN_RE.sub(' ', strip_invisible_padding(subject or '')).strip()
    if not subj:
        subj = _NO_SUBJECT
    elif len(subj) > SUBJECT_MAX:
        subj = subj[:SUBJECT_MAX].rstrip() + '…'
    subj_safe = html.escape(subj)
    lines = [
        f'🆔: <b>{acc_safe}</b>',
        f'#️⃣: <b>{tags_safe}</b>',
        '',                                  # пустой разделитель
        f'Клиент: <b>{from_safe}</b>',
        f'Тема: <b>{subj_safe}</b>',
    ]
    if body_preview:  # body_preview уже нормализован+обрезан (PREVIEW_LEN) в notify_service
        lines.append('')                     # пустой разделитель перед превью
        lines.append(html.escape(body_preview))
    return '\n'.join(lines)
```

**Нормализация превью (выполняется в `notify_service.dispatch_one_payload`, НЕ в SQL — см. §2.4):**
- источник — `message.body_text` (plain). Если `body_text` пуст → `strip_tags(message.body_html)` через существующий `sanitize_telegram_html()` + дополнительное снятие оставшейся разметки до plain. **Обоснование выбора `body_text`:** round-29 зафиксировал, что у Apple `body_text` и `body_html` **различаются** (UI рендерит `body_html`). Для тизера в push нужен короткий человекочитаемый текст без верстки/CSS/трекинг-пикселей — `text/plain` part письма заведомо «чище» (нет тегов, нет инлайн-стилей), поэтому даёт осмысленный teaser «из коробки». `body_html` берём только как fallback, прогоняя через тот же sanitiser, что и callback (§2.6), чтобы не протёк CSS/скрипт. Несовпадение версий некритично: push — это тизер-приманка, полный «правильный» рендер (`body_html`) пользователь видит по кнопке «Посмотреть сообщение».
- схлопнуть любой whitespace (переводы строк `\n\r`, табы, множественные пробелы, неразрывный пробел ` ` и zero-width padding) в **один** пробел; обрезать по краям;
- срезать до `PREVIEW_LEN = 100` символов (round-36; было 120 в round-34); если исходник длиннее — `[:100].rstrip() + '…'`;
- если после нормализации строка пуста → передать `''` (строка превью + предшествующий пустой разделитель не печатаются).

Пример с ником почты + тегом + темой + телом (round-36):
```
🆔: <b>Apple Test 1</b>
#️⃣: <b>DPLA.PLA</b>

Клиент: <b>sender@gmail.com</b>
Тема: <b>Ваш заказ #12345 отправлен</b>

Здравствуйте! Ваш заказ был передан в службу доставки и поступит в пункт выдачи в течение 2–3 раб…
```

Пример без ника почты (только email) + без тега + без темы + с телом (fallback'и «Не сортировано» / «(без темы)»):
```
🆔: <b>support@example.com</b>
#️⃣: <b>Не сортировано</b>

Клиент: <b>AppStoreNotices@apple.com</b>
Тема: <b>(без темы)</b>

Your subscription will renew soon. Tap to review the details and manage your plan in the App St…
```

Пример с несколькими тегами + без тела (строка превью и пустой разделитель перед ней отсутствуют):
```
🆔: <b>support@example.com</b>
#️⃣: <b>DPLA.PLA, VIP</b>

Клиент: <b>sender@gmail.com</b>
Тема: <b>Welcome</b>
```

**Edge-cases:**
- пустой `display_name` аккаунта (`NULL`/`''`) → строка `🆔:` показывает `email` (резолв `display_name or email` в `notify_service`);
- письмо без тегов / `TG_NOTIFY_ALL_MESSAGES=true` → строка `#️⃣:` показывает **«Не сортировано»** (строка всегда присутствует — отличие от round-31);
- несколько тегов на письме → все через `, ` (после дедупа по `(name, color)`);
- пустой `subject` (`None` или `''` после strip) → строка `Тема:` показывает **«(без темы)»** (строка всегда присутствует — отличие от round-34);
- пустое тело (`body_text` и `body_html` оба пусты / дают пустой результат после нормализации) → строка превью **и** предшествующий пустой разделитель опускаются (без «висящей» пустой строки в конце);
- очень длинная тема (>150) → срез `[:150].rstrip()+'…'`; очень длинное тело (>100) → срез `[:100].rstrip()+'…'`;
- HTML/спецсимволы (`<`, `>`, `&`) и кавычки в `ник`/`email`/`теге`/`subject`/превью → `html.escape()` (значения сохраняются как обычный текст, не как разметка);
- многострочный `subject` (редко, но в письмах встречаются folded-заголовки) и переводы строк в теле → схлопываются в один пробел, push остаётся компактным (4096-лимит Bot API не превышается: максимум ~150+100 видимых символов + статичный текст);
- результат `format_notification` гарантированно ≤ ~400 символов после escape → одна `sendMessage`, без chunk-логики (chunk-сплит остаётся только в callback §2.6 для полного тела).

**Следствие для dispatcher (round-31):** ранний `if not message_tags: return` в `dispatch_one_payload` (§2.4) **убирается** — при пустом списке тегов продолжаем с `tag_names=[]` (round-36: `format_notification` сам рендерит «Не сортировано»). Дедуп тегов по `(name, color)` (round-21) сохраняется и выполняется в `notify_service` до вызова `format_notification`.

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

#### 2.10. Нормализация пустых строк при ПРОСМОТРЕ письма (round-37, bug «множество пустых строк»)

**Проблема.** При открытии письма на web-странице `GET /messages/{id}` (шаблон `message_view.html`) — и, как следствие, в TG «Посмотреть сообщение», т.к. кнопка ведёт на ту же страницу через `?embed=tg` (см. §2.6, §2.5 inline-keyboard) — тело письма показывается с **множеством подряд идущих пустых строк**. Типичный кейс — Apple/маркетинговые письма (например, «Your Apple Account information has been updated»): 15+ пустых строк между абзацами.

**Это НЕ относится к тексту push-уведомления** (§2.5): там превью уже схлопывает любой whitespace в один пробел через `normalize_preview`. Баг — только в **полном** отображении тела письма при просмотре.

**Где артефакт возникает (две render-ветки `message_view.html`, см. §«Tech»):**

1. **HTML-ветка** (`{{ message.body_html | safe }}`) — основной кейс Apple (у письма есть `text/html` part). `mail_accounts`-ingest сохраняет `body_html` через `sanitize_email_html` (`shared/html_sanitize.py`), который **whitelist'ит** блочные теги (`<p>`, `<div>`, `<br>`, `<table>`, `<tr>`…), но **не схлопывает вертикальный whitespace**. Пустые блочные элементы (`<p>&nbsp;</p>`, пустые `<div>`, spacer-`<tr>`) и подряд идущие `<br>` рендерятся браузером как высокий столб пустого вертикального пространства. (В отличие от Telegram-ветки `sanitize_telegram_html`, где `_COLLAPSE_BLANK_LINES_RE` уже схлопывает `\n{3,}` → `\n\n`.)
2. **Plain-text-ветка** (`<pre>{{ message.body_text | e }}</pre>`) — для писем без `text/html` part. При ingest `body_text` для таких писем формируется `html2text(msg.html)` (`worker/app/imap_fetcher.py`), который оставляет прогоны `\n\n\n…`. `<pre>` сохраняет каждый `\n` буквально → видны пустые строки.

**Решение: нормализация при ОТОБРАЖЕНИИ (render-time), НЕ при ingest.** Хранимые `body_text`/`body_html` остаются нетронутыми.

**Обоснование выбора render-time (а не ingest/worker):**
- **Чинит существующие письма.** В БД уже лежат письма с артефактом (retention 30 дней — все они видны пользователю). Нормализация при ingest исправила бы только новые письма; render-time чинит и старые без data-миграции/реингеста.
- **Не теряем оригинал.** Хранимое тело остаётся исходным — если правило схлопывания окажется слишком агрессивным, его можно ослабить без потери данных. Также `body_text` используется для tag-matching (`body_contains`, ADR-0017 §4.2) и для push-превью — менять хранимое значение под нужды одного UI-вью неверно (затронуло бы и другие потребители).
- **Дёшево.** Тело уже клампится до 1 MiB при ingest (§«Tech»); схлопывание на render — один проход regex по строке ≤1 MiB на запрос просмотра (не hot-path: один просмотр = один пользователь).
- **Стоимость render-time:** нормализация выполняется на каждый просмотр (нет кэша). Приемлемо: просмотр письма — редкая интерактивная операция (≤5 users), не batch. Если профиль покажет проблему — можно материализовать в отдельную колонку (follow-up, не требуется сейчас).

**Точные правила нормализации (для backend):**

Обе ветки нормализуются **на стороне backend** (в `MessageService.get` → `MessageDetail`, см. ниже), чтобы JSON-API (`GET /api/messages/{id}`) и HTML-страница давали идентичный результат и логика была покрыта unit-тестами (а не пряталась в Jinja-фильтре). Добавляются две чистые функции в `shared/html_sanitize.py` (рядом с уже существующими sanitiser'ами — единый модуль очистки тел):

1. **Plain-text** (`body_text`, ветка `<pre>`): схлопнуть 3+ подряд идущих «пустых» строк (строка, состоящая только из whitespace) в максимум **одну** пустую строку-разделитель (т.е. абзацы остаются разделены ровно одной пустой строкой). Также убрать пустые строки в начале/конце.

   ```python
   # shared/html_sanitize.py
   # Строка из необязательного horizontal whitespace, затем перевод строки —
   # повторённая 2+ раза подряд после первого \n → один разделитель абзаца ("\n\n").
   # \n\s*\n\s*\n+  → \n\n   (3+ переводов строки с произвольным h-whitespace между ними).
   _COLLAPSE_BLANK_TEXT_LINES_RE: Final[re.Pattern[str]] = re.compile(r"\n[ \t\r\f\v]*(?:\n[ \t\r\f\v]*)+\n")

   def collapse_blank_lines_text(text: str) -> str:
       """Схлопнуть прогоны из 3+ переводов строки (пустые строки между абзацами)
       в один разделитель абзаца (\\n\\n). Сохраняет читаемость: абзацы разделены
       ровно одной пустой строкой. Ведущие/замыкающие пустые строки убираются.
       Пустой/None-вход → ''. Хранимое body НЕ меняется — функция применяется на render."""
       if not text:
           return ""
       collapsed = _COLLAPSE_BLANK_TEXT_LINES_RE.sub("\n\n", text)
       return collapsed.strip("\n")
   ```

   Замечание: regex использует **не** `\s` внутри класса (чтобы `\s` не «съел» сами `\n` непредсказуемо), а явный класс horizontal-whitespace `[ \t\r\f\v]`; переводы строк `\n` матчатся явно. Это надёжнее, чем `\n\s*\n\s*\n+`, и даёт детерминированный «максимум одна пустая строка».

2. **HTML** (`body_html`, ветка `| safe`): свернуть вертикальные «пустые» конструкции, которые браузер рендерит как пустые строки, оставив максимум один разделитель абзаца. Чистим **после** `sanitize_email_html` (т.е. над уже-санитизированным whitelist-HTML — безопасно, теги уже сужены):
   - схлопнуть 3+ подряд `<br>` (с произвольным whitespace между ними) → `<br><br>`;
   - удалить «пустые» блочные элементы-разделители, состоящие только из whitespace/`&nbsp;`/`<br>`: `<p>…</p>`, `<div>…</div>` с пустым содержимым → удалить целиком;
   - схлопнуть прогоны whitespace между блочными тегами.

   ```python
   # shared/html_sanitize.py
   _EMPTY_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
       r"<(p|div)\b[^>]*>(?:\s|&nbsp;|<br\s*/?>)*</\1>", re.IGNORECASE
   )
   _MULTI_BR_RE: Final[re.Pattern[str]] = re.compile(
       r"(?:<br\s*/?>\s*){3,}", re.IGNORECASE
   )

   def collapse_blank_lines_html(html: str) -> str:
       """Свернуть пустые блочные элементы и прогоны <br> в санитизированном
       HTML-теле, чтобы при рендере не было столба пустых строк. Применяется
       на render поверх вывода sanitize_email_html. Хранимое body НЕ меняется.
       Пустой/None-вход → ''."""
       if not html:
           return ""
       collapsed = _EMPTY_BLOCK_RE.sub("", html)        # удалить пустые <p>/<div>
       collapsed = _MULTI_BR_RE.sub("<br><br>", collapsed)  # 3+ <br> → 2
       return collapsed
   ```

   Применяется **один проход** (не итеративно): вложенные пустые блоки на практике у Apple/ESP редки и одного прохода достаточно для устранения видимого столба; итеративный fixpoint — overkill (не вводим).

**Где встраивается (backend):**

- `backend/app/messages/service.py` → `MessageService.get(...)`: перед сборкой `MessageDetail` пропустить `msg.body_text` через `collapse_blank_lines_text(...)` и `msg.body_html` через `collapse_blank_lines_html(...)`. То есть `MessageDetail.body_text` / `.body_html` несут **уже нормализованные для отображения** значения. Это автоматически чинит и HTML-страницу (`message_view.html`), и JSON `GET /api/messages/{id}`, и TG-view (`?embed=tg`) — все три потребляют `MessageDetail`.
- **`message_view.html` не меняется** — он рендерит `message.body_html | safe` / `message.body_text | e` как раньше; нормализация уже произошла в service.
- **Хранимое НЕ трогаем:** `worker/app/imap_fetcher.py`, `messages`-repo, миграции — без изменений. tag-matching (`body_contains`) и push-превью продолжают читать сырое `messages.body_text`/`body_html` из БД (они идут через repo/worker, не через `MessageService.get`).

**Edge-cases:**
- письмо без тела (`body_present=false`) → ветки не достигаются (шаблон показывает «нет читаемого текстового тела»); функции получают `''`/`None` → возвращают `''` — безопасно.
- `body_truncated=true` (тело обрезано до 1 MiB) → нормализация применяется к обрезанному значению, флаг truncated сохраняется; обрезка по байтам могла оставить «рваный» хвост тега в HTML — `| safe` это уже допускает сегодня (поведение не ухудшается), браузер закрывает незакрытый тег.
- одиночная пустая строка между абзацами (нормальный текст) → НЕ трогается (правило срабатывает только на 3+ переводах строки / 3+ `<br>`).
- легитимный `<pre>`-блок с намеренным форматированием внутри HTML-тела — `_EMPTY_BLOCK_RE` матчит только `<p>`/`<div>`, `<pre>` не затрагивается.

**Миграций нет.** Изменение чисто на уровне отображения; схема БД и хранимые данные не меняются. Чинит и существующие письма (нормализация на каждом просмотре).

##### round-39 — post-sanitize collapse для TG «Посмотреть сообщение» (bug «множество пустых строк в TG full-body view»)

**Постановка проблемы (баг, подтверждён на проде — письмо Apple `id=1252`).**

round-37 (выше) добавил `collapse_blank_lines_text` / `collapse_blank_lines_html` и применил их в `MessageService.get` к `body_text` / `body_html`. Это закрыло **web**-вью (`message_view.html`) и JSON `GET /api/messages/{id}`. Но TG «Посмотреть сообщение» рендерится **не** из `MessageService.get`, а отдельным путём — `backend/app/telegram/callback_handler.py::_format_message_body` (строки 74–113), который при наличии `body_html` строит тело так:

```python
body_safe = sanitize_telegram_html(body_html)   # строка 98
```

Диагноз на письме Apple `id=1252`:
- сохранённый `body_text` **чистый** (без строк-отступов); `body_html` присутствует (1917 байт) — табличная вёрстка (Apple/marketing: вложенные `<table>/<tr>/<td>` со spacer-ячейками и whitespace-узлами).
- `_format_message_body` идёт по **HTML-ветке** (`body_html` непустой) → `sanitize_telegram_html`. Эта функция оставляет только Telegram-подмножество (`b/i/u/s/a/code/pre`), а `<table>/<tr>/<td>/<div>` **стрипает**, при этом блок-закрытия (`_BLOCK_CLOSE_TO_NL_RE`, вкл. `</table>`/`</tr>`) → `\n`. Сплошные вложенные таблицы с пустыми ячейками + whitespace-узлы после стрипа дают **много** строк-отступов вида `\n                \n`.
- **Почему round-37 не помог:** `collapse_blank_lines_html` чистит только пустые `<p>/<div>` и 3+`<br>` — таблицы он не трогает; а пустота **рождается на этапе `sanitize_telegram_html` (стрип таблиц)**, который выполняется **после** `MessageService.get`-нормализации (в TG-ветке `MessageService.get` вообще не на пути — `_format_message_body` сам зовёт `sanitize_telegram_html`). Поэтому артефакт остаётся.
- **Почему встроенный `_COLLAPSE_BLANK_LINES_RE` (`\n{3,}`) внутри `sanitize_telegram_html`, строка 342, не справляется:** он матчит только прогоны **голых** `\n`. Строки-отступы содержат пробелы (`\n`+пробелы+`\n`), поэтому `\n{3,}` на них **не срабатывает** — между переводами строки стоят whitespace-символы. Это и есть корневая причина.

**Решение (round-39): схлопывать пустые строки ПОСЛЕ `sanitize_telegram_html`**, расширенным правилом, которое (а) считает «пустой» строку с **любым** whitespace (вкл. `\xa0`/zero-width/` `/`　`), и (б) обрабатывает **смешанные** прогоны `\n` и `<br>`.

**Где применять — вариант (а) «в `_format_message_body`», НЕ (б) «внутри `sanitize_telegram_html`».**

| Критерий | (а) collapse в `_format_message_body` (после строки 98) | (б) встроить collapse в `sanitize_telegram_html` |
| --- | --- | --- |
| Кто получит фикс | только TG full-body view (единственный потребитель, сохраняющий построчную структуру) | все потребители `sanitize_telegram_html` |
| Польза для `html_to_plain` (preview) | n/a — preview и так не страдает | **нулевая**: `html_to_plain` сразу гонит выход через `_ANY_TAG_RE` + `normalize_preview`, который `[\s\xa0]+ → " "` схлопывает **любой** whitespace в один пробел; пустые строки для preview уже неразличимы |
| Польза для `notify_format` | n/a | нулевая (тот же `normalize_preview`) |
| Риск регрессии | минимальный, локализован одной call-site | средний: изменение «общего» санитайзера может тонко сдвинуть существующее поведение preview/tag-stripping и потребует переутверждения юнит-тестов `test_notify_format` |
| Семантика | «компактный вывод именно для full-body view» — там, где это и нужно | размывает контракт `sanitize_telegram_html` («сузить markup до TG-подмножества») доп. ответственностью |
| blast-radius | 1 функция + 1 call-site | 1 функция, но затрагивает 3 потребителя |

**Выбран вариант (а).** Единственный потребитель `sanitize_telegram_html`, который **сохраняет построчную структуру** и потому страдает от пустых строк, — это `_format_message_body` (TG full-body view). `html_to_plain` (preview) и `notify_format` уже выравнивают любой whitespace в один пробел через `normalize_preview` (`_WHITESPACE_RUN_RE = [\s\xa0]+`), поэтому встраивание в санитайзер им **ничего не даёт**, но расширяет blast-radius и риск регрессии общего модуля. Фикс локализуется в TG-ветке `_format_message_body`, что соответствует принципу простоты README.

> Существующий `_COLLAPSE_BLANK_LINES_RE` (`\n{3,}`) **внутри** `sanitize_telegram_html` (строка 342) **оставляем как есть** — он дёшев и схлопывает голые `\n`-прогоны для всех потребителей; round-39 добавляет **дополнительный** проход поверх его выхода в TG-ветке, закрывающий whitespace-классы и `<br>`, которые `\n{3,}` пропускает. Дубль-чистка идемпотентна и безвредна.

**Новая функция (`shared/html_sanitize.py`):**

```python
# shared/html_sanitize.py — round-39 (ADR-0022 §2.10)
#
# «Пустая» строка для TG full-body view = строка из ЛЮБОГО whitespace.
# В отличие от round-37 _COLLAPSE_BLANK_TEXT_LINES_RE (узкий ASCII-класс
# [ \t\r\f\v]) здесь нужен ШИРОКИЙ класс, т.к. строки-отступы Apple/marketing
# содержат U+00A0 (nbsp), U+2003 (em space), U+3000 (ideographic space) и т.п.
# zero-width-символы (U+200B/200C/200D/2060/FEFF) НЕ являются whitespace в
# Unicode-смысле (класс [^\S\n] их НЕ матчит), но к этому моменту они уже
# удалены внутри sanitize_telegram_html (strip_invisible_padding, до collapse) —
# поэтому на входе collapse их нет. Порядок «collapse ПОСЛЕ санитайза» обязателен.
#
# Прогон-сепаратор = (необязательный whitespace, затем перевод строки ИЛИ <br>),
# повторённый так, что между двумя «контентными» строками стоит 2+ разрыва.
# Схлопываем такой прогон в РОВНО один разделитель абзаца ("\n\n").
#
# \S в Python re (str-режим, re.UNICODE по умолчанию) = НЕ-whitespace; значит
# [^\S\n] = «весь Unicode-whitespace, КРОМЕ \n» — включает \xa0 / em-space / 　,
# но НЕ съедает сами \n (они — «разрывы», матчатся отдельно через _TG_BREAK).
# Это даёт детерминированный «horizontal-whitespace вокруг разрывов».
# Перевод строки и <br> нормализуем как взаимозаменяемые «разрывы».
_TG_BREAK = r"(?:\n|<br\s*/?>)"
_TG_HSPACE = r"[^\S\n]"            # любой whitespace КРОМЕ \n (incl. \xa0, ,　)
#
# 3+ разрыва (\n|<br>) с произвольным h-whitespace между/вокруг → "\n\n".
_COLLAPSE_TG_BLANK_RE: Final[re.Pattern[str]] = re.compile(
    rf"{_TG_HSPACE}*{_TG_BREAK}(?:{_TG_HSPACE}*{_TG_BREAK}){{2,}}{_TG_HSPACE}*"
)
#
# Split по <pre>…</pre>: захватывающая группа → блоки <pre> попадают в
# НЕЧЁТНЫЕ сегменты результата re.split, обычный текст — в ЧЁТНЫЕ. Collapse
# применяется ТОЛЬКО к чётным (вне <pre>); переносы внутри <pre> значимы и
# сохраняются дословно. <pre> входит в _TELEGRAM_ALLOWED_TAGS, т.е. доживает
# до collapse — split встроен ПРЯМО в тело функции (не «требование к backend»).
_TG_PRE_SPLIT_RE: Final[re.Pattern[str]] = re.compile(
    r"(<pre\b.*?</pre>)", re.DOTALL | re.IGNORECASE
)

def collapse_blank_lines_tg(text: str | None) -> str:
    """Схлопнуть пустые строки в УЖЕ-санитизированном Telegram-HTML
    (выход sanitize_telegram_html: смесь "\\n" и "<br>").

    Прогон из 3+ разрывов строки (любая комбинация "\\n" и "<br>", с
    произвольным horizontal-whitespace — вкл. \\xa0/\\u2003/\\u3000 — между
    ними) схлопывается в один разделитель абзаца ("\\n\\n"). Абзацы остаются
    разделены ровно одной пустой строкой; одиночный разрыв и одиночная пустая
    строка не трогаются. Ведущие/замыкающие пустые строки убираются.

    <pre>-содержимое НЕ трогается: вход режется по <pre>…</pre> через
    _TG_PRE_SPLIT_RE (захватывающая группа → блоки <pre> = НЕЧЁТНЫЕ сегменты),
    collapse применяется ТОЛЬКО к чётным (вне <pre>) сегментам; переносы
    внутри <pre> сохраняются дословно. Сегменты склеиваются обратно.

    Применяется ТОЛЬКО в TG full-body view (_format_message_body) поверх
    sanitize_telegram_html. Пустой/None-вход → ''. Хранимое body НЕ
    меняется (render-time)."""
    if not text:
        return ""
    parts = _TG_PRE_SPLIT_RE.split(text)
    # чётные сегменты = текст вне <pre> (collapse); нечётные = <pre>…</pre> (как есть)
    for i in range(0, len(parts), 2):
        parts[i] = _COLLAPSE_TG_BLANK_RE.sub("\n\n", parts[i])
    return "".join(parts).strip("\n")
```

**Точный whitespace-класс (round-39).** «Пустой» считается строка, содержащая любой из:
- ASCII horizontal: ` ` `\t` `\r` `\f` `\v`;
- Unicode-пробелы, попадающие в `\s` при `str`-режиме `re`: `\xa0` (U+00A0 NBSP), ` ` (EM SPACE), `　` (IDEOGRAPHIC SPACE), ` / … `, ` `, ` `, и др. из Zs;
- zero-width (`​/‌/‍/⁠/﻿`) — уже удалены `strip_invisible_padding` внутри `sanitize_telegram_html` до collapse, но класс `[^\S\n]` их не «ловит» (они не whitespace в Unicode-смысле). Поэтому **порядок обязателен**: collapse применяется к выходу `sanitize_telegram_html`, который уже прогнал `strip_invisible_padding` (строка 336) — zero-width на входе collapse отсутствуют. Дополнительной защиты не требуется.

Класс выражен как `[^\S\n]` = «любой whitespace, кроме `\n`». Это надёжнее перечисления: `\S` = не-whitespace, `[^\S\n]` = whitespace-минус-`\n`, что точно отделяет horizontal-whitespace от самих разрывов строк и даёт детерминированный результат (round-37 для plain-ветки использует узкий `[ \t\r\f\v]`; round-39 расширяет до Unicode, т.к. артефакт содержит `\xa0`/` `).

**Обработка смешанных `\n` / `<br>` (round-39).** После `sanitize_telegram_html` в выводе встречаются **оба** разрыва: `\n` (из `_BR_TO_NL_RE` + `_BLOCK_CLOSE_TO_NL_RE`) и потенциально оставшиеся `<br>` (Telegram-санитайзер конвертит `<br>`→`\n` **до** bleach, так что `<br>` в выходе быть не должно — но `collapse_blank_lines_tg` устойчив к обоим на случай прямого вызова/будущих изменений). `_TG_BREAK = (?:\n|<br\s*/?>)` трактует `\n` и `<br>` как **взаимозаменяемые** разрывы; прогон из 3+ любых их комбинаций (`\n\n<br>`, `<br>\n<br>`, `\n   \n   \n`) → `\n\n`. Результат — чистый `\n`-разделённый TG-HTML (без `<br>`-остатков), что корректно для `parse_mode=HTML` (Telegram не принимает `<br>`).

**Где встраивается (backend, точная правка):**

`backend/app/telegram/callback_handler.py::_format_message_body`, HTML-ветка — после строки 98:

```python
if body_html:
    body_safe = sanitize_telegram_html(body_html)
    body_safe = collapse_blank_lines_tg(body_safe)   # round-39: post-sanitize collapse
    if not body_safe.strip():
        body_safe = ""
```

И импорт `collapse_blank_lines_tg` рядом с `sanitize_telegram_html` (строка 52).

**body_text-ветка (строки 105–109)** — `linkify_plain_text(strip_invisible_padding(body_text))`. По диагнозу `body_text` для проблемных писем **чистый**, но HTML-fallback писем без `text/html` part может содержать `html2text`-артефакт. round-39 **не меняет** body_text-ветку: (1) её артефакт уже покрывается тем, что для таких писем `body_html` обычно пуст → ветка достигается, но `linkify_plain_text` сохраняет переводы строк, а основной баг (Apple) идёт по HTML-ветке; (2) применять `collapse_blank_lines_tg` к выходу `linkify_plain_text` безопасно, но **не требуется** для закрытия текущего бага. Решение: **scope round-39 = только HTML-ветка `_format_message_body`** (минимальный фикс под подтверждённый баг). Если позже всплывёт артефакт в plain-fallback TG-view — добавить `collapse_blank_lines_tg` и в body_text-ветку (тривиально, отдельной итерацией); сейчас не раздуваем scope.

**Edge-cases (round-39):**
- **Кликабельные ссылки (`<a>`).** `sanitize_telegram_html` сохраняет `<a href>`; `collapse_blank_lines_tg` матчит только разрывы строк (`\n`/`<br>`) и horizontal-whitespace **между** ними — `<a …>текст</a>` не содержит таких прогонов внутри и **не рвётся**. Regex не трогает содержимое тегов.
- **`<pre>`-блоки.** Переносы внутри `<pre>` значимы. `<pre>` входит в `_TELEGRAM_ALLOWED_TAGS` и доживает до collapse, поэтому защита встроена **прямо в тело `collapse_blank_lines_tg`** (а не оставлена «требованием к backend»): вход режется через `_TG_PRE_SPLIT_RE = re.compile(r'(<pre\b.*?</pre>)', re.DOTALL | re.IGNORECASE)`; захватывающая группа кладёт блоки `<pre>…</pre>` в **нечётные** сегменты `re.split`, обычный текст — в **чётные**; `_COLLAPSE_TG_BLANK_RE.sub("\n\n", …)` применяется **только к чётным** сегментам, нечётные (`<pre>`) переносятся в результат **дословно**, затем сегменты склеиваются. Так переносы внутри `<pre>` гарантированно сохранены при любом письме (не только когда `<pre>` фактически отсутствует у Apple/marketing). Backend копирует код-блок §2.10 как есть — отдельной правки не требуется.
- **Пустое тело.** `body_html=""`/`None` → HTML-ветка не достигается или `collapse_blank_lines_tg("")→""` → fallback на body_text / `<em>(пустое тело)</em>` (строки 105–111). Без изменений.
- **Одиночная пустая строка / одиночный разрыв** — НЕ трогается (правило срабатывает на 3+ разрывах).
- **Заголовки (`Тема:`/`От:`)** формируются отдельно (`html.escape`, строки 94–95, 113) и в `body_safe` не входят — collapse их не затрагивает.

**WEB-ветка (`message_view.html`) — scope-решение round-39.** Web рендерит `body_html | safe` как **полный** HTML с таблицами (`<table>` в whitelist `sanitize_email_html`); в браузере Apple-таблицы со spacer-ячейками дают вертикальные пробелы, а `collapse_blank_lines_html` (round-37, чистит только `<p>/<div>/<br>`) их не убирает. **Решение: web-ветку в round-39 НЕ трогаем.** Обоснование:
- Основной канал просмотра у пользователя — **TG «Посмотреть сообщение»** (подтверждённый баг именно там); web — вторичный.
- Чистка spacer-ячеек таблиц в **полном** HTML — отдельная задача с риском сломать легитимную табличную вёрстку (отличить spacer-`<td>` от контентного без рендера сложно); это раздуло бы scope.
- В TG-ветке таблицы **стрипаются** санитайзером (Telegram их не рендерит), поэтому достаточно схлопнуть результат; в web таблицы **сохраняются намеренно** (богатый просмотр) — другой компромисс.

Если web-артефакт станет приоритетным — отдельная итерация: либо расширить `collapse_blank_lines_html` правилом «удалять `<tr>`/`<td>`, чьё единственное содержимое — whitespace/`&nbsp;`/spacer-`<img>`», либо ограничить высоту через CSS. Заведено как tech-debt `TD-039` (`100-known-tech-debt.md`) — **не блокирует** round-39 TG-фикс.

**Миграций нет.** Изменение render-time, локализовано в TG full-body view (`_format_message_body`) + одна новая чистая функция в `shared/html_sanitize.py`. Схема БД и хранимые данные не меняются. Чинит и существующие письма (нормализация на каждом просмотре). Юнит-тесты — отдельной задачей QA (вне scope architect): `collapse_blank_lines_tg` (смешанные `\n`/`<br>`, `\xa0`/` `/`　`, `<a>`-неразрыв, `<pre>`-сохранение, пустой вход) + интеграционный на `_format_message_body` с Apple-подобным HTML.

##### round-40 — удаление невидимых bidi-форматтеров (LRM/RLM) из spacer-строк marketing-писем (bug «строка-спейсер не схлопывается, Glassdoor `id=1264`»)

**Постановка проблемы (баг, подтверждён на проде — письмо Glassdoor `id=1264`).**

round-39 (выше) добавил `collapse_blank_lines_tg` и применил его в `_format_message_body` к выходу `sanitize_telegram_html`. Это закрыло Apple-письма (вложенные таблицы → строки-отступы из `\n`+whitespace схлопываются классом `[^\S\n]`, включающим `\xa0`). **Но** marketing-письма (Glassdoor и подобные) после `sanitize_telegram_html` + `collapse_blank_lines_tg` **сохраняют** длинную строку-СПЕЙСЕР preheader'а, которая не схлопывается.

Диагноз на письме Glassdoor `id=1264`:
- после `sanitize_telegram_html` + `collapse_blank_lines_tg` остаётся одна длинная строка-спейсер — повтор паттерна `"\xa0‎‏"`: U+00A0 NO-BREAK SPACE ×93, U+200E LEFT-TO-RIGHT MARK ×83, U+200F RIGHT-TO-LEFT MARK ×83.
- **Корневая причина:** whitespace-класс `_TG_HSPACE = [^\S\n]` (round-39) матчит `\xa0` (он whitespace в Unicode-смысле), **но НЕ** матчит U+200E / U+200F — это символы Unicode-категории **Cf** (Format), которые **не** являются whitespace. Поэтому строка-спейсер считается «непустой» (содержит не-whitespace U+200E/U+200F) → `_COLLAPSE_TG_BLANK_RE` её не схлопывает, и прогон `\n` вокруг неё не объединяется (между двумя `\n` стоит «непустой» спейсер).
- **Почему `strip_invisible_padding` (round-12) не убрал их:** действующий набор `_INVISIBLE_PADDING_CODEPOINTS` = `{200B, 200C, 200D, 2060, FEFF}` — в нём **нет** U+200E (LRM) и U+200F (RLM). Поэтому `sanitize_telegram_html` (зовёт `strip_invisible_padding` на строке 406, до collapse) их **не удаляет**, и к моменту collapse они на месте. Подтверждено: после sanitize U+200E/U+200F остаются.

**Решение (round-40): РАСШИРИТЬ существующий `_INVISIBLE_PADDING_CODEPOINTS`** двумя bidi-форматтерами — U+200E (LRM) и U+200F (RLM) — а **не** заводить новую функцию `strip_format_chars`. Тогда `strip_invisible_padding` (который уже вызывается внутри `sanitize_telegram_html` **до** collapse) удалит U+200E/U+200F; строка-спейсер `"\xa0‎‏…"` превратится в `"\xa0\xa0…"` (только NO-BREAK SPACE) → попадёт под whitespace-класс `[^\S\n]` round-39 → схлопнётся штатно вместе с соседними `\n`. **Новый код в `collapse_blank_lines_tg` и в `_format_message_body` не нужен — фикс целиком в наборе кодпоинтов.**

**Выбор: расширить набор vs. `unicodedata.category=='Cf'` vs. новая функция.**

| Вариант | Плюсы | Минусы | Решение |
| --- | --- | --- | --- |
| **(A) Расширить `_INVISIBLE_PADDING_CODEPOINTS` явным набором** (добавить 200E, 200F) | предсказуемо, детерминированно; `str.translate` (C-level, дёшево); унификация — один источник для всех 5 потребителей; не тянет `unicodedata` в hot-ish path (`normalize_preview` на каждый push) | ловит только перечисленные кодпоинты (будущие Cf-спейсеры потребуют доп. правки) | **ВЫБРАН** |
| (B) Удалять по `unicodedata.category(ch)=='Cf'` | ловит **любые** будущие Format-символы | посимвольный Python-цикл (медленнее `translate`) на каждый body/preview; шире → риск зацепить значимые Cf; менее предсказуемо | отклонён — overkill + риск |
| (C) Отдельная `strip_format_chars` в collapse-ветке | изоляция от прочих потребителей | дублирование; не чинит web-инбокс/preview (где тот же мусор так же безвреден); лишняя функция | отклонён — унификация предпочтительна |

**Унификация с `strip_invisible_padding` безопасна для всех 5 потребителей** (проверено по всем call-site):

| Call-site | Что делает | Влияние удаления LRM/RLM |
| --- | --- | --- |
| `sanitize_telegram_html` (строка 406) | TG-подмножество, до collapse | **целевой фикс**: спейсер → чистый `\xa0` → схлопывается round-39 |
| `sanitize_email_html` (строка 259) | web-инбокс (`body_html`) | положительно: тот же невидимый мусор уходит и из web-вью |
| `linkify_plain_text` (строка 432) | plain-fallback | положительно: чище preview/ссылки |
| `worker/imap_fetcher.py:189` | `body_text` при ingest | положительно; **не конфликтует** — `body_text`-ветка `_format_message_body` зовёт `strip_invisible_padding(body_text)` ещё раз (строка 111), идемпотентно |
| `notify_format.normalize_preview` (строка 106) | push-превью | положительно: LRM/RLM убраны до `_WHITESPACE_RUN_RE` (который их тоже не матчит как whitespace) — больше не «зависают» в превью |

Ни один потребитель не ломается: bidi-форматтеры в marketing-mail — чистые spacer'ы, а вывод — plain-ish текст для Telegram/web, где направление абзаца задаётся первым сильным символом контента, а не явными LRM/RLM-маркерами. `body_text`-ветка `_format_message_body` **не затрагивается негативно** — там `strip_invisible_padding` уже стоит и просто начнёт убирать на 2 кодпоинта больше (идемпотентно с ingest-вызовом).

**Точный набор round-40.** `_INVISIBLE_PADDING_CODEPOINTS` после правки:
```
0x200B  ZERO WIDTH SPACE
0x200C  ZERO WIDTH NON-JOINER
0x200D  ZERO WIDTH JOINER
0x200E  LEFT-TO-RIGHT MARK      <- round-40 (NEW)
0x200F  RIGHT-TO-LEFT MARK      <- round-40 (NEW)
0x2060  WORD JOINER
0xFEFF  ZERO WIDTH NO-BREAK SPACE / BOM
```
Кодпоинты задаются hex-литералами — исходник остаётся без невидимых runtime-символов (ruff PLE2515). **U+00A0 (NO-BREAK SPACE) НЕ добавляется** в набор: это **whitespace**, нужный для схлопывания (его ловит `[^\S\n]` round-39); удаление сломало бы фикс — спейсер-строки перестали бы считаться whitespace-прогоном.

**Взаимодействие с `<pre>` (round-40 trade-off).** `strip_invisible_padding` через `str.translate` удаляет LRM/RLM **везде, включая содержимое `<pre>`**, т.к. вызывается внутри `sanitize_telegram_html` **до** `_TG_PRE_SPLIT_RE`-сплита (сплит защищает `<pre>` только от **collapse**, не от strip). Это **допустимо и желательно**: LRM/RLM невидимы и не несут смысла в код-блоке (`<pre>` в TG — моноширинный текст/код); их удаление **не ломает** отображение кода (переносы `\n` и значимые пробелы/`\xa0` внутри `<pre>` сохраняются — strip трогает только bidi-форматтеры). Контракт round-39 «collapse только вне `<pre>`» **не нарушается** — round-40 не добавляет collapse, только расширяет strip, который и до этого работал везде (200B/200C/… уже убирались внутри `<pre>`). Поведение `<pre>` относительно strip — без изменений (просто +2 кодпоинта).

**RTL trade-off (known, отмечен).** LRM/RLM — bidi-control-символы Unicode. В **подлинно** RTL-письмах (арабский/иврит) они изредка задают направление смешанного LTR/RTL-фрагмента; безусловное удаление в **очень редком** случае может сместить визуальное направление куска текста. Оценка риска — **низкий, допустимый**:
- основной канал — TG/web plain-ish текст-вью, где направление абзаца определяется первым сильным символом контента (Unicode bidi base direction);
- подавляющее большинство LRM/RLM в нашем трафике — marketing-спейсеры (подтверждено Glassdoor `id=1264`), не семантические bidi-маркеры; пользователи — ru/en-mail;
- **NB по хранению:** `worker/imap_fetcher.py:189` зовёт `strip_invisible_padding` при **ingest** для `body_text` — т.е. хранимый `body_text` будет без LRM/RLM (как уже без 4 zero-width сегодня). Это не новость семантики, но для RTL-полноты фиксируем: подлинный bidi-маркер не сохранится в `body_text`. `body_html` стрипается только на render (`sanitize_email_html`/`sanitize_telegram_html`) — хранимый `body_html` остаётся с маркерами.

Заведено как **TD-040** (`100-known-tech-debt.md`): «strip LRM/RLM глобально — теоретическая потеря bidi-направления в подлинно-RTL письмах». **Не блокирует** round-40 (риск ≈0 на текущем трафике).

**Порядок операций (инвариант, для backend): strip Cf → collapse.** Внутри `sanitize_telegram_html`: `strip_invisible_padding` (строка 406, теперь убирает и LRM/RLM) **→** `_COLLAPSE_BLANK_LINES_RE` (`\n{3,}`, строка 412). Затем в `_format_message_body`: `collapse_blank_lines_tg` (round-39). Порядок **strip Cf → collapse** уже соблюдён существующей архитектурой — round-40 ничего не переставляет, только наполняет набор strip'а. **Это причина, почему round-40 — правка одной константы, без изменения `collapse_blank_lines_tg`/`_format_message_body`.**

**Точные правки для backend (round-40):**

1. `shared/html_sanitize.py` — в `_INVISIBLE_PADDING_CODEPOINTS` (строки 41–47) добавить два элемента (между `0x200D` и `0x2060`):
   ```python
   _INVISIBLE_PADDING_CODEPOINTS: Final[tuple[int, ...]] = (
       0x200B,  # ZERO WIDTH SPACE
       0x200C,  # ZERO WIDTH NON-JOINER
       0x200D,  # ZERO WIDTH JOINER
       0x200E,  # LEFT-TO-RIGHT MARK (round-40: marketing preheader spacer)
       0x200F,  # RIGHT-TO-LEFT MARK (round-40: marketing preheader spacer)
       0x2060,  # WORD JOINER
       0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
   )
   ```
   `_INVISIBLE_PADDING_TRANSLATE` (строки 48–50) пересоберётся автоматически (comprehension по кортежу) — отдельной правки **не требует**.
2. Обновить module-docstring (строки 17–21) и комментарий-блок над `_INVISIBLE_PADDING_CODEPOINTS` (строки 34–40): упомянуть LRM/RLM в перечне удаляемых символов и зачем (bidi-спейсеры marketing-mail, мешающие collapse).
3. Обновить комментарий round-39 в коде (где сказано «zero-width НЕ ловятся `[^\S\n]`, но уже удалены strip'ом» — комментарий-блок над `_COLLAPSE_TG_BLANK_RE`, строки ~303–326): добавить, что strip теперь покрывает и LRM/RLM (Cf-форматтеры), поэтому спейсер-строки приходят на collapse как чистый whitespace.
4. **Никаких изменений** в `collapse_blank_lines_tg`, `_COLLAPSE_TG_BLANK_RE`, `_TG_HSPACE`, `_TG_PRE_SPLIT_RE`, `_format_message_body` (`callback_handler.py`), `imap_fetcher.py`, `notify_format.py` — все переиспользуют расширенный `strip_invisible_padding` без правок.

**Edge-cases (round-40):**
- **Эмодзи / обычный текст / ссылки.** `🔥`, буквы, цифры, URL, `<a href>` — не в наборе кодпоинтов, не трогаются. `str.translate` бьёт строго по 7 перечисленным кодпоинтам.
- **`\xa0` (NO-BREAK SPACE).** Намеренно **НЕ** в наборе — нужен как whitespace для round-39. После удаления LRM/RLM спейсер-строка станет цепочкой `\xa0` → whitespace-прогон → схлопнется.
- **Смешанные спейсеры.** Паттерн Glassdoor `"\xa0‎‏"×N` → после strip `"\xa0"×N` (одна длинная whitespace-строка) → `[^\S\n]*` round-39 её поглотит вокруг разрывов.
- **Идемпотентность.** Двойной `strip_invisible_padding` (ingest `body_text` + render `body_text`-ветка) безвреден — `translate` идемпотентен.
- **Подлинно-RTL письмо.** См. RTL trade-off / TD-040 — допустимо для plain-ish вью, риск ≈0.

**Миграций нет.** round-40 — правка одной константы в `shared/html_sanitize.py` (+ комментарии). Схема БД не меняется. `body_html` хранимый не трогается (strip на render). `body_text` хранимый при будущем ingest будет без LRM/RLM (как уже без zero-width). Чинит существующие письма в TG/web-вью на каждом просмотре (render-time strip). Юнит-тесты — задача QA (вне scope architect): `strip_invisible_padding` удаляет U+200E/U+200F и **сохраняет** `\xa0`/эмодзи/текст; через полный `sanitize_telegram_html`→`collapse_blank_lines_tg` Glassdoor-подобный спейсер схлопнут; регрессия `_format_message_body` на Glassdoor-подобном HTML → нет строки-спейсера.

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

#### Изменение (round-37, §2.10): `GET /api/messages/{id}` и `GET /messages/{id}` — поля `body_text` / `body_html` в ответе/контексте теперь **нормализованы для отображения** (схлопнуты прогоны пустых строк/`<br>`/пустых блоков). Контракт схемы `MessageDetail` не меняется (типы те же); меняется только значение — артефакт «множества пустых строк» устранён. Хранимое в БД тело не затронуто.

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

11. **(round-37, §2.10) Нормализовать пустые строки при ingest** (в `worker/app/imap_fetcher.py`, до сохранения `body_text`/`body_html`). Отвергнуто — (а) не исправило бы уже сохранённые письма (потребовался бы реингест/data-миграция); (б) изменило бы хранимое значение, которое используется не только этим UI-вью, но и tag-matching (`body_contains`, ADR-0017) и push-превью (§2.5) — менять источник под нужды одного потребителя неверно; (в) теряется оригинал. Принято — нормализация на render-time в `MessageService.get` (см. §2.10).

12. **(round-37, §2.10) Чистить пустые строки в Jinja-фильтре в `message_view.html`.** Отвергнуто — логика спряталась бы в шаблоне и не покрывала бы JSON-API (`GET /api/messages/{id}`), давая расхождение HTML vs JSON; её сложнее unit-тестировать. Принято — нормализация в backend-service (`MessageService.get` → `MessageDetail`), общая для HTML-страницы и JSON-API; чистые функции в `shared/html_sanitize.py` покрываются unit-тестами.

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
- `backend/app/telegram/sso_service.py` (реальное имя файла; класс `TelegramSSOService`):
  ```python
  class TelegramSSOService:
      async def validate_init_data(init_data: str) -> ValidatedTelegramUser  # HMAC + timestamp, raises InvalidInitData
      async def try_sso(init_data: str, ip: str, ua: str) -> SSOResult  # (linked_session, csrf) | (pending_token,)
      async def link_pending(*, telegram_user_id, user_id, ip, user_agent) -> None       # login_flow (allow_rebind_from_other=True)
      async def link_session_add(*, telegram_user_id, user_id, ip, user_agent) -> None    # session_add (POST /api/telegram/links; allow_rebind_from_other=False)
      async def self_heal_link(*, telegram_user_id, user_id, ip, user_agent) -> None       # round-38 §1.6; best-effort, не raise
      async def revoke_for_user(user_id: int, reason: str = "logout") -> None              # вызывается из auth.logout / admin.reset_password
      # Общий _link(...): для уже-живой привязки того же user (dead_at IS NULL) → NO-OP
      #   (НЕ upsert, created_at не сдвигается, audit не пишется — round-38 critical-fix, §1.6 edge-3).
      #   upsert(created_at=now(), dead_at=NULL) только при INSERT / реактивации dead / rebound.
      #   Правило единообразно для link_pending / link_session_add / self_heal_link.
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
| **round-38:** `tg.js`: initData непустая → POST `/api/telegram/auth` делается ВСЕГДА (gate `data-anonymous` снят, §1.2) | unit (JS) | mock fetch вызван и при наличии `mas_session` |
| **round-38 (self-heal NO-OP, edge-3 critical-fix):** при наличии сессии и УЖЕ-ЖИВОЙ привязке того же user (`user_id=current`, `dead_at IS NULL`) | integration | `_link` НЕ вызывает upsert; `created_at` НЕ меняется; audit `telegram_link_created`/`replaced` НЕ пишется; письма из окна между заходами не теряются для push |
| **round-38 (self-heal реактивация):** привязка того же user, `dead_at IS NOT NULL` | integration | upsert: `dead_at=NULL`, `created_at=now()`; audit `telegram_link_created replaced=true via=self_heal` |
| **round-38 (self-heal rebound):** привязка на ДРУГОГО user | integration | upsert переносит `user_id`, `created_at=now()`; audit `telegram_link_rebound via=self_heal` |
| **round-38 (login_flow/session_add NO-OP):** повторный upsert уже-живой привязки того же user через login_flow / session_add | integration | NO-OP — `created_at` не сдвигается (единообразное правило, §1.6 edge-3) |
| **round-39 (collapse_blank_lines_tg):** прогон смешанных `\n`/`<br>` с whitespace (`\xa0`/` `/`　`) между ними | unit | 3+ разрывов → ровно `\n\n`; одиночный разрыв/пустая строка не тронуты; ведущие/замыкающие убраны |
| **round-39:** `<a href>` внутри прогона | unit | ссылка не разорвана, остаётся кликабельной |
| **round-39:** `<pre>`-блок с переносами | unit | переносы внутри `<pre>` сохранены (collapse применён только вне `<pre>`-сегментов) |
| **round-39:** `_format_message_body` на Apple-подобном HTML (вложенные таблицы, spacer-ячейки) | integration | после `sanitize_telegram_html`+`collapse_blank_lines_tg` нет столба пустых строк; заголовки `Тема:`/`От:` целы |
| **round-39:** пустое тело (`body_html`=`''`/`None`) | unit | `collapse_blank_lines_tg('')==''`; fallback на body_text / `(пустое тело)` |
| **round-40 (strip_invisible_padding):** строка с U+200E/U+200F | unit | LRM/RLM удалены; `\xa0`, эмодзи (`🔥`), обычный текст, URL сохранены |
| **round-40:** Glassdoor-подобный спейсер `"\xa0‎‏"×N` между `\n` через полный `sanitize_telegram_html`→`collapse_blank_lines_tg` | unit | после strip спейсер → `\xa0`-прогон → схлопнут в `\n\n`; нет длинной строки-спейсера |
| **round-40:** `_format_message_body` на Glassdoor-подобном HTML (preheader-спейсер) | integration | нет строки-спейсера в выводе; заголовки `Тема:`/`От:` целы; ссылки кликабельны |
| **round-40:** `<pre>` с LRM/RLM внутри | unit | LRM/RLM удалены и внутри `<pre>` (невидимый мусор), переносы/значимые пробелы `<pre>` сохранены |

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
- `05-modules.md` — расширение модуля 18 (`telegram`) на SSO + dispatcher + bot.send_notification; новый sub-модуль `repositories/telegram_*`, `repositories/user_settings`; изменения в `auth`, `admin`, `worker.sync_cycle`. **round-37:** модуль `messages` (`MessageService.get` нормализует тело при отображении, §2.10) + `shared/html_sanitize` (новые `collapse_blank_lines_text`/`collapse_blank_lines_html`). **round-39:** модуль `telegram` (`callback_handler._format_message_body` — post-sanitize collapse для TG full-body view, §2.10 round-39) + `shared/html_sanitize` (новая `collapse_blank_lines_tg`); web-вью (`message_view.html`) вне scope → `TD-039`.
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
| round-34 | 2026-05-27 | Push-уведомление (§2.5): добавлены строка `Тема:` и превью тела письма (`PREVIEW_LEN=120`), обе опциональные (опускаются при пустом значении). Введены `notify_format.html_to_plain` / `normalize_preview`; превью считается **один раз на письмо** в `notify_service.dispatch_one_payload` (источник — `body_text`, fallback `body_html` через sanitiser). Срез — в Python, не в SQL. Лейбл «Отправитель» сохранён. Миграций нет. |
| round-36 | 2026-05-31 | **Новый формат push-уведомления (§2.5), финализирован.** Карточка из 6 строк с emoji-метками: `🆔:` (ник почты `display_name`‖`email`, **всегда**), `#️⃣:` (все теги через `, ` либо fallback **«Не сортировано»**, **всегда**), пустой разделитель, `Клиент:` (был «Отправитель», **всегда**), `Тема:` (**всегда**; пустая → **«(без темы)»**), пустой разделитель, превью тела (только если непусто). Превью `PREVIEW_LEN` 120 → **100**. Значения выделяются `<b>`, emoji-метки plain; все user-controlled значения `html.escape()`. Убран ранний `if not message_tags: return` (теги опциональны). Константы `PREVIEW_LEN=100`, `SUBJECT_MAX=150` — в `notify_format.py`. Миграций нет. **Код `notify_format.py` приводится к этому формату backend-агентом** (на момент round-36 doc-финала код был на round-34). |
| round-37 | 2026-06-01 | **Bug «множество пустых строк» при ПРОСМОТРЕ письма (§2.10, новый раздел).** При открытии `GET /messages/{id}` (web и TG `?embed=tg`) тело Apple/маркетинговых писем показывалось с 15+ подряд пустыми строками (HTML-ветка: пустые блочные элементы + прогоны `<br>` не схлопывались `sanitize_email_html`; plain-ветка: прогоны `\n\n\n` от `html2text` в `<pre>`). Решение — нормализация **на render-time** (не при ingest): две чистые функции `collapse_blank_lines_text` / `collapse_blank_lines_html` в `shared/html_sanitize.py`, применяемые в `MessageService.get` к `MessageDetail.body_text`/`.body_html`. Схлопывает 3+ пустых строк / 3+ `<br>` / пустые `<p>`/`<div>` в максимум один разделитель абзаца. Хранимое body НЕ меняется (чинит и существующие письма, не ломает tag-matching/push-превью). Общая логика для HTML-страницы и JSON-API. Миграций нет. |
| round-39 | 2026-06-01 | **Bug «множество пустых строк» в TG «Посмотреть сообщение» (§2.10).** TG full-body view строится не через `MessageService.get`, а через `_format_message_body` → `sanitize_telegram_html`, который стрипает Apple-таблицы и рождает строки-отступы `\n`+whitespace; round-37 их не закрывал. Решение — новая `collapse_blank_lines_tg` (`shared/html_sanitize.py`), применяемая в HTML-ветке `_format_message_body` поверх `sanitize_telegram_html`. Широкий класс `[^\S\n]` (вкл. `\xa0`/em/ideographic space) + смешанные `\n`/`<br>`-разрывы; `<pre>` защищён внутренним сплитом. Web-вью — вне scope (TD-039). Миграций нет. |
| round-40 | 2026-06-02 | **Bug «строка-спейсер не схлопывается» в TG «Посмотреть сообщение» (§2.10, Glassdoor `id=1264`).** После round-39 marketing-письма сохраняли длинную preheader-строку-спейсер `"\xa0‎‏"×N` (U+00A0 + U+200E LRM + U+200F RLM): класс `[^\S\n]` round-39 матчит `\xa0`, но НЕ Cf-форматтеры LRM/RLM → строка «непустая» → не схлопывается. Корень — `_INVISIBLE_PADDING_CODEPOINTS` не содержал 200E/200F, поэтому `strip_invisible_padding` (вызывается в `sanitize_telegram_html` до collapse) их не убирал. Решение — **расширить `_INVISIBLE_PADDING_CODEPOINTS`** на `0x200E`/`0x200F` (не новая функция; унификация — фикс для всех 5 потребителей `strip_invisible_padding`). Спейсер → чистый `\xa0` → схлопывается штатно round-39. `\xa0` НЕ добавляется (нужен как whitespace). Порядок strip Cf → collapse уже соблюдён. `collapse_blank_lines_tg`/`_format_message_body` не меняются. RTL trade-off → TD-040 (риск ≈0). Миграций нет. |

