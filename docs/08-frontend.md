# 08. Frontend

Frontend сервиса — server-rendered HTML (Jinja2 в `api`-контейнере) + минимальный vanilla JS для UX-улучшений. Без SPA-фреймворков, без bundler'ов. Стиль — минималистичный, чистый, без лишних украшений.

API-контракты — в [`04-api-contracts.md`](./04-api-contracts.md). Этот документ описывает **UX-флоу, страницы, компоненты, шаблоны**.

---

## 1. UX-карта

```mermaid
flowchart LR
    Login[/login/] --> SetPwd[/set-password/]
    Login --> Inbox[/]
    SetPwd --> Inbox
    Inbox -- click message --> View[/messages/{id}/]
    Inbox -- "Compose new" --> Compose[/compose/]
    View -- "Reply" --> Compose
    Compose -- send --> Inbox
    Inbox -- nav --> Accounts[/accounts/]
    Accounts -- "Add" --> AccNew[/accounts/new/]
    Accounts -- "Edit" --> AccEdit[/accounts/{id}/edit/]
    AccNew --> Accounts
    AccEdit --> Accounts
    Inbox -- nav (admin only) --> AdminUsers[/admin/]
    AdminUsers --> AdminAudit[/admin/audit/]
```

---

## 2. Шаблоны Jinja2 (полный список)

| Файл | Страница | Используется на |
| --- | --- | --- |
| `base.html` | — (layout) | все страницы |
| `_macros.html` | макросы (csrf_input, flash, pagination) | импортируется |
| `login.html` | Login | `GET /login` |
| `set_password.html` | Set password | `GET /set-password` |
| `inbox.html` | Inbox (list) | `GET /` |
| `message_view.html` | View one message | `GET /messages/{id}` |
| `compose.html` | Compose new / reply | `GET /compose`, `GET /compose?reply_to=...` |
| `accounts/list.html` | Accounts list | `GET /accounts` |
| `accounts/form.html` | Add / edit account | `GET /accounts/new`, `GET /accounts/{id}/edit` |
| `admin/users.html` | Admin users | `GET /admin` |
| `admin/audit.html` | Admin audit log | `GET /admin/audit` |
| `errors/4xx.html` | Generic 4xx error | error handlers |
| `errors/5xx.html` | Generic 5xx error | error handlers |

### `base.html` структура

```
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Mail Aggregator{% endblock %}</title>
  <link rel="stylesheet" href="/static/css/main.css">
</head>
<body>
  <header class="topbar">
    <a href="/" class="brand">Mail Aggregator</a>
    {% if request.state.session %}
      <nav>
        <a href="/">Inbox</a>
        <a href="/accounts">Accounts</a>
        {% if request.state.session.role == 'admin' %}<a href="/admin">Admin</a>{% endif %}
        <form method="POST" action="/logout" class="inline">
          {{ csrf_input() }}
          <button class="link">Log out</button>
        </form>
      </nav>
    {% endif %}
  </header>
  <main>
    {{ flash_messages() }}
    {% block content %}{% endblock %}
  </main>
  <script src="/static/js/csrf.js"></script>
  {% block extra_js %}{% endblock %}
</body>
</html>
```

---

## 3. JS-файлы (полный список)

| Файл | Назначение |
| --- | --- |
| `csrf.js` | Универсальная функция `csrfFetch(url, options)` — обёртка над `fetch`, читает `mas_csrf` cookie и добавляет `X-CSRF-Token`. Все остальные JS используют только её. |
| `inbox.js` | Inbox: live-toggle "mark as read"; periodic polling списка (опционально); UX-обработка клика по строке. |
| `compose.js` | Compose: подсветка некорректных email-адресов; счётчик символов subject; клиентская проверка длины body. |
| `account_form.js` | Add/edit account: при вводе email — auto-fill IMAP/SMTP defaults для известных доменов (хардкод-таблица в JS, см. ниже; backend-эндпоинта provider-suggest нет, чтобы не плодить лишних round-trip'ов); кнопка "Test connection" — POST `/api/mail-accounts/test`, показывает inline-результат. |
| `admin_users.js` | Admin: раскрытие/сворачивание списка mail-аккаунтов внутри строки пользователя; confirm-диалоги для reset/delete. |

**Provider auto-suggest**: хардкод-таблица в `account_form.js` (короткий объект, дублирующий `accounts/providers.py`). Минимальный набор — `gmail.com`, `yandex.ru`, `mail.ru`, `outlook.com`; backend-агент при необходимости расширяет JS-таблицу до полного списка из `providers.py` (см. `05-modules.md` секция 9). Backend остаётся источником истины — он ре-валидирует всё при POST/test.

---

## 4. Wireframe-описания

### 4.1 Login (`login.html`)

```
┌────────────────────────────┐
│   Mail Aggregator          │
│                            │
│   ┌──────────────────────┐ │
│   │ Username             │ │
│   └──────────────────────┘ │
│   ┌──────────────────────┐ │
│   │ Password             │ │
│   └──────────────────────┘ │
│   [ Sign in ]              │
│                            │
│   (no "register" link —    │
│    accounts created by     │
│    admin only)             │
└────────────────────────────┘
```

- Form POST `/login`. На fail: `flash` "Invalid credentials" (без раскрытия деталей).
- Lockout: красный flash "Too many attempts. Try again in N minutes." (`Retry-After`).

### 4.2 Set password (`set_password.html`)

```
┌────────────────────────────────┐
│   Set your password            │
│                                │
│   Hello, {{ username }}.       │
│   Please choose a password.    │
│                                │
│   ┌──────────────────────────┐ │
│   │ New password (min 12)    │ │
│   └──────────────────────────┘ │
│   ┌──────────────────────────┐ │
│   │ Confirm password         │ │
│   └──────────────────────────┘ │
│   [ Save ]                     │
│                                │
│   * must contain a letter and  │
│     a digit                    │
└────────────────────────────────┘
```

- Form POST `/set-password`.

### 4.3 Inbox (`inbox.html`)

```
┌──────────────────────────────────────────────────────────────────────┐
│  [Compose new]   Filter: [All accounts ▼]   ☐ unread only   [Refresh]│
├──────────────────────────────────────────────────────────────────────┤
│  ● [acc:gmail]  John Doe        Subject of message    13:42          │
│  ○ [acc:yandex] Newsletter      Some news subject     12:11          │
│  ● [acc:gmail]  Boss            Quick question        Yesterday      │
│  ○ [acc:work]   GitHub          PR ready for review   2 days ago     │
│  ...                                                                 │
├──────────────────────────────────────────────────────────────────────┤
│                                              [< prev]  [next >]      │
└──────────────────────────────────────────────────────────────────────┘
```

- Каждая строка — `<a href="/messages/{id}">`.
- "●" — непрочитанное (bold), "○" — прочитанное.
- Filter dropdown: "All accounts" + список mail-аккаунтов пользователя.
- Pagination: keyset (next_cursor); кнопка `[next >]` ведёт на `/?cursor=...`. Кнопка `[< prev]` хранит previous cursor через JS history (опционально; в простой версии — только next).
- "Refresh" — JS-обновление списка через `GET /api/messages?...` (без перезагрузки).
- Empty state: "No messages yet. Add a mail account to start syncing." с кнопкой `[Add account]`.

### 4.4 Message view (`message_view.html`)

```
┌──────────────────────────────────────────────────────┐
│  [< Inbox]   [Reply]   [Mark as unread]              │
├──────────────────────────────────────────────────────┤
│  Subject: {{ subject }}                              │
│  From: {{ from_name }} <{{ from_addr }}>             │
│  To: {{ to_addrs }}                                  │
│  Cc: {{ cc_addrs }}                                  │
│  Date: {{ internal_date | local }}                   │
│  Account: {{ mail_account_email }}                   │
├──────────────────────────────────────────────────────┤
│  (plain-text body in <pre>, monospace, wrap)         │
│  ...                                                 │
│  ⓘ Body truncated at 1 MiB.   (если body_truncated)  │
├──────────────────────────────────────────────────────┤
│  Attachments:                                        │
│   - report.pdf  (1.2 MiB)   [Download]               │
│   - image.png   (340 KiB)   [Download]               │
│   - huge.zip    (skipped: too large > 25 MiB)        │
└──────────────────────────────────────────────────────┘
```

- Body — внутри `<pre class="body">` с CSS `white-space: pre-wrap; word-break: break-word;`.
- "Mark as unread" — JS `POST /api/messages/{id}/mark-read {is_read:false}`.
- "Reply" — `<a href="/compose?reply_to={id}">`.
- Skipped attachments — серым, без кнопки.

### 4.5 Compose (`compose.html`)

```
┌──────────────────────────────────────────────────────┐
│  [< Cancel]                                          │
├──────────────────────────────────────────────────────┤
│  From:    [ {{ user_email }} (Gmail)            ▼]   │
│  To:      [ comma,separated@addresses             ]  │
│  Cc:      [                                       ]  │
│  Bcc:     [                                       ]  │
│  Subject: [                                       ]  │
│                                                      │
│  ┌────────────────────────────────────────────────┐  │
│  │                                                │  │
│  │  (plain-text body, large textarea)             │  │
│  │                                                │  │
│  └────────────────────────────────────────────────┘  │
│                                                      │
│  [Send]                                              │
└──────────────────────────────────────────────────────┘
```

- "From" dropdown — список mail-аккаунтов пользователя; default — первый активный.
- При reply: subject prefilled `Re: {{ original.subject }}` (без удвоения "Re:"); body prefilled цитата:
  ```
  
  
  On {{ original.date }} {{ original.from_name }} <{{ original.from_addr }}> wrote:
  > line 1
  > line 2
  ```
- Form POST `/api/messages/send` через `csrfFetch`. На успех — redirect `/` + flash "Message sent".
- На fail (502) — inline error с текстом ошибки SMTP.
- На текущей итерации **аттачи не поддерживаются** (см. `03-data-model.md`); UI не показывает поле upload.

### 4.6 Accounts list (`accounts/list.html`)

```
┌──────────────────────────────────────────────────────────────────┐
│  Your mail accounts                            [+ Add account]   │
├──────────────────────────────────────────────────────────────────┤
│  ● my@gmail.com    Gmail   Last sync: 3 min ago    [Edit][Delete]│
│  ● my@yandex.ru    Yandex  Last sync: 2 min ago    [Edit][Delete]│
│  ✗ work@corp.ru    custom  ⚠ Auth failed           [Edit][Delete]│
│                                                                  │
│  [Sync now] на каждой строке (в развёрнутом виде)               │
└──────────────────────────────────────────────────────────────────┘
```

- "●" зелёный для is_active=true, "✗" красный для disabled.
- При наличии `last_sync_error` — inline tooltip / отдельная строка с pre-text.
- "Delete" — confirm-диалог "Delete account my@gmail.com? All cached messages will be removed."

### 4.7 Account form (`accounts/form.html`)

```
┌────────────────────────────────────────────────┐
│   Add mail account                             │
│                                                │
│   Email:   [ user@gmail.com                  ] │
│   Password:[ ********                        ] │
│                                                │
│   ── IMAP ──                                   │
│   Host:    [ imap.gmail.com                  ] │
│   Port:    [ 993 ]   ☑ SSL                     │
│                                                │
│   ── SMTP ──                                   │
│   Host:    [ smtp.gmail.com                  ] │
│   Port:    [ 465 ]   ☑ SSL   ☐ STARTTLS        │
│                                                │
│   ▶ Use separate SMTP credentials              │
│     (раскрывает поля username / password)      │
│                                                │
│   [ Test connection ]   [ Save ]               │
└────────────────────────────────────────────────┘
```

- При вводе email — JS auto-fills IMAP/SMTP defaults (см. сек. 3).
- "Test connection" — JS вызов `/api/mail-accounts/test`; inline результат:
  - "✓ IMAP OK, SMTP OK" (зелёный) — кнопка Save разблокирована.
  - "✗ IMAP login failed: <details>" (красный) — Save заблокирован.
- "Save" сразу делает POST `/api/mail-accounts` (повторяет тест; защита от устаревшей валидации).
- Edit-форма: password optional (не вводят — оставляем существующий зашифрованный); JS показывает "Leave empty to keep current".

### 4.8 Admin users (`admin/users.html`)

```
┌──────────────────────────────────────────────────────────────────┐
│  Users                                          [+ Create user]  │
│  Search: [                          ]                            │
├──────────────────────────────────────────────────────────────────┤
│  ▶ alice     created 2026-04-01     last login: 2026-05-04        │
│      [Reset password]   [Delete]                                  │
│                                                                   │
│  ▼ bob       created 2026-04-15     last login: never             │
│      pwd: not set                                                 │
│      Mail accounts:                                               │
│        - bob@gmail.com   (last sync 2 min ago)                    │
│        - bob@yandex.ru   (DISABLED — auth failed)                 │
│      [Reset password]   [Delete]                                  │
│                                                                   │
│  ▶ admin (super-admin)                                            │
│      (no actions — system account)                                │
└──────────────────────────────────────────────────────────────────┘
```

- ▶/▼ — раскрытие списка mail-аккаунтов пользователя (JS, без перезагрузки).
- "Create user" — модалка / отдельная страница: поле username (обяз.), email (опц.). После create — UI показывает "User created. Tell them to log in with their username; password setup will be required."
- "Reset password" — confirm-диалог.
- "Delete" — confirm-диалог с явным "Type username to confirm" (защита от misclick).
- Super-admin строка — без кнопок.

### 4.9 Admin audit (`admin/audit.html`)

```
┌──────────────────────────────────────────────────────────────────────┐
│  Audit log                                                           │
│  Filter: action [▼ all]  user [▼ all]  from [date]  to [date]        │
├──────────────────────────────────────────────────────────────────────┤
│  2026-05-05 13:42  admin   admin_login            ip 1.2.3.4         │
│  2026-05-05 12:01  admin   create_user  bob       ip 1.2.3.4         │
│  2026-05-05 09:00  system  account_auto_disabled  bob@yandex.ru:auth │
│  ...                                                                 │
│                                                                      │
│  [Pagination]                                                        │
└──────────────────────────────────────────────────────────────────────┘
```

- Pagination 50/page.
- Click on action — раскрывается JSON `details`.
- Read-only (нет edit/delete).

---

## 5. CSS / стиль

- Файл `static/css/main.css`.
- Палитра: light theme.
  - Background `#ffffff`, text `#1a1a1a`, subtle borders `#e5e7eb`, primary action `#2563eb`, danger `#dc2626`, success `#16a34a`.
- Шрифты: system stack `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, sans-serif`. Мономейн для body писем: `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`.
- Layout: max-width 960px на основном content, full-width topbar.
- Mobile: одна колонка; табличные строки inbox адаптируются под flex.
- Без иконочных шрифтов; SVG inline в шаблонах (опционально).

---

## 6. Доступность (a11y) — минимум

- Все `<input>` имеют `<label>`.
- `<button>` для actions (не `<a>` для destructive).
- Контраст не ниже WCAG AA.
- Focus-стили видимы (не убирать `outline`).
- Семантический HTML (`<main>`, `<nav>`, `<header>`, `<form>`, `<table>`).

---

## 7. Локализация

На первой итерации — **только английский язык в UI**, но допустимы письма на любом языке (charset utf-8 в БД и шаблонах).

i18n-инфраструктура не поднимается. Если потребуется — отдельный ADR.

---

## 8. Поведение JS-disabled

Технический механизм поддержки no-JS (HTTP method override через `_method` + form-encoded acceptance + content negotiation на ответе) описан в [ADR-0015](./adr/ADR-0015-no-js-fallback.md); whitelist endpoints и формат запросов — в [`04-api-contracts.md`](./04-api-contracts.md) секция "Form-encoded fallback".

Сценарии, которые обязаны работать без JS:
- Login, set-password, logout (все form-POST).
- Inbox listing (без auto-refresh).
- Открытие сообщения, скачивание вложений.
- Compose + send (form-POST на `/api/messages/send` form-encoded; backend поддерживает form-encoded POST как альтернативу JSON; multi-value `to`/`cc`/`bcc` — одна строка с разделителем `,`/`;`).
- Add/edit/delete account (form-POST; для PATCH/DELETE используется `_method=PATCH`/`_method=DELETE` поверх POST; "Test connection" недоступен — Save сам делает тест на сервере).
- Admin: create user, reset, delete (form-POST; для DELETE — `_method=DELETE` на sibling-роуте `.../delete`; confirm через `<button onclick>` отвалится при no-JS — допустимо, страховка на стороне сервера через сами actions достаточна).

JS только улучшает UX, но не блокирует базовую функциональность.

---

## 9. Сводный чек-лист для frontend-исполнителя

- [ ] Все шаблоны из секции 2 созданы.
- [ ] Все JS-файлы из секции 3 созданы.
- [ ] CSS из секции 5 написан.
- [ ] CSP-совместимо: нет inline `<script>`, нет inline `<style>`. CSP — строгий, `style-src 'self'` (без `'unsafe-inline'`); все стили только из `static/css/main.css`. Любые "стили в шаблонах" недопустимы.
- [ ] CSRF-input вставлен во все form-POST.
- [ ] Email-адреса HTML-escape'ятся (Jinja2 `|e` default).
- [ ] Тела писем в `<pre>` с белым пробелом сохранением.
- [ ] Файлы вложений — Content-Disposition с правильным RFC 5987 encoding.
- [ ] Confirm-диалоги для destructive actions.
- [ ] Формы для DELETE-операций POST'ят на sibling-роут `.../delete` с `<input type="hidden" name="_method" value="DELETE">`; формы для PATCH POST'ят на канонический путь с `<input type="hidden" name="_method" value="PATCH">` (см. ADR-0015 + `04-api-contracts.md`).
- [ ] Flash-сообщения после redirect рендерятся через `flash_messages()` macro в `base.html`.
- [ ] Адаптивная вёрстка (mobile-first).
- [ ] Базовая a11y соблюдена.
