# ADR-0018 — Telegram launcher bot + WebApp без линковки аккаунтов

| | |
| --- | --- |
| Статус | **superseded by [ADR-0043](./ADR-0043-strip-to-connector-push-to-crm.md)** (2026-07-10) — Telegram/Mini App переносятся в CRM (`/tg/mail`); ранее partially superseded by ADR-0022 |
| Дата | 2026-05-07 |
| Заменяет / отменён | — |

## Context

Пользователь хочет открывать сервис не вводя URL `https://postapp.store`
в браузере мобильного устройства, а одной кнопкой из Telegram. Главные
драйверы:

- Mobile UX — браузер на телефоне неудобен для повторного ввода URL и
  логина; Telegram уже открыт у пользователя.
- Сервис не имеет публичной регистрации (см. ADR-0016, `08-frontend.md`
  §4.1) — пользователей создаёт админ. Поэтому бот не должен решать
  задачу "регистрации через Telegram".
- Существующий two-step login (ADR-0016) и весь auth-стек (Redis-сессии
  ADR-0004, CSRF ADR-0010, rate-limit ADR-0009) уже работает; ломать его
  ради бота нет смысла.

Явное требование пользователя: **никакой линковки** Telegram-аккаунта с
учётной записью сервиса; **никакой initData-аутентификации**;
**никакого нового способа login** в БД. Bot — только launcher.

## Decision

### 1. Bot как чистый launcher

Бот реализует ровно две команды:

- `/start` — отвечает текстом `Open the app:` и inline-keyboard с одной
  WebApp-кнопкой `Open Mail Aggregator`, у которой
  `web_app.url = https://postapp.store` (значение из env
  `TELEGRAM_WEBAPP_URL`).
- `/help` — короткое сообщение "Send /start to open the app". Любые
  другие апдейты (произвольные текстовые сообщения, callback_query'и,
  edited_message и т.п.) — игнорируются (HTTP 200 OK без действий).

Нажатие кнопки открывает встроенный WebView Telegram на основном URL
сервиса. Пользователь видит **обычную login-форму** (step-1 — username,
step-2 — password), логинится cookie-сессией ровно как в браузере. Нет
отдельного "login through Telegram", нет отдельного URL под бот, нет
отдельного шаблона.

### 2. Webhook вместо long-poll

Bot API подключаем через webhook:

- Endpoint: `POST /api/telegram/webhook/{secret}` — `{secret}` равен env
  `TELEGRAM_WEBHOOK_SECRET` (32 hex-символа,
  `openssl rand -hex 16`). Secret в URL-path служит proof-of-bot.
- Дополнительно проверяется header `X-Telegram-Bot-Api-Secret-Token`
  (см. Bot API `setWebhook?secret_token=`). Если значения не совпадают
  c env — 403 без обработки.
- Long-poll отвергнут: webhook надёжнее в prod (no extra processes),
  меньше race-условий, не нужен отдельный worker.

Обработчик асинхронный, отвечает 200 моментально и НЕ блокирует ответ
ожиданием send-результатов (если ответ боту нужен — отправляется
последующим `POST sendMessage`, не как inline-reply).

### 3. Без Telegram SDK на backend

Используем сырой Bot API через уже установленный `httpx`:
`https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage`. Никаких
`aiogram`/`python-telegram-bot` — для двух команд это лишняя
зависимость и слой абстракций.

### 4. WebApp adaptation на frontend

Web интерфейс уже mobile-friendly через существующие CSS-правила
`@media (max-width: 640px)` в `static/css/main.css`. Дополнительно:

- В `base.html` подключается официальный SDK
  `https://telegram.org/js/telegram-web-app.js` (CDN Telegram); CSP
  расширяется до `script-src 'self' https://telegram.org`.
- Тонкий клиентский скрипт `static/js/tg.js` (defer):
  - При DOMContentLoaded проверяет `window.Telegram?.WebApp`. Если SDK
    не подгрузился (открыто из обычного браузера) — выходит без
    действий.
  - Вызывает `Telegram.WebApp.ready()` (закрывает loader Telegram).
  - Читает `themeParams` (`bg_color`, `text_color`, `hint_color`,
    `link_color`, `button_color`, `button_text_color`,
    `secondary_bg_color`) и применяет на `document.documentElement` как
    CSS-vars `--tg-bg`, `--tg-text`, `--tg-hint`, `--tg-link`,
    `--tg-button`, `--tg-button-text`, `--tg-secondary-bg`.
  - Добавляет `tg-app` класс на `<body>`.
  - Подписывается на событие `themeChanged` для re-применения.
- Когда `<body>` имеет класс `tg-app`:
  - top-bar `<nav>` скрывается (Telegram WebApp предоставляет свой
    back-button и меню; дублирование избыточно).
  - Базовые цвета (`background-color`, `color`, primary-action) read
    из CSS-vars `--tg-*` с fallback на light-палитру (`08-frontend.md`
    §5).
- Никаких inline `<script>` / inline `<style>` (CSP остаётся строгой).

### 5. Никаких изменений в auth/session/CSRF/БД

- Не создаются таблицы `telegram_users`, `tg_user_links` и т.п.
- Не добавляются поля `telegram_user_id` в `users`.
- Cookie `mas_session`/`mas_csrf` ставятся ровно как в браузере;
  Telegram WebView корректно работает с `SameSite=Lax` + `Secure`
  поверх HTTPS (опытно проверено в Telegram WebApp 7.x; cookies
  shared с system WebView, не sandbox'ятся отдельно от обычного
  браузера на iOS/Android).

### 6. Конфигурация (env)

| Переменная | Required | Описание |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes (если бот включён) | Bot-token от BotFather. Маскируется в логах (см. ниже). |
| `TELEGRAM_WEBHOOK_SECRET` | yes (если бот включён) | 32 hex символа. Используется и в URL-path, и в `X-Telegram-Bot-Api-Secret-Token`. |
| `TELEGRAM_WEBAPP_URL` | yes (если бот включён) | URL, который бот вкладывает в WebAppInfo. Prod: `https://postapp.store`. Dev: ngrok URL. |
| `TELEGRAM_BOT_ENABLED` | no, default `false` | Если `false` — webhook-роут регистрируется, но при срабатывании всегда отвечает 200 без действий, и бот не отправляет ответы. Позволяет проверить, что роут не падает в окружении без bot-настройки. Включается одноразово после deploy + setWebhook. |

Все три обязательные значения хранятся в `.env` с `chmod 600`.
`TELEGRAM_BOT_TOKEN` добавляется в structlog redact-list рядом с
`MAIL_ENCRYPTION_KEY`/`password`/`session_token` (см. ADR-0014).

### 7. Setup webhook (one-shot после deploy)

```bash
curl -F "url=https://postapp.store/api/telegram/webhook/${TELEGRAM_WEBHOOK_SECRET}" \
     -F "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
     "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook"
```

Полный runbook — `07-deployment.md` секция "Telegram bot setup".

## Consequences

### Positive

- Минимальная поверхность изменений: один webhook-роут, один
  bot-сервис (~50–80 строк Python), один JS-файл (~30 строк), нулевые
  изменения в БД/auth.
- Auth/session/CSRF/rate-limit логика остаётся единственной — нечего
  дублировать, нечего синхронизировать.
- Никаких новых secrets кроме trivial bot-token + webhook-secret.
- Телеграм-WebApp UX: тёмная тема системы Telegram автоматически
  применяется к UI без дублирования темы в БД пользователя.
- Отказ бота не валит web — это полностью независимый код-путь.

### Negative

- Cookies в Telegram WebView формально работают, но в edge-cases
  (старые версии Telegram, iOS WebKit ограничения) может потребоваться
  повторный login каждый раз — risk известен, mitigation: сессия
  sliding 12h, пользователь введёт пароль не чаще раза в день.
- CSP расширяется на одно стороннее происхождение
  `https://telegram.org` (только для script-src — стилей и img их CDN
  не загружает).
- Без линковки невозможны push-уведомления "новое письмо" в Telegram —
  это явный out-of-scope для текущей итерации (см. tech-debt ниже).

### Tech debt registry

- **TD-013** (см. `100-known-tech-debt.md`): отсутствуют
  push-уведомления в Telegram о новых письмах — требует линковки
  `telegram_user_id ↔ user_id` и opt-in flow. Намеренно отложено.

## Alternatives considered

1. **WebApp initData auth (без password).** Telegram отдаёт в WebApp
   подписанный `initData` со всеми полями user'а; backend верифицирует
   HMAC-SHA256(secret=BOT_TOKEN) и автоматически создаёт сессию для
   привязанного `users.telegram_user_id`. **Отвергнуто**: пользователь
   явно требует обычный flow с username+password — "точно так же как
   на сайте"; добавление этого пути усложняет auth (две модели
   credential — пароль и Telegram-signed-payload), требует таблицы
   линковки, требует UI "связать Telegram", добавляет surface area
   для атаки (подмена initData, кража BOT_TOKEN ⇒ компрометация всех
   сессий через бот).

2. **Линковка `telegram_user_id ↔ user_id`** (без auto-login —
   просто "знаем кому слать пуши"). **Отвергнуто**: пользователь явно
   сказал "линковки нет"; OOS для текущей итерации; без линковки нет
   и пушей — accept (см. TD-013).

3. **Long-poll бот** (отдельный фоновый процесс, периодически
   опрашивает `getUpdates`). **Отвергнуто**: нужен ещё один контейнер
   или scheduler-job в worker'е; webhook надёжнее, проще, без
   дополнительного state (offset).

4. **Bot SDK (`aiogram`/`python-telegram-bot`).** **Отвергнуто**:
   overkill для двух команд; SDK тянет десятки модулей и dispatcher,
   tier'ы фильтров, FSM-state, что нам не нужно. Сырой `httpx` +
   pydantic-валидация update'ов — на порядок проще.

5. **Использовать единый webhook-secret в env без `secret_token`
   header.** **Отвергнуто**: secret в URL логируется reverse-proxy
   (nginx access-log с full path), header — нет; double-проверка
   защищает на случай leak логов.

## Cross-references

- `04-api-contracts.md` — секция "Telegram webhook" (новая).
- `05-modules.md` — модуль 19 `telegram` (новый); раздел 0 (layout)
  расширен на `backend/app/telegram/*` и `backend/app/static/js/tg.js`.
- `06-security.md` — секция 1.8 (STRIDE для webhook), redact-list
  обновлён на `TELEGRAM_BOT_TOKEN`.
- `07-deployment.md` — секция 4 (env) расширена; новая секция 14
  "Telegram bot setup".
- `08-frontend.md` — новая секция 10 "Telegram WebApp adaptation".
- `100-known-tech-debt.md` — TD-011 (push-уведомления требуют
  линковки).
