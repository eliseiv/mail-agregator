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

### 1.7 Admin actions (super_admin / group_leader)

После ADR-0019 у пользователей трёхуровневая модель ролей: `super_admin` (один, из env), `group_leader`, `group_member`. Admin-роуты (`/admin/*`, `/api/admin/*`) доступны **только** для `super_admin`. `group_leader` имеет расширенные права на mail-аккаунты и сообщения **в рамках своих команд** (через `VisibilityScope`), но **не на user-management** — создание/удаление/reset пользователей доступны только super_admin'у.

**ADR-0030 (multi-group).** Пользователь может состоять в **нескольких** командах одновременно (M:N-таблица `user_groups` — источник истины видимости/уведомлений; `users.group_id` сохраняется как «домашняя» команда). `VisibilityScope` несёт множество `group_ids` (все команды пользователя). Правила управления членствами: добавлять можно **всех, кроме `super_admin`**; **перенос лидера запрещён** (move отклоняется для `group_leader` — нарушил бы инвариант лидера); add/remove/move **ревокируют все сессии** цели, чтобы scope перечитался. Эти угрозы и митигации — в таблице §1.7 ниже.

| Угроза | Описание | Митигация |
| --- | --- | --- |
| S | Spoof admin via cookie theft | Защита та же, что для user (sec 1.1); session-payload в Redis содержит `role` и `group_id` (см. ADR-0019 §10), проверяется на каждом admin endpoint через `require_super_admin` dependency |
| T | Privilege escalation | `role` и `group_id` фиксируются при создании сессии из БД, не из cookie/JWT. Создание `super_admin` через API запрещено (`AdminService.create_user` отвергает `role='super_admin'`); единственный путь — `seed_super_admin` из env |
| T | Stale role/group в существующей сессии | При `PATCH /api/admin/users/{id}` (изменение role или group_id) backend вызывает `SessionStore.revoke_all_for_user(target_user_id)` — все активные сессии target-user'а инвалидируются, новая сессия будет иметь актуальные `role`/`group_id` (см. ADR-0019 §10) |
| T | Group-scope bypass лидером | Лидер/участник пытается видеть/менять чужой mail-account через подмену `account_id`. Защита: каждый Service-метод (accounts/messages) принимает `VisibilityScope` и строит WHERE-фильтр по **членствам** зрителя — `mail_accounts.group_id ∈ scope.group_ids` (множество команд из `user_groups`, ADR-0030; ранее — единичное `users.group_id = scope.group_id`, ADR-0019 §7); чужой `account_id` → 404. См. ADR-0019 §7 + ADR-0030 |
| T | Stale `group_ids` после изменения членства | При add/remove/move членства (`POST`/`DELETE /api/admin/users/{id}/groups`, `PATCH /api/admin/users/{id}`) backend вызывает `SessionStore.revoke_all_for_user(target_user_id)` — все сессии цели инвалидируются, новая сессия перечитает `VisibilityScope.group_ids` из `user_groups` (ADR-0030, как уже делается при смене группы по ADR-0019 §10) |
| E | Добавление `super_admin` в команду | `POST /api/admin/users/{id}/groups` с целью `super_admin` → `400 cannot_add_super_admin_to_group` (он и так видит всё; членства нарушили бы инвариант `super_admin → group_id IS NULL` и «нет строк в `user_groups`», ADR-0030) |
| E | Перенос лидера ломает инвариант лидера | «Переместить» (`PATCH /api/admin/users/{id}` со сменой `group_id`) для `role='group_leader'` → `409 cannot_move_group_leader`. Смена домашней команды лидера нарушила бы инвариант лидера ADR-0019 §6; лидеру доступно только «Добавить в команду» (доп. членство в `user_groups`). См. ADR-0030 |
| E | Удаление домашнего членства | `DELETE /api/admin/users/{id}/groups/{group_id}` с `group_id == users.group_id` → `400 cannot_remove_home_membership`; домашняя команда меняется только через «Переместить». См. ADR-0030 |
| T | Cross-group target_user_id при create mail-account | Лидер передаёт `target_user_id` участника чужой группы. Backend проверяет `target_user.group_id == scope.group_id`, иначе `403 user_not_in_group_scope` (см. ADR-0019 §8) |
| E | Положить новый ящик в чужую команду (create `group_id`) | `POST /api/mail-accounts` с `group_id` вне допустимости инициатора. Единая функция `_validate_target_group` (ADR-0031 §4): `group_member` — `group_id ∈ его user_groups`, только на себя; `group_leader` себе → его `user_groups`, участнику своей команды → его команда; `super_admin` — любая/`NULL`. Вне scope → `403 user_not_in_group_scope`; несуществующая → `404 group_not_found`; `NULL` для не-super_admin → `403 user_not_in_group_scope`. **Никогда `500`** (см. ADR-0031) |
| E | Перенос ящика в чужую команду (transfer `group_id`) | `PATCH /api/mail-accounts/{id}` с `group_id`. Сам ящик должен быть виден инициатору (`get_in_scope`, иначе `404 not_found`). Целевая команда валидируется той же `_validate_target_group`. `group_leader` — только свои команды/команда участника; `super_admin` — любая/`NULL`. Вне scope → `403 user_not_in_group_scope`; несуществующая → `404 group_not_found`. **Никогда `500`** (ADR-0031 §3/§4) |
| E | `group_member` переносит существующий ящик | `PATCH /api/mail-accounts/{id}` с `group_id` от `group_member` → `403 forbidden` **всегда** (даже на свой ящик): смена принадлежности существующего ящика команде — административно-значимое действие, недоступное участнику. Выбор команды разрешён только при **создании** своего ящика (ADR-0031 §4) |
| R | Скрытие переноса ящика между командами | `PATCH /api/mail-accounts/{id}` с реальной сменой `group_id` пишет `admin_audit` `mail_account_group_change` (`details={mail_account_id, from_group_id, to_group_id}`, `actor=инициатор`, `target_user_id=владелец ящика`). Создание ящика с выбором команды audit **не** пишет (часть обычного create-flow). См. ADR-0031 §6 |
| R | Скрытие действий | Все super-admin actions пишут `admin_audit`. Расширены actions: `group_create`, `group_delete`, `group_rename`, `user_role_change`, `user_group_change` (см. ADR-0019 §9), **`user_group_add`, `user_group_remove`** (add/remove дополнительного членства, ADR-0030). Действия group_leader / group_member **не** пишутся в audit — это обычные user-actions, structlog в stdout достаточен |
| I | — | — |
| D | Brute admin password | Тот же rate-limit + lockout (ADR-0009) |
| E | Self-delete admin | Endpoint отказывает (`cannot_delete_admin`) |
| E | Удаление лидера через `DELETE /api/admin/users/{id}` | Невозможно — `groups.leader_user_id ON DELETE RESTRICT` блокирует. Backend возвращает `409 conflict` с `details.reason='user_is_group_leader'`. Super-admin сначала удаляет/распускает группу, потом — user'а |
| E | Удаление группы с участниками | `DELETE /api/admin/groups/{id}` отвергает с `400 group_has_members`, если в группе есть users (включая лидера). Super-admin сначала переводит/удаляет участников и лидера. Каскад `users.group_id ON DELETE SET NULL` остаётся как safety-net на случай прямого DDL обхода (см. ADR-0019 §4) |

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

**ADR-0022 partially supersedes** часть «без линковки / без initData auth» из этой секции и ADR-0018. См. секцию 1.9 ниже.

### 1.9 Telegram persistent SSO + push-нотификации (ADR-0022)

| Угроза | Описание | Митигация |
| --- | --- | --- |
| S | Поддельный `init_data` от злоумышленника | HMAC-SHA256 валидация с `secret_key = HMAC_SHA256("WebAppData", TELEGRAM_BOT_TOKEN)` (см. https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app). Неверная подпись → 401 `invalid_init_data`, никаких side effects. |
| S | Replay украденного `init_data` | `auth_date` TTL = 5 минут (env `TG_AUTH_INIT_DATA_TTL_SEC=300`); по умолчанию Telegram даёт свежий `initData` при каждом открытии WebApp. Внутри окна replay теоретически возможен, но требует MitM между Telegram и backend — TLS+secret-проверка отсекает. Anti-replay set в Redis не реализован в MVP (см. ADR-0022 Q-001-1). |
| T | Подмена `telegram_user_id` в payload | Невозможна — `user` поле подписано HMAC'ом. Любая мутация → 401. |
| T | Подмена `mas_tg_pending` cookie | Token random 32 байта (base64url, `secrets.token_urlsafe(32)`); HttpOnly + Secure + SameSite=Lax + 15min TTL; одноразовый (deleted в Redis после link или после `mas_session` создан). |
| T | Brute-force HMAC без bot-token | HMAC-SHA256 неразрешим без ключа. Rate-limit `30/min per IP` + `10/min per telegram_user_id` (после успешной валидации) отсекает скан. |
| R | Скрытие факта линковки/разлинковки | Новые actions в `admin_audit`: `telegram_link_created` (с `details.replaced: bool`), `telegram_link_rebound` (ADR-0024 — TG перепривязан с другого user'а), `telegram_link_revoked` (**round-43:** `reason='user_unlink'` — явная отвязка одного TG (`details.telegram_user_id`); `reason='password_reset'`/`'link_user_missing'` — массовый отзыв (`details.telegram_user_ids: [...]`). **logout БОЛЬШЕ НЕ пишет эту запись** — расцеплён с привязкой, ADR-0022 §1.5 / ADR-0024 §5 round-43), `telegram_link_dead_marked` (с `details.reason='bot_api_403'\|...`), `telegram_link_limit_reached` (ADR-0024 — достигнут `TG_MAX_LINKS_PER_USER`), `telegram_link_collision` (**deprecated** — ADR-0024 §3, больше не пишется; запись остаётся читаемой для истории). Все super_admin может видеть в `/admin/audit`. |
| I | Утечка `init_data` через логи | `init_data` НЕ логируется в полной форме (содержит подписанные PII — user name, потенциально username). Логируем только `telegram_user_id` и `auth_date` ПОСЛЕ валидации. |
| I | Утечка bot-token → выпуск произвольных сессий | Знание bot-token + любого валидного `telegram_user_id` залинкованного user'а позволит атакующему выпустить себе HMAC + получить сессию. Митigация: bot-token строго в env + redact-list (ADR-0014); при компрометации — массовый `DELETE FROM telegram_links` + ротация bot-token + ротация секрета webhook'а. |
| D | DOS на `/api/telegram/auth` | Rate-limit slowapi 30/min per IP + 10/min per telegram_user_id. HMAC валидация дешёвая (~µs). |
| E | Один tg-user → две разные линковки → escalation | Невозможно: PK на `telegram_user_id` гарантирует ровно одну активную линковку. `INSERT … ON CONFLICT DO UPDATE` перезаписывает атомарно; audit фиксирует `replaced=true`. |
| E | Получение чужой сессии через подмену `mas_tg_pending` | Token random, Redis-backed; чужой token не пройдёт `GET tg_pending:{token}` (вернёт None). Без cookie pending-flow не активируется. |

#### Push-нотификации

| Угроза | Описание | Митигация |
| --- | --- | --- |
| S | Атакующий получает уведомление о письме чужого user'а | Recipient SQL фильтрует по `visibility scope` (super_admin / **членство в команде ящика через `user_groups`**, ADR-0030 / owner) + наличию **своего тега** у recipient'а (per-user `tags`/`message_tags`). Получатель видит ровно то, что мог бы увидеть в UI — член N команд получает уведомления по письмам всех своих команд. (Предикат `u.group_id = ma.group_id` заменён на `EXISTS (SELECT 1 FROM user_groups ug WHERE ug.user_id = u.id AND ug.group_id = ma.group_id)`.) |
| T | Подмена контента уведомления через injection (HTML/Markdown) | Все user-controlled значения (`tag.name`, `from_addr`, `from_name`, `mail_account.display_name`, `mail_account.email`) экранируются `html.escape()` перед формированием HTML-строки для Bot API parse_mode=HTML. |
| R | Скрытие факта доставки уведомления | Запись в `telegram_notifications (message_id, user_id, sent_at, telegram_message_id)` для каждой доставки. |
| I | Утечка содержимого письма через notification | Текст уведомления содержит только email-адрес ящика, имя/email отправителя, имена тегов и короткое превью (≤100 симв.) — не полное тело. Кнопка «Посмотреть сообщение» — `callback_data "msg:{id}"` (Bug-fix #5, ADR-0022 §2.5/§2.6): тап шлёт `callback_query` на webhook, который **резолвит владельца по подписанному Telegram `from.id`** (`telegram_links`, живая привязка) и грузит письмо **под visibility-scope этого user'а** (ADR-0019) перед отправкой тела в чат. Без живой привязки или при потере доступа (смена группы) — отказ/404. Доступ к телу контролируется на стороне сервера в `callback_handler`, не доверяя клиенту. |
| D | Spam-rate exceeded → user-блокировка бота | Получатель сам контролирует через opt-out (`users_settings.tg_notifications_enabled=false`). При 403 от Bot API (user заблокировал) — линковка mark-dead, спам прекращается автоматически. **ADR-0024:** mark-dead изолирован per `telegram_user_id` — блокировка в одном чате не отключает остальные привязки того же user'а. |
| T | (ADR-0024) Пользователь отвязывает чужой TG | `DELETE /api/telegram/links/{tg_user_id}` фильтрует WHERE `user_id=session AND telegram_user_id=path` — нельзя удалить привязку, не принадлежащую вызывающему. |
| E | (ADR-0024) Добавление второго TG к чужому аккаунту | `POST /api/telegram/links` привязывает только к `session.user_id`; если `telegram_user_id` уже принадлежит другому — `409 tg_link_owned_by_other` (перепривязка из чужого аккаунта только через login-flow с паролем). Мягкий лимит `TG_MAX_LINKS_PER_USER` против абьюза. |
| D | Очередь `tg_notify_queue` забивается при outage Bot API | Backoff на 429; transient/5xx → re-LPUSH (max 1 in-place retry, дальше — следующий тик). Recovery_scan покрывает потерянные. Bot API quota (~30 msg/sec) выше нашего ожидаемого rate (~5 msg/sec пик). |
| E | Получатель видит уведомление о письме, которое не должен видеть | Recipient SQL построен поверх той же `VisibilityScope` модели (членства через `user_groups`, ADR-0030), что и UI — никаких асимметрий. Полный SQL — ADR-0022 §2.2. |
| I | Push-боты ADR-0027 и multi-group | Push-боты ADR-0027 (broadcast по `account.group_id` → фиксированные `ADMIN_TELEGRAM_IDS` из `.env`) **не зависят** от членств пользователей и multi-group их **не затрагивает** — выбор бота по команде ящика, получатели заданы статически. Учёт членств — только у основного notification-бота ADR-0022 (см. строку S выше). См. ADR-0030 §Decision 6. |

### 1.10 Outbound webhooks для команд (ADR-0023)

| Угроза | Описание | Митигация |
| --- | --- | --- |
| S | Spoof receiver'а (атакующий imitate'ит наш URL) | Не применимо — мы инициируем POST, получатель статичен (URL зафиксирован лидером). Атакующий мог бы поднять receiver, если получит URL, но без `X-Webhook-Secret` валидация не пройдёт; secret меняется через rotate. |
| S | Receiver получает POST от non-нас (replay / атакующего с украденным secret) | Static-secret схема не защищает от replay в полной мере — это accepted risk MVP (см. ADR-0023 «Alternatives 2: HMAC-signature отклонён» и Q-WH-1). Mitigation: secret хранится в env receiver'а (его ответственность); ротация при подозрении через `POST /api/webhooks/me/rotate-secret`. Если потребуется replay-resistance — отдельный ADR с HMAC-signature + timestamp. |
| T | Подмена payload в полёте | TLS до receiver'а (URL обязан быть `https://`); httpx валидирует cert. |
| T | Подмена URL злоумышленником с украденной сессией лидера | Cookie-based session + CSRF — те же гарантии, что для других endpoint'ов. Аудит `webhook_updated` фиксирует изменение URL. |
| R | Скрытие создания/удаления/ротации/dead-mark | Новые actions в `admin_audit`: `webhook_created`, `webhook_updated`, `webhook_deleted`, `webhook_secret_rotated`, `webhook_dead_marked`. Все super_admin видит в `/admin/audit`. |
| I | Утечка secret через логи | `secret_plaintext`, `X-Webhook-Secret`, `secret` — в structlog redact-list (рядом с `MAIL_ENCRYPTION_KEY`, `TELEGRAM_BOT_TOKEN`). |
| I | Утечка secret через response_excerpt receiver'а | Receiver может в response body вернуть echo своих headers (вкл. наш `X-Webhook-Secret`). Мы храним только первые 500 байт `resp.text`; backend-агент при mark_sent/mark_failed применяет redact на excerpt (поиск substring `X-Webhook-Secret:` и замена value на `<redacted>`). |
| I | Утечка БД → расшифровка secret_encrypted | Без `MAIL_ENCRYPTION_KEY` blob бесполезен; AAD по `webhook_id` дополнительно блокирует перестановку blob между webhook'ами. См. §2 ниже. |
| I | Утечка через webhook payload (receiver видит письмо без права) | Receiver — внешняя система; лидер сам выбирает receiver. По дизайну ADR-0023 webhook принадлежит **команде**, и payload содержит данные, видимые этой команде (через `VisibilityScope` на `mail_accounts.group_id`). Если receiver — недоверенный сторонний сервис, ответственность лидера: настройка собственной фильтрации на стороне receiver. |
| D | DOS receiver'а массовым потоком POST'ов | `WEBHOOK_BATCH_SIZE=30` за тик (~6 POST/sec пик при full batch). Если receiver медленный — наши таймауты 10s ограничивают cycle. Recovery_scan не флудит — `LIMIT 5000`, `WHERE NOT EXISTS` фильтрует уже доставленные. |
| D | Receiver падает массово (5xx storm) | Через 24 ч окно recovery_scan перестаёт пытаться (письмо устаревает); `dead_at` ставится только при последовательных 4xx (`≥ 10`) или 410, не при 5xx. Лидер должен починить receiver — наши логи показывают `last_error`. |
| D | Нагрузка БД на recovery_scan | `LIMIT 5000` + индексы по `webhook_deliveries(message_id)` + `messages(fetched_at)` + EXISTS-проверки. Тик раз в час — нагрузка ничтожна. |
| E | SSRF: лидер указывает `https://localhost:<port>` или `https://10.x.y.z` → сканирование внутренней сети | Lexical reject `localhost`/`127.0.0.1`/`0.0.0.0`/`[::1]`; DNS-резолв всех A/AAAA + проверка на приватные CIDR (см. §4 ниже). При попадании → `400 webhook_url_private_ip` на CRUD. В диспатчере на момент POST — accepted risk (DNS cache poisoning), `dead_at` если decrypt failed по любой причине. `httpx.AsyncClient(follow_redirects=False)` — 3xx → треатируется как failed, не следует за redirect (защита от Location: внутренняя сеть). |
| E | Чужой лидер настроил webhook на чужую команду | Невозможно: `scope.group_id` строго фиксируется на сессии (`group_leader`) или явно через `?group_id=` (только `super_admin`). `group_member` отвечен 403 на всех endpoint'ах. |
| E | Compromise `MAIL_ENCRYPTION_KEY` → расшифровка всех secret'ов всех webhook'ов | Те же последствия, что для `mail_accounts.encrypted_password` (ADR-0005). Ротация ключа через `mas-cli reencrypt` обрабатывает `webhooks.secret_encrypted` наравне; после ротации **обязательная массовая rotate-secret** через `POST /api/webhooks/me/rotate-secret` (потому что receiver'ам поставляется plaintext-secret, который мог утечь вместе с ключом). Backend-агент при реализации `mas-cli reencrypt` добавляет `webhooks` в список обрабатываемых таблиц. |

### 1.11 OAuth2 Outlook (ADR-0025)

| Угроза | Описание | Митигация |
| --- | --- | --- |
| S | CSRF / authorization-code injection на callback | `state` (32B random) в Redis `oauth_state:{state}` TTL 600с, привязан к инициировавшему `user_id`, одноразовый (GET+DEL). Callback без валидного state → `400 oauth_state_invalid`. PKCE S256 (`code_verifier` хранится со state) защищает от code-interception. |
| S | Атакующий подсовывает свой `code` | code-обмен требует `client_secret` + `code_verifier` (PKCE); без них token endpoint Microsoft отклонит. Полученный токен привязывается к `user_id` из state, не к произвольному. |
| T | Подмена `redirect_uri` | `redirect_uri` зарегистрирован в Azure App и проверяется Microsoft при authorize и token-обмене; mismatch → отказ на стороне Microsoft. |
| R | Скрытие подключения OAuth-аккаунта | Audit `oauth_account_linked` (`mail_account_id`, `email`, `scopes`); инвалидация — `oauth_refresh_invalidated`. |
| I | Утечка БД → расшифровка refresh/access токенов | `oauth_refresh_token_encrypted`/`oauth_access_token_encrypted` — AES-256-GCM (`MailPasswordCipher`, AAD=`account_id`, ADR-0005). Без `MAIL_ENCRYPTION_KEY` blob бесполезен; AAD блокирует перестановку между аккаунтами. См. §2.2. |
| I | Логирование токенов / code / client_secret | `access_token`, `refresh_token`, `code`, `client_secret`, `OUTLOOK_CLIENT_SECRET`, `id_token` — в structlog redact-list (рядом с `MAIL_ENCRYPTION_KEY`/`TELEGRAM_BOT_TOKEN`). Ответы token endpoint не логируются целиком. |
| I | Избыточные scopes → доступ шире необходимого | Только delegated `IMAP.AccessAsUser.All`, `SMTP.Send`, `offline_access`, `openid`, `email`, `profile`. Никаких Mail.ReadWrite/широких Graph. |
| E | Compromise `MAIL_ENCRYPTION_KEY` → расшифровка всех refresh-токенов | Те же последствия, что для `mail_accounts.encrypted_password`. После ротации ключа `mas-cli reencrypt` обрабатывает `oauth_refresh_token_encrypted`/`oauth_access_token_encrypted` наравне; access-кэш можно просто инвалидировать (перевыпустится по refresh). |
| D | Refresh инвалидирован (Microsoft `invalid_grant`) | `oauth_needs_consent=true`, sync аккаунта пропускается, UI показывает «переподключить»; не зацикливаемся на failed-refresh. |

> SSRF к Outlook IMAP/SMTP не релевантен (хосты фиксированы Microsoft'ом, не вводятся пользователем). Per-account proxy (`proxy_url`) заложен, но не используется (TD-029) — трафик идёт напрямую.

### 1.12 Push-only боты по командам + push-callback (ADR-0027, round-42)

3 push-бота (`ivan`/`alexandra`/`andrei`) шлют письма своей команды (по `group_id`) на `ADMIN_TELEGRAM_IDS`. round-42 добавил каждому собственный webhook для callback-кнопки «Посмотреть сообщение». Модель прав push-callback **отличается** от основного бота (§1.9): авторизация по членству в `ADMIN_TELEGRAM_IDS` (`.env`), а не по `telegram_links`→`user`→visibility.

| Угроза | Описание | Митигация |
| --- | --- | --- |
| S | Поддельный push-webhook от чужого процесса | Per-бот `BOT_{NAME}_WEBHOOK_SECRET` (32 hex), проверяется в header `X-Telegram-Bot-Api-Secret-Token` (constant-time, `secrets.compare_digest`), симметрично основному webhook. Несовпадение / ненастроенный бот / неизвестный `bot_name` → `not_found` (неперечислимо — роут неотличим от «нет такого пути»). Rate-limit 60/min per IP до secret-проверки. |
| E | Не-админ открывает тело письма по callback | Тело отдаётся **только** если `callback_query.from.id ∈ admin_telegram_ids`. `from.id` подписан Telegram (доказан до webhook'а). Не админ → `answerCallbackQuery` «Нет доступа», тело не показывается. Push-админ идентифицируется по id в `.env`, **не** по `telegram_links` (у него может не быть `user`-строки). |
| E | Админ вытягивает письмо чужой команды подделкой `msg:{id}` | DEFENSIVE group-match: загруженное письмо обязано принадлежать группе этого бота (`mail_accounts.group_id == BOT_{NAME}_GROUP_ID`); mismatch → ignore («Сообщение недоступно»), лог `push_callback_group_mismatch`. Кнопку шлёт бот команды X → через webhook бота X можно достать только письма команды X. |
| T | Подмена контента / HTML-injection в теле письма | Тело проходит `sanitize_telegram_html` + `collapse_blank_lines_tg` (round-39/41, тот же pipeline, что основной callback `_format_message_body`); Bot API принимает только whitelist-теги. |
| I | Утечка токена / webhook-secret push-бота через логи | `BOT_{IVAN,ALEXANDRA,ANDREI}_TOKEN` и `BOT_{…}_WEBHOOK_SECRET` — в structlog redact-list рядом с `TELEGRAM_WEBHOOK_SECRET`. push-webhook не логирует secret (ни URL, ни header). |
| I | Компрометация токена push-бота | Атакующий может слать сообщения от имени бота 2 известным админам (фишинг) и прислать себе callback-кнопку, но: тело письма не отдаётся (не админ → deny), доступа к системе/письмам нет (бот push-only, без БД/SSO). Митигация: ротация токена (BotFather `/revoke`) + `webhook_secret` (`openssl rand -hex 16`), обновление `.env` (api+worker), повтор push-`setWebhook`. |
| D | Шквал spoofed push-webhook'ов | Rate-limit 60/min per IP (тот же `_LIMIT_TG_WEBHOOK`), `not_found` на secret-fail — после rate-limit. |
| E | Inbound-команды (`/start`) к push-боту | push-webhook обрабатывает **только** `callback_query`; любой `message`/`/start`/прочее тихо дропается (200). Inbound-поверхность сведена к одному типу update — нет launcher/SSO-вектора. |

> Доставка уведомлений остаётся fire-and-forget (TD-041) — webhook касается только inbound-callback, не исходящей доставки. См. [ADR-0027](./adr/ADR-0027-push-team-bots.md) §8/§10/§11.

### 1.13 External pull-API для стороннего сервиса (ADR-0029)

**Trusted external pull.** Доверенный B2B-партнёр опрашивает `GET /api/external/messages` и забирает ВСЕ письма системы (super_admin visibility). Аутентификация — **single-factor static `EXTERNAL_API_KEY`** (`X-API-Key` / `Authorization: Bearer`, constant-time `secrets.compare_digest`). Read-only, без cookie-сессии/`VisibilityScope`. См. [ADR-0029](./adr/ADR-0029-external-pull-api.md).

| Угроза | Описание | Митигация |
| --- | --- | --- |
| S | Атакующий выдаёт себя за партнёра без ключа | Каждый запрос требует валидный `EXTERNAL_API_KEY` (256 бит, `openssl rand -hex 32`); `compare_digest` constant-time. Без/неверный ключ → `401 not_authenticated`. |
| S | Brute-force ключа | `LIMIT_EXTERNAL_API` (`120/min` per IP) consume **ДО** сравнения ключа — anti-flood. 256-битный ключ практически неперебираем. |
| T | Подмена данных в транзите | Только через nginx :443 (TLS), как остальной API. |
| R | Скрытие доступа | structlog-событие на каждый запрос: `client_ip`, `since_id`, `limit`, `returned_count` (без ключа). Audit-таблица **не** пишется (нет per-user actor; это machine-to-machine read; достаточно application-логов — симметрично решению по `group_leader`/`group_member` read-actions, §8). |
| I | Утечка ключа через логи | `EXTERNAL_API_KEY`, `X-API-Key`, `Authorization` — в structlog redact-list (рядом с `MAIL_ENCRYPTION_KEY`/`TELEGRAM_BOT_TOKEN`). Значение ключа не логируется ни в каком виде. |
| I | Партнёр видит ВСЕ письма (single-factor, broad scope) | **Accepted risk** (явное требование). Mitigations: ключ в env (`chmod 600`), read-only (только поля письма — нет паролей/токенов/IMAP-UID/secret'ов), ротация при подозрении (смена `EXTERNAL_API_KEY` + `force-recreate api`). Single-factor — компрометация ключа = доступ ко всем письмам; нет per-client-ключей / отзыва в MVP (один партнёр). Несколько партнёров / отзыв → отдельный ADR (таблица hash-ключей). |
| I | Утечка чувствительных полей (пароли/токены) через payload | DTO `ExternalMessageDTO` whitelist'ит **только** поля письма (`id`/`subject`/`internal_date`/`from_*`/`to_addrs`/`cc_addrs`/`mail_account.{id,email,display_name}`/`body_*`/`tags`). Не делит код с UI-DTO и не имеет доступа к `encrypted_password`/`oauth_*`. |
| D | DoS опросом | `LIMIT_EXTERNAL_API` + `limit≤200` cap; read-only keyset по PK (`messages.id`) — дешёвый запрос, без фоновой работы на нашей стороне. |
| E | Enumeration «включена ли фича» | «Фича выключена» (`EXTERNAL_API_KEY` пуст) и «неверный ключ» возвращают **одинаковый** `401 not_authenticated` — конфиг неперечислим. |
| E | Replay перехваченного запроса | Static-key схема не защищает от replay (как ADR-0023). Accepted risk MVP — запрос read-only (идемпотентен, не меняет состояние); TLS защищает транзит. HMAC/nonce — отдельный ADR при необходимости. |

> Ротация ключа: смена `EXTERNAL_API_KEY` в `.env` → `docker compose up -d --force-recreate api`; партнёр обновляет ключ синхронно. Старый ключ немедленно недействителен (нет grace-периода). См. §10 (таблица ротации).

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

### 2.1 Outbound webhook secret storage (ADR-0023)

`webhooks.secret_encrypted` использует **тот же примитив** AES-256-GCM (`shared/crypto.py::MailPasswordCipher`) с **другим AAD-префиксом** для domain-separation:

```
plaintext (UTF-8 string, 44 chars = secrets.token_urlsafe(32))
   │
   ├── key  = base64decode(env.MAIL_ENCRYPTION_KEY)        # тот же 32-byte ключ; общий с mail-passwords
   ├── iv   = os.urandom(12)                                # 96 bits
   ├── aad  = b"webhook_secret|" + str(webhook_id).encode("ascii")
   ▼
ciphertext + tag = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), aad)
   │
   ▼
blob = b"\x01" || iv (12B) || ciphertext_with_tag (variable)
```

**Domain separation через AAD:** prefix `b"webhook_secret|"` отличается от `b"mail_account_password|"` (см. §2 выше) → атакующий, имеющий доступ к БД, **не может** взять blob из `mail_accounts.encrypted_password` и подставить в `webhooks.secret_encrypted` — расшифровка упадёт на `InvalidTag`, потому что AAD не совпадает. Аналогично — нельзя переставить blob между двумя webhook'ами (AAD биндинг по `webhook_id`).

**Невозможность INSERT без id**: `nextval('webhooks_id_seq')` → шифрование с этим id в AAD → INSERT с явным id (тот же паттерн, что у `mail_accounts`).

**One-time-show в API response:**
- `POST /api/webhooks/me` и `POST /api/webhooks/me/rotate-secret` возвращают `secret` plaintext **только** в response этого конкретного запроса (поле `secret`).
- Никакого `GET`-эндпоинта, отдающего plaintext-secret, **нет**.
- HTML-страница `/my/integrations` показывает plaintext **один раз** через one-shot flash-сообщение категории `secret_reveal` (cleared при следующем GET).
- Plaintext secret попадает в structlog redact-list по ключам `secret`, `secret_plaintext`, `X-Webhook-Secret` — никогда не логируется.

**Ротация:**
- Лидер может в любой момент сделать `POST /api/webhooks/me/rotate-secret` — генерируется новый `secret_plaintext`, шифруется с тем же `webhook_id` (новый IV → новый blob), UPDATE row. Старый secret немедленно недействителен; receiver обязан получить новый и обновить своё хранилище.
- Rate-limit: 5/час per webhook_id (защита от accidental DoS на receiver-side rotation logic).
- Двойного secret (старый ещё валиден M минут) в MVP **нет** (см. ADR-0023 Q-WH-1 / TD-019).

**Ротация мастер-ключа `MAIL_ENCRYPTION_KEY`** (см. §10 ниже): `mas-cli reencrypt` обрабатывает blob'ы обеих таблиц (`mail_accounts.encrypted_password` + `webhooks.secret_encrypted` + **OAuth-токены `mail_accounts.oauth_refresh_token_encrypted`/`oauth_access_token_encrypted`**, ADR-0025) — общий механизм `version_byte` (0x00 = старый ключ, 0x01 = новый). После ротации **обязательная массовая `POST /api/webhooks/me/rotate-secret`** через UI, потому что plaintext-secret уже передан receiver'у и считается потенциально скомпрометированным.

### 2.2 OAuth2 token storage (ADR-0025)

`mail_accounts.oauth_refresh_token_encrypted` и `oauth_access_token_encrypted` используют **тот же примитив** AES-256-GCM (`MailPasswordCipher`) с **тем же AAD-префиксом**, что и пароли (`b"mail_account_password|" + account_id`) — токены живут в той же таблице `mail_accounts`, AAD по `account_id` блокирует перестановку blob между аккаунтами. (Domain-separation между password-blob и token-blob не нужна: оба принадлежат одному `account_id`; разные колонки разделяют их структурно.)

- **refresh-token** — источник истины OAuth-доступа; шифруется при создании/каждом rotation (Microsoft может вернуть новый refresh при refresh-grant).
- **access-token** — кэш (~1ч), шифруется; при потере перевыпускается по refresh (не критичен).
- **Не логировать**: см. §1.11 redact-list.
- `INSERT` с предсказанным id (`nextval('mail_accounts_id_seq')`) — тот же паттерн, что для password-аккаунтов (см. §2 / модуль `crypto`).

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

### 4.1 SSRF-защита для outbound webhook URL (ADR-0023)

Тот же helper, что в §4 выше, применяется к URL'у webhook'а в трёх точках:

1. **`POST /api/webhooks/me` / `PATCH /api/webhooks/me`** (CRUD): валидация URL до сохранения.
2. **`POST /api/webhooks/me/test`**: дополнительный resolve перед каждым тестом (URL мог поменяться между create и test через cache).
3. **`worker.webhook_dispatch.dispatch_one_payload`**: SSRF-check не повторяется на каждом POST (это было бы дорого; accepted risk DNS cache poisoning между created и dispatch). Защита от redirect через `httpx.AsyncClient(follow_redirects=False)` — Location header в 3xx игнорируется (3xx трактуется как failed).

Дополнительная **лексическая** проверка (до DNS):
- `host == 'localhost' | '127.0.0.1' | '0.0.0.0' | '[::1]'` → `400 webhook_url_private_ip`.
- `scheme != 'https'` → `400 validation_error` (DB CHECK constraint duplicate-guard).

При попадании при DNS-резолве в запрещённую сеть → `400 webhook_url_private_ip`.

В диспатчере при `InvalidTag` (decrypt secret failed по любой причине, включая ротацию ключа) → `dead_at = now()` + audit `webhook_dead_marked` `reason='secret_decrypt_failed'`. Это не SSRF, но обработано симметрично — diagnostic для лидера в UI.

Исключение для dev-режима (`APP_ENV=dev`): то же, что в §4 — private IPs разрешены для тестов с локальным mock-receiver'ом.

---

## 5. Сессии

См. ADR-0004 + ADR-0019 §10. Сводно:

| Параметр | Значение |
| --- | --- |
| Storage | Redis (`session:{token}` JSON) |
| Token | 32 random bytes -> base64url |
| Cookie name | `mas_session` |
| Cookie attrs | `HttpOnly`, `Secure` (prod), `SameSite=Lax`, `Path=/` |
| Sliding TTL | 12 hours |
| Absolute TTL | 7 days |
| Payload (ADR-0019 §10) | `{user_id, role, group_id, csrf_token, ip, ua_hash, created_at, last_seen_at}` — `role ∈ {super_admin, group_leader, group_member}`, `group_id` integer для leader/member, `null` для super_admin |
| Revoke | DEL key + SREM из `user_sessions:{user_id}` |
| Force revoke per user | Через set `user_sessions:{user_id}` — вызывается при `reset_password`, `delete_user`, при `PATCH /api/admin/users/{id}` с изменением `role`/`group_id` (ADR-0019 §10), и при **add/remove/move членства** (`POST`/`DELETE /api/admin/users/{id}/groups`, move через `PATCH`; ADR-0030 — чтобы `VisibilityScope.group_ids` перечитался из `user_groups`) |

**Breaking change при деплое 003_groups_and_roles**: payload-формат изменился (старое `is_admin: bool` → новое `role: str + group_id: int|null`). Все активные сессии становятся невалидными при первом deploy после миграции — пользователи будут разлогинены однократно (см. ADR-0019 «Отрицательные / компромиссы»).

**Telegram SSO pending cookie (ADR-0022):** в дополнение к `mas_session`/`mas_csrf` существует короткоживущий cookie `mas_tg_pending`:

| Параметр | Значение |
| --- | --- |
| Имя | `mas_tg_pending` |
| Storage | Redis `tg_pending:{token}` (JSON `{telegram_user_id: int}`) |
| Token | `secrets.token_urlsafe(32)` |
| Attrs | `HttpOnly`, `Secure` (prod), `SameSite=Lax`, `Path=/` |
| TTL | 15 минут (env `TG_PENDING_COOKIE_TTL_SEC=900`) — Redis TTL + cookie Max-Age синхронны |
| Создание | `POST /api/telegram/auth` если линковки нет → `linked: false` |
| Использование | `POST /login/password` step-2 и `POST /set-password` читают cookie, делают upsert в `telegram_links`, удаляют Redis ключ, clear cookie |
| Очистка | После успешной линковки ИЛИ при истечении TTL ИЛИ при ручном clear на `POST /api/telegram/auth` от другого `telegram_user_id` |

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

**Дополнительно (ADR-0022):**
- `POST /api/telegram/auth` — двухуровневый rate-limit: `30/min per IP` (slowapi) **+** `10/min per telegram_user_id` (применяется ПОСЛЕ успешной HMAC-валидации, чтобы не открывать enumeration через rate-limit timing).

**Дополнительно (ADR-0023):**
- `POST /api/webhooks/me` — `10/час per group_id` (защита от accidental замусоривания UNIQUE-conflict'ами).
- `PATCH /api/webhooks/me` — `30/час per webhook_id`.
- `DELETE /api/webhooks/me` — `10/час per webhook_id`.
- `POST /api/webhooks/me/rotate-secret` — `5/час per webhook_id` (защита от accidental DoS на receiver-side rotation logic).
- `POST /api/webhooks/me/test` — `10/час per webhook_id` (env `WEBHOOK_TEST_LIMIT=10`; защита от использования теста как ad-hoc HTTP probe).

**Дополнительно (ADR-0029):**
- `GET /api/external/messages` — `LIMIT_EXTERNAL_API` (env `EXTERNAL_API_RATE_LIMIT_PER_MINUTE`, `int`, default `120`, `ge=1`; лимит запросов в минуту на IP). Consume **до** проверки `EXTERNAL_API_KEY` (anti-flood / anti-brute-force). 429 → `Retry-After`.

---

## 8. Audit log

- Хранится в `admin_audit` (таблица в `03-data-model.md`).
- Все super-admin actions:
  - User-management: `create_user`, `reset_password`, `delete_user`.
  - Auth: `admin_login`, `admin_logout`.
  - Groups (ADR-0019 §9): `group_create`, `group_delete`, `group_rename`, `user_role_change`, `user_group_change`.
  - Multi-group membership (ADR-0030): `user_group_add`, `user_group_remove` (add/remove дополнительного членства в `user_groups`; `user_group_change` покрывает «Переместить»).
- Authentication-related: `lockout_triggered`.
- System: `account_auto_disabled` (worker отключил аккаунт за 3 fail).
- **Telegram (ADR-0022 + ADR-0024):** `telegram_link_created` (`details.replaced: bool, telegram_user_id`), `telegram_link_rebound` (ADR-0024 — TG перепривязан с другого user'а; `details.telegram_user_id, replaced=true`), `telegram_link_revoked` (**round-43:** `reason='user_unlink'` — явная отвязка одного TG (`details.telegram_user_id`); `reason='password_reset'`/`'link_user_missing'` — массовый отзыв (`details.telegram_user_ids: [...]`). **logout БОЛЬШЕ НЕ генерирует** — расцеплён с привязкой, ADR-0022 §1.5 / ADR-0024 §5 round-43), `telegram_link_dead_marked` (`details.telegram_user_id, reason='bot_api_403'\|...` — per-chat), `telegram_link_limit_reached` (ADR-0024 — достигнут `TG_MAX_LINKS_PER_USER`; `details.attempted_telegram_user_id, limit`). `telegram_link_collision` — **deprecated** (ADR-0024 §3: больше не пишется, инвариант «один user — один TG» снят; запись остаётся читаемой для истории). `actor_user_id` = сам пользователь (user-инициированное действие) — допустимо в этой таблице, хотя исторически она для super_admin actions; решение: расширяем семантику (см. ADR-0022 §1.4), `target_user_id` = тот же user.
- **OAuth Outlook (ADR-0025):** `oauth_account_linked` (`details={mail_account_id, email, scopes}` — подключён/переподключён OAuth-аккаунт), `oauth_refresh_invalidated` (`details={mail_account_id, reason}` — refresh отозван Microsoft `invalid_grant`, аккаунт требует re-consent). `actor_user_id` = инициатор подключения, `target_user_id` = тот же user.
- **Перенос ящика между командами (ADR-0031):** `mail_account_group_change` (`actor_user_id = инициатор переноса`, `target_user_id = владелец ящика` = `mail_accounts.user_id`, `details = {mail_account_id, from_group_id, to_group_id}`). Пишется при **реальной** смене `group_id` через `PATCH /api/mail-accounts/{id}` — для `super_admin` (всегда) и для `group_leader` (как admin-significant действие на ящик). **Исключение из ADR-0019 §9** (действия лидера обычно не аудируются): перенос — редкое структурное изменение принадлежности данных, поэтому фиксируется для super_admin'ского аудит-следа. Создание ящика с выбором команды audit **не** пишет. `group_member` транзакцию переноса выполнить не может (`403`), поэтому от него этого audit не бывает.
- **Outbound webhooks (ADR-0023):** `webhook_created` (`actor = инициатор, target_user_id = leader группы, details = {group_id, webhook_id, url}`), `webhook_updated` (`details = {webhook_id, changed_fields: [...], previous_dead_at}`), `webhook_deleted` (`details = {webhook_id, group_id, url}`), `webhook_secret_rotated` (`details = {webhook_id}`), `webhook_dead_marked` (`actor = leader_user_id (system action на его команде), target_user_id = leader_user_id, details = {webhook_id, reason: '410_gone'\|'consecutive_4xx'\|'secret_decrypt_failed'}`). При каскадном удалении группы (`DELETE /api/admin/groups/{id}`) отдельный `webhook_deleted` **не** пишется — каскад покрыт audit'ом `group_delete`.
- **Не пишутся в audit**: действия `group_leader` и `group_member` (создание mail-аккаунтов, отправка писем, теги). Для них достаточно structlog application-логов (см. ADR-0019 §9). **Единственное исключение** — `mail_account_group_change` (перенос ящика лидером), см. выше: структурное изменение принадлежности данных аудируется намеренно (ADR-0031 §6).
- Доступен через `/admin/audit` UI и `GET /api/admin/audit` (только super_admin).
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

## 9a. Authorization matrix (ADR-0019)

Сводка прав по ролям. Источник истины — ADR-0019. Каждая ячейка означает, что роль может выполнить операцию через API/UI; restricted-варианты помечены явно.

| Операция | super_admin | group_leader | group_member |
| --- | --- | --- | --- |
| Login / logout / set-password | ✅ | ✅ | ✅ |
| Видеть свой `/api/me` | ✅ | ✅ | ✅ |
| Видеть/фильтровать messages по `group_id` | ✅ (любую группу) | ❌ (только своя группа неявно) | ❌ |
| Видеть messages всех во **всех своих** командах (ADR-0030) | n/a (видит все) | ✅ | ✅ |
| Видеть mail-accounts всех во **всех своих** командах (ADR-0030) | n/a (видит все) | ✅ | ✅ |
| Создать mail-account на себя | ✅ | ✅ | ✅ |
| Создать mail-account на участника группы (`target_user_id`) | ✅ (на любого user'а) | ✅ (только в своей группе) | ❌ |
| Выбрать команду ящика при создании (`group_id`, ADR-0031) | ✅ (любая / «Без команды» = `NULL`) | ✅ (себе — свои `user_groups`; участнику — своя команда) | ✅ (только свои `user_groups`, только на себя) |
| Перенести существующий ящик в другую команду (`PATCH group_id`, ADR-0031) | ✅ (любой видимый → любая / `NULL`) | ✅ (в пределах видимости → свои команды/команда участника) | ❌ 403 forbidden |
| Получить список целевых команд (`GET /api/my/groups`, ADR-0031) | ✅ (все группы + фронт-опция «Без команды») | ✅ (свои `user_groups`) | ✅ (свои `user_groups`) |
| Edit/delete/sync-now mail-account в области видимости | ✅ (любой) | ✅ (любой в своей группе) | ✅ (любой в своей группе) |
| Send письма от любого account в области видимости | ✅ | ✅ | ✅ |
| Mark-read message в области видимости | ✅ | ✅ | ✅ |
| Создавать/редактировать **свои** теги | ✅ | ✅ | ✅ |
| Видеть теги других пользователей | ❌ (теги per-user, ADR-0017) | ❌ | ❌ |
| Доступ к `/admin`, `/admin/audit`, `/admin/groups` (HTML) | ✅ | ❌ 403 | ❌ 403 |
| `GET /api/admin/users` | ✅ | ❌ 403 | ❌ 403 |
| `POST /api/admin/users` (create user) | ✅ | ❌ 403 | ❌ 403 |
| `PATCH /api/admin/users/{id}` (role/group/display_name) | ✅ | ❌ 403 | ❌ 403 |
| `POST /api/admin/users/{id}/reset` | ✅ | ❌ 403 | ❌ 403 |
| `DELETE /api/admin/users/{id}` | ✅ (кроме self и leader'ов с непустой группой) | ❌ 403 | ❌ 403 |
| `GET /api/admin/groups` | ✅ | ❌ 403 | ❌ 403 |
| `POST/PATCH/DELETE /api/admin/groups/*` | ✅ | ❌ 403 | ❌ 403 |
| `POST /api/admin/users/{id}/groups` (add membership, ADR-0030) | ✅ (цель ≠ super_admin) | ❌ 403 | ❌ 403 |
| `DELETE /api/admin/users/{id}/groups/{gid}` (remove membership, ADR-0030) | ✅ (только доп. членство, не домашнее) | ❌ 403 | ❌ 403 |
| `PATCH /api/admin/users/{id}` move (смена домашней команды, ADR-0030) | ✅ (кроме `group_leader` — `cannot_move_group_leader`) | ❌ 403 | ❌ 403 |
| `GET /api/admin/audit` | ✅ | ❌ 403 | ❌ 403 |

**Примечания**:
- «область видимости» (`VisibilityScope`) реализована централизованно в FastAPI dependency (см. ADR-0019 §7 + ADR-0030 + `05-modules.md` модули `accounts`/`messages`). С ADR-0030 scope несёт множество `group_ids` (все команды пользователя из `user_groups`).
- Никаких per-record ACL — права = (role × множество команд `user_groups`). Per-group роли **не вводятся** (ADR-0030): роль глобальна. Если в будущем потребуется асимметрия «лидер vs участник» — отдельный ADR.
- Multi-group: член N команд видит ящики/письма всех своих команд и получает по ним TG-уведомления (основной бот ADR-0022). Push-боты ADR-0027 — broadcast по команде ящика, членств не учитывают (ADR-0030 §Decision 6).
- Sub-permissions внутри группы (read-only / write) **не реализуются** на текущей итерации (см. ADR-0019 §11 «Out of scope»).

---

## 10. Рекомендации по ротации ключей

| Ключ | Частота | Процедура |
| --- | --- | --- |
| `MAIL_ENCRYPTION_KEY` | Раз в 12 месяцев или при компрометации | См. ADR-0005 (`mas-cli reencrypt`) |
| `ADMIN_PASSWORD` | По требованию | Обновить `.env` → `docker compose restart api worker`. `seed_super_admin` идемпотентно перезапишет `users.password_hash` (см. `07-deployment.md` sec. 11.1). UI смены пароля для супер-админа сознательно не предусмотрен. |
| Session cookie name / domain | По требованию | Через env, разовая настройка |
| `EXTERNAL_API_KEY` (ADR-0029) | По требованию / при компрометации | Сгенерировать `openssl rand -hex 32` → заменить в `.env` → `docker compose up -d --force-recreate api`. Партнёр обновляет ключ синхронно. Старый ключ немедленно недействителен (нет grace-периода). Пустое значение = фича выключена. |

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
