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
    Inbox -- nav --> Tags[/tags/]
    Tags -- "+ Добавить тег" --> TagNew[/tags/new/]
    Tags -- "Edit" --> TagEdit[/tags/{id}/edit/]
    TagNew --> Tags
    TagEdit --> Tags
    Inbox -- "nav (super_admin only)" --> AdminUsers[/admin/]
    AdminUsers --> AdminAudit[/admin/audit/]
    Inbox -- "nav (super_admin only)" --> AdminGroups[/admin/groups/]
    AdminGroups -- "+ Создать группу" --> GroupNew[/admin/groups/new/]
    AdminGroups -- "Изменить" --> GroupEdit[/admin/groups/{id}/edit/]
    GroupNew --> AdminGroups
    GroupEdit --> AdminGroups
```

---

## 2. Шаблоны Jinja2 (полный список)

| Файл | Страница | Используется на |
| --- | --- | --- |
| `base.html` | — (layout) — включает topbar-nav (desktop) + bottom-nav (mobile/tg-app, секция 11) | все страницы |
| `_macros.html` | макросы (csrf_input, flash_messages, error_text, effective_user_name, effective_account_label, role_label, tag_chip) | импортируется |
| `login.html` | Login | `GET /login` |
| `set_password.html` | Set password | `GET /set-password` |
| `inbox.html` | Inbox (list) | `GET /` |
| `message_view.html` | View one message | `GET /messages/{id}` |
| `compose.html` | Compose new / reply | `GET /compose`, `GET /compose?reply_to=...` |
| `accounts/list.html` | Accounts list | `GET /accounts` |
| `accounts/form.html` | Add / edit account | `GET /accounts/new`, `GET /accounts/{id}/edit` |
| `admin/users.html` | Admin users | `GET /admin` |
| `admin/audit.html` | Admin audit log | `GET /admin/audit` |
| `tags/list.html` | Tags list page | `GET /tags` |
| `tags/form.html` | Create / edit tag form | `GET /tags/new`, `GET /tags/{id}/edit` |
| `admin/groups/list.html` | Groups list page (super_admin only, ADR-0019) | `GET /admin/groups` |
| `admin/groups/form.html` | Create / edit group form | `GET /admin/groups/new`, `GET /admin/groups/{id}/edit` |
| `errors/4xx.html` | Generic 4xx error | error handlers |
| `errors/5xx.html` | Generic 5xx error | error handlers |

### `base.html` структура

`lang="ru"` (см. ADR-0021). Все user-facing texts на русском.

```jinja2
<!doctype html>
<html lang="ru">
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
        <a href="/">Входящие</a>
        <a href="/accounts">Аккаунты</a>
        <a href="/tags">Теги</a>
        {% if request.state.session.role == 'super_admin' %}
          <a href="/admin">Пользователи</a>
          <a href="/admin/groups">Группы</a>
        {% endif %}
        {# Log out button (ADR-0019; восстановлен после редизайна) #}
        <form method="POST" action="/logout" class="inline logout-form">
          {{ csrf_input() }}
          <button class="link" type="submit">Выйти</button>
        </form>
      </nav>
    {% endif %}
  </header>
  <main>
    {{ flash_messages() }}
    {% block content %}{% endblock %}
  </main>
  <script src="/static/js/csrf.js"></script>
  <!-- Telegram WebApp adaptation (ADR-0018); no-op в обычном браузере -->
  <script src="https://telegram.org/js/telegram-web-app.js" defer></script>
  <script src="/static/js/tg.js" defer></script>
  {% block extra_js %}{% endblock %}
</body>
</html>
```

**Изменения relative к предыдущей версии**:
- `lang="en"` → `lang="ru"` (ADR-0021).
- Все nav-ссылки переведены на русский.
- Проверка роли админа: `role == 'admin'` → `role == 'super_admin'` (ADR-0019). Только super_admin видит `Пользователи` и `Группы`. Лидер группы и участник видят только базовый набор (Входящие / Аккаунты / Теги).
- **Восстановлена кнопка `Выйти`** — была потеряна после UI-редизайна (Sprint предыдущий), теперь снова в `<nav>` как `<form method="POST" action="/logout">` с `csrf_input()`. CSS-класс `.logout-form` для мелкой стилизации (по умолчанию выглядит как обычная nav-ссылка, без рамки кнопки). Backend-агент / frontend-агент при реализации обязаны убедиться, что эта форма присутствует в `base.html` (тест: `assert 'action="/logout"' in rendered_base_html`).

**Topbar nav** в шаблоне дополнительно скрывается, когда `<body>` имеет класс `tg-app` (Telegram WebApp предоставляет свой back-button — дублирование избыточно). Скрытие через CSS-правило `body.tg-app header.topbar nav { display: none; }` в `main.css` (см. секцию 10).

**КРИТИЧНО (ADR-0019 §11 / Sprint feature)**: при скрытии topbar-nav в `tg-app`-режиме теряется **logout-кнопка** для пользователя, открывшего сервис из Telegram-бота (а также теряется навигация в mobile-режиме при узком экране). Решение — **bottom-navigation** (см. секцию 11 ниже): для `tg-app`-режима и для mobile (`@media (max-width: 640px)`) рендерится фиксированная нижняя панель с 5 пунктами: «Входящие», «Аккаунты», «Теги», «Админ» (только для super_admin), «Выйти». Пункт «Выйти» — `<form method="POST" action="/logout">` (не `<a>`-link с GET, чтобы соблюсти CSRF и не делать logout через GET). Frontend-агент при реализации обязан проверить, что в обоих режимах (browser desktop / Telegram WebApp / mobile browser) у пользователя **всегда** есть видимая возможность выйти (тест: на каждой авторизованной странице после render присутствует ровно один `action="/logout"` form, либо в `.topbar` либо в `.bottom-nav`).

---

## 3. JS-файлы (полный список)

| Файл | Назначение |
| --- | --- |
| `csrf.js` | Универсальная функция `csrfFetch(url, options)` — обёртка над `fetch`, читает `mas_csrf` cookie и добавляет `X-CSRF-Token`. Все остальные JS используют только её. |
| `inbox.js` | Inbox: live-toggle "mark as read"; periodic polling списка (опционально); UX-обработка клика по строке; **searchable account-combobox** (фильтр «по почте»): читает список почт из data-island `<script type="application/json">`, клиентская фильтрация по email+display_name (case-insensitive), ARIA 1.2 combobox-навигация, проставляет hidden `account_id` и сабмитит GET-форму; `×`/«Все почты» сбрасывают. Прогрессивное улучшение — без JS остаётся `<select>`-fallback. |
| `compose.js` | Compose: подсветка некорректных email-адресов; счётчик символов subject; клиентская проверка длины body. |
| `account_form.js` | Add/edit account: при вводе email — auto-fill IMAP/SMTP defaults для известных доменов (хардкод-таблица в JS, см. ниже; backend-эндпоинта provider-suggest нет, чтобы не плодить лишних round-trip'ов); кнопка "Test connection" — POST `/api/mail-accounts/test`, показывает inline-результат. |
| `admin_users.js` | Admin: раскрытие/сворачивание списка mail-аккаунтов внутри строки пользователя; confirm-диалоги для reset/delete. |
| `tags.js` | Tags form (create / edit): динамическое добавление/удаление строк condition (rule_type[] + rule_pattern[]); валидация (тип выбран, pattern непустой) перед submit; color-picker swatches (см. секцию 5.1); confirm-диалог при DELETE тега и DELETE rule. **Важно:** без JS форма всё равно работает — template рендерит фиксированное число пустых rule-row (например, 5); пользователь заполняет столько, сколько нужно; backend пропускает empty pairs. |
| `tg.js` | Telegram WebApp adaptation (ADR-0018). На DOMContentLoaded: если `window.Telegram?.WebApp` существует — `Telegram.WebApp.ready()`, читает `themeParams` и применяет как CSS-vars (`--tg-bg`, `--tg-text`, `--tg-hint`, `--tg-link`, `--tg-button`, `--tg-button-text`, `--tg-secondary-bg`) на `document.documentElement`; добавляет класс `tg-app` на `<body>`; подписывается на `themeChanged`. Если SDK не загружен (открыто в браузере) — no-op. Подключается на каждой странице (в `base.html`) с `defer`. |

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
│  [Написать]  [Группа: Все ▼ (super_admin)]  [Аккаунт: Все ▼]         │
│              [Тег: Все ▼]   ☐ только непрочитанные   [Обновить]      │
├──────────────────────────────────────────────────────────────────────┤
│  ● [Apple Test 1] Иван Петров   John Doe    Тема   13:42 [● Диспут]  │
│  ○ [Yandex] Анна          Newsletter  Some news    12:11             │
│  ● [Apple Test 1] Иван Петров   Boss        Вопрос  вчера            │
│  ...                                                                 │
├──────────────────────────────────────────────────────────────────────┤
│                                              [< назад]  [далее >]    │
└──────────────────────────────────────────────────────────────────────┘
```

- Каждая строка — `<a href="/messages/{id}">`.
- "●" — непрочитанное (bold), "○" — прочитанное.
- **Колонка «Аккаунт»** (mail_account label): использует `effective_account_label(account)` (display_name → email; ADR-0020). Отображается chip'ом в начале строки.
- **Колонка «Владелец»** (owner) — показывается **только** при `request.state.session.role == 'super_admin'` ИЛИ когда у текущего пользователя в видимости больше одного user'а (т.е. он лидер/участник группы и в группе ≥ 2 человек). Использует `effective_user_name(owner)`. Помогает быстро понять «чей это ящик» при group-видимости (ADR-0019 §7.2).
- **Filter «Группа»** — dropdown появляется **только** для `role == 'super_admin'`. Опции: «Все» + список групп. Параметр `group_id` в query.
- **Filter «Аккаунт»** — **searchable typeahead-combobox** (UX-слой поверх неизменного серверного фильтра `account_id`). Пользователь вводит email **или** никнейм (`display_name`) ящика → клиентская фильтрация видимого списка почт (case-insensitive, по `email` + `display_name`) → выбор пункта проставляет hidden `account_id` → submit GET-формы → письма выбранной почты. «Все почты» / кнопка `×` сбрасывают фильтр (пустой `account_id`). Источник списка почт — те же mail-аккаунты в области видимости (`VisibilityScope` фильтрует на backend); combobox лишь облегчает выбор. Поведение (см. `inbox.js`, секция 3):
  - **ARIA 1.2 combobox pattern** (`role="combobox"` + `aria-expanded`/`aria-activedescendant`, listbox с `role="option"`).
  - **Progressive enhancement / noscript fallback**: без JS рендерится обычный `<select>` (combobox-обёртка скрыта до JS; hidden `account_id` disabled пока JS не активен — чтобы не дублировать параметр с `<select>`). Серверный контракт идентичен: единственный передаваемый параметр — `account_id`.
  - **CSP-safe**: список почт передаётся через data-island `<script type="application/json">`; обработчики навешиваются через `data-*` + `addEventListener` (без inline-`onclick`, без inline-`style`).
  - **Backend не меняется**: серверный фильтр `account_id` и его scope-авторизация прежние (см. `04-api-contracts.md` `GET /` и `GET /api/messages`). `display_name` уже присутствует в контексте (`mail_account_display_name` / `MailAccountDTO`).
- **Filter «Тег»** — dropdown «Все» + список тегов **текущего пользователя** (теги per-user; ADR-0017). Параметр `tag_id`.
- Pagination: keyset (next_cursor); кнопка `[далее >]` ведёт на `/?cursor=...`. Кнопка `[< назад]` хранит previous cursor через JS history (опционально; в простой версии — только next).
- "Обновить" — JS-обновление списка через `GET /api/messages?...` (без перезагрузки).
- Empty state: «Сообщений пока нет. Добавьте email-аккаунт, чтобы начать синхронизацию.» с кнопкой `[Добавить аккаунт]`.

### 4.4 Message view (`message_view.html`)

```
┌──────────────────────────────────────────────────────┐
│  [< Входящие]   [Ответить]   [Пометить непрочитанным]│
├──────────────────────────────────────────────────────┤
│  Тема: {{ subject }}  [● Важное] [● Диспут]          │
│  От: {{ from_name }} <{{ from_addr }}>               │
│  Кому: {{ to_addrs }}                                │
│  Копия: {{ cc_addrs }}                               │
│  Дата: {{ internal_date | local }}                   │
│  Аккаунт: {{ effective_account_label(account) }}     │
│  Владелец: {{ effective_user_name(owner) }}  (если   │
│             режим group-видимости и owner != self)   │
├──────────────────────────────────────────────────────┤
│  (plain-text body in <pre>, monospace, wrap)         │
│  ...                                                 │
│  ⓘ Тело письма обрезано до 1 МБ.  (если body_truncated)│
├──────────────────────────────────────────────────────┤
│  Вложения:                                           │
│   - report.pdf  (1.2 МБ)    [Скачать]                │
│   - image.png   (340 КБ)    [Скачать]                │
│   - huge.zip    (пропущено: слишком большой > 25 МБ) │
└──────────────────────────────────────────────────────┘
```

- Body — внутри `<pre class="body">` с CSS `white-space: pre-wrap; word-break: break-word;`.
- «Пометить непрочитанным» — JS `POST /api/messages/{id}/mark-read {is_read:false}`.
- «Ответить» — `<a href="/compose?reply_to={id}">`.
- Skipped attachments — серым, без кнопки.
- **Аккаунт** показывается через `effective_account_label(account)` (ADR-0020).
- **Владелец** (`owner.display_name | username`) показывается только если текущий пользователь — `super_admin` или лидер/участник группы и `owner.id != current_user.id` (ADR-0019). Если письмо своё (owner = self) — поле скрыто.
- **Теги**: рядом с темой показываются chip'ы тегов **владельца ящика** (per-user, см. ADR-0017 + ADR-0019 §7.4). Это важно: если лидер видит письмо участника, теги — участника, а не лидера.
- **`?embed=tg` режим (ADR-0022 §2.6)**: если query-параметр `embed=tg` присутствует, backend выставляет в Jinja-контекст `embed_tg=True`. (**Bug-fix #5:** push-кнопка «Посмотреть сообщение» **больше не** ведёт на этот URL — она `callback_data "msg:{id}"`, тело письма приходит в чат через webhook `callback_handler`. Route остаётся residual web-страницей.) Шаблон при `embed_tg=True` **скрывает** всю секцию `Вложения` (включая список и кнопки «Скачать»). Остальное (header, body, mark-read, bottom-nav, logout) — без изменений. Класс `body.tg-app` выставляется независимо от query — через `tg.js` при `window.Telegram?.WebApp` detected; в комбинации с `embed=tg` получается чистый view без topbar nav и без attachments, удобный для просмотра внутри Telegram WebApp.

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
│  Email-аккаунты                            [+ Добавить аккаунт]  │
├──────────────────────────────────────────────────────────────────┤
│  ● Apple Test 1     [Иван Петров]  apple-test-1@gmail.com        │
│       Синхр.: 3 мин назад                  [Изменить] [Удалить]  │
│  ● Yandex Личный    [Иван Петров]  ivan.personal@yandex.ru       │
│       Синхр.: 2 мин назад                  [Изменить] [Удалить]  │
│  ✗ Work             [Анна Петрова] work@corp.ru                  │
│       ⚠ Ошибка аутентификации              [Изменить] [Удалить]  │
│                                                                  │
│  [Синхронизировать сейчас] на каждой строке (в развёрнутом виде) │
└──────────────────────────────────────────────────────────────────┘
```

- Главный текст строки — `effective_account_label(account)` (display_name → email; ADR-0020).
- В квадратных скобках после ярлыка — owner: `effective_user_name(owner)` (ADR-0019). Показывается только в group-видимости (когда видимых владельцев больше одного); для личного режима (всё своё) скрывается.
- Ниже мелким шрифтом — full email (если `display_name` задан, чтобы пользователь всегда видел реальный адрес).
- "●" зелёный для `is_active=true`, "✗" красный для disabled.
- При наличии `last_sync_error` — inline tooltip / отдельная строка с pre-text.
- «Удалить» — confirm-диалог: «Удалить аккаунт {email}? Все кешированные письма будут удалены.»
- В группах: super_admin / лидер / участник могут редактировать любой аккаунт в области видимости (ADR-0019 §8). UI не различает «свой / чужой» — все одинаково editable.
- **Действие «Перенести в другую команду»** (ADR-0031): на строке ящика, рядом с `[Изменить]`/`[Удалить]`.
  - **Видимость кнопки:** показывается **только** если у инициатора `≥ 2` доступных целевых команд для этого ящика (для `group_member` кнопка **не рендерится никогда** — перенос ему запрещён, ADR-0031 §4; для `super_admin` и `group_leader` — если `GET /api/my/groups` вернул `≥ 2` команд, либо для super_admin доступна также опция «Без команды», что тоже даёт `≥ 2` вариантов). При `< 2` доступных команд кнопка скрыта (переносить некуда).
  - Открывает компактную форму «Сменить команду» (см. §4.7) — отдельную от edit-формы ящика; она шлёт `PATCH /api/mail-accounts/{id}` только с полем `group_id` (no-JS: `POST /api/mail-accounts/{id}` + `_method=PATCH` + `group_id` + `csrf_token`), не затрагивая остальные поля.

### 4.7 Account form (`accounts/form.html`)

```
┌────────────────────────────────────────────────┐
│   Добавить email-аккаунт                       │
│                                                │
│   Никнейм:  [ Apple Test 1                   ] │
│             (опционально; если задан — будет   │
│              показываться вместо email)        │
│   Email:    [ user@gmail.com                 ] │
│   Пароль:   [ ********                       ] │
│                                                │
│   Владелец: [ Иван Петров (я)              ▼]  │ (super_admin/leader)
│                                                │
│   ── IMAP ──                                   │
│   Хост:     [ imap.gmail.com                 ] │
│   Порт:     [ 993 ]   ☑ SSL                    │
│                                                │
│   ── SMTP ──                                   │
│   Хост:     [ smtp.gmail.com                 ] │
│   Порт:     [ 465 ]   ☑ SSL   ☐ STARTTLS       │
│                                                │
│   ▶ Использовать отдельные SMTP-учётные данные │
│     (раскрывает поля username / password)      │
│                                                │
│   [ Проверить соединение ]   [ Сохранить ]     │
└────────────────────────────────────────────────┘
```

- **Поле «Никнейм»** (display_name; ADR-0020): `<input name="display_name" maxlength="100" placeholder="Опционально">`. После trim'а пустое = NULL.
- При вводе email — JS auto-fills IMAP/SMTP defaults (см. сек. 3).
- **Поле «Владелец»** (target_user_id; ADR-0019 §8) — dropdown:
  - Для `group_member`: поле **отсутствует** (всегда self).
  - Для `group_leader`: dropdown показывает участников своей группы (включая self). Default = self. `<select name="target_user_id">` с `value=user.id`, label = `effective_user_name(user)`. Один опшен per group-member.
  - Для `super_admin`: dropdown — все пользователи системы (включая self), либо search-input. Можно выбрать любого. **Дополнительно**: dropdown «Группа» для фильтрации списка пользователей (опционально для упрощения выбора).
- **Поле «Команда»** (group_id; ADR-0031 §2/§5) — селектор целевой команды ящика. Источник опций — `GET /api/my/groups` (`{groups:[{id,name}], home_group_id}`):
  - **Рендерится ТОЛЬКО если доступных команд `> 1`.** При **одной** доступной команде (single-group пользователь) селектор **не рендерится вообще** — ящик молча попадает в единственную/домашнюю команду (форма не шлёт `group_id`, backend берёт домашнюю; полная обратная совместимость).
  - При `> 1` командах: `<select name="group_id">` с `<option value="{id}">{name}</option>` по `groups`. **Default-предвыбор** для multi-team — `home_group_id` (домашняя команда).
  - Для `super_admin`: к опциям из API добавляется фронтовая опция «Без команды» (`<option value="">` ⇒ `group_id=NULL`, персональный ящик); у super_admin селектор показывается, т.к. вариантов всегда `> 1` (все группы + «Без команды»).
  - no-JS: server-render формы тоже наполняет `<option>`-ы из того же `GET /api/my/groups`-сервиса (источник истины один, ADR-0031 §5). Отсутствие/пустая строка `group_id` в submit → backend трактует как «домашняя» (для super_admin пустое = явный выбор «Без команды» = `NULL`).
- «Проверить соединение» — JS вызов `/api/mail-accounts/test`; inline результат:
  - «✓ IMAP OK, SMTP OK» (зелёный) — кнопка «Сохранить» разблокирована.
  - «✗ Ошибка IMAP: <details>» (красный) — «Сохранить» заблокирован.
- «Сохранить» сразу делает POST `/api/mail-accounts` (повторяет тест; защита от устаревшей валидации).
- Edit-форма: password optional (не вводят — оставляем существующий зашифрованный); JS показывает «Оставьте пустым, чтобы не менять». Поле «Никнейм» предзаполнено существующим значением. **Поле «Владелец» в edit отсутствует** — переназначить владельца аккаунта нельзя (это отдельный flow, не реализуется в этом скоупе). **Команду ящика переназначить МОЖНО** (ADR-0031) — но не через edit-форму, а через отдельное действие «Перенести в другую команду» (см. ниже и §4.6); в edit-форме селектор команды **не показывается** (чтобы случайно не сменить команду при правке хостов/никнейма).

#### Форма «Сменить команду» (ADR-0031 §3)

Отдельная компактная форма, открываемая действием «Перенести в другую команду» из списка ящиков (§4.6). Доступна `super_admin` и `group_leader`; для `group_member` действие/форма **не существует** (перенос запрещён, ADR-0031 §4):

```
┌────────────────────────────────────────────────┐
│   Перенести ящик «Apple Test 1» в команду      │
│   Команда:  [ Маркетинг (текущая)           ▼] │
│             [ Поддержка                        ]│
│             [ Без команды           (super)    ]│
│   [ Перенести ]                                 │
└────────────────────────────────────────────────┘
```

- Источник опций — `GET /api/my/groups` (тот же, что в форме добавления). Текущая команда ящика помечается «(текущая)» и предвыбрана.
- Действие/форма показываются **только при `≥ 2` доступных целевых командах** (иначе переносить некуда — см. §4.6). Для `super_admin` опция «Без команды» (`group_id=NULL`) доступна всегда.
- Submit: `PATCH /api/mail-accounts/{id}` с **единственным** значащим полем `group_id` (no-JS: `POST /api/mail-accounts/{id}` + `_method=PATCH` + `group_id` + `csrf_token`); не затрагивает никнейм/хосты/пароль ⇒ повторный IMAP/SMTP-тест не выполняется (ADR-0031 §3).
- Success-flash (no-JS): «Ящик перенесён в другую команду», redirect `/accounts` (см. `04-api-contracts.md`).
- Ошибки: `403` (вне scope / `group_member`) и `404` (несуществующая команда) — re-render списка с flash-error; **никогда `500`**.

### 4.8 Admin users (`admin/users.html`)

Доступно только `super_admin`. Лидер/участник → 403.

```
┌────────────────────────────────────────────────────────────────────────┐
│  Пользователи                              [+ Создать пользователя]    │
│  Поиск: [                          ]                                   │
├────────────────────────────────────────────────────────────────────────┤
│  ▶ alice (Алиса Иванова)  Лидер группы / Группа Apple Test             │
│       создан 2026-04-01    последний вход: 2026-05-04                  │
│       [Изменить] [Сбросить пароль] [Удалить]                           │
│                                                                        │
│  ▼ bob (Боб)                Участник / Группа Apple Test               │
│       создан 2026-04-15    последний вход: никогда                     │
│       пароль: не задан                                                 │
│       Email-аккаунты:                                                  │
│         - Apple Test 1 / bob@gmail.com   (синхр. 2 мин назад)          │
│         - Yandex / bob@yandex.ru   (ОТКЛЮЧЁН — ошибка авторизации)     │
│       [Изменить] [Сбросить пароль] [Удалить]                           │
│                                                                        │
│  ▶ admin (Супер-админ)                                                 │
│       (системный аккаунт — действия недоступны)                        │
└────────────────────────────────────────────────────────────────────────┘
```

- Колонки строки: `username (display_name)` + chip с `role_label(role)` + chip «Группа `{group.name}`» (если есть).
- ▶/▼ — раскрытие списка mail-аккаунтов пользователя (JS, без перезагрузки).
- «Изменить» — открывает edit-форму (страница / модалка) c полями `display_name`, `role` (radio: «Лидер группы» / «Участник группы»; super-admin не доступен), `group_id` (select — отображается только для `role='group_member'`).
  - При смене role на `'group_leader'`: super-admin указывает либо «создать новую группу» (auto-create по `display_name`), либо текущую группу пользователя — но только если в ней нет другого лидера.
  - При смене role на `'group_member'` для текущего лидера: backend проверяет `cannot_demote_lone_leader` — если лидер один в группе, операция блокируется с понятной ошибкой и инструкцией.
- «Создать пользователя» — модалка / отдельная страница: поля `username` (обяз.), `email` (опц.), `display_name` (опц.; ADR-0019 §2), `role` (radio: «Лидер группы» / «Участник группы»; default = «Участник»), `group_id` (select):
  - Если `role = «Участник»`: select обязателен (existing groups; пустой список → super-admin сначала идёт на `/admin/groups/new`).
  - Если `role = «Лидер»`: select **скрыт** (auto-create); подсказка «Группа будет создана автоматически с именем «Группа `{display_name | username}`»». Имя группы можно отредактировать на `/admin/groups/{id}/edit` после создания.
  - После create — flash «Пользователь создан. Сообщите ему username; потребуется установить пароль.»
- «Сбросить пароль» — confirm-диалог.
- «Удалить» — confirm-диалог с явным «Введите username для подтверждения». Если user — лидер группы, кнопка выводит alert «Сначала удалите или переназначьте группу» (backend всё равно вернёт 409 с подробностями).
- Super-admin строка — без actions.

#### 4.8.1 Edit user form (`admin/users.html` — модалка / отдельная страница)

```
┌──────────────────────────────────────────────────────────────────┐
│   Изменить пользователя «alice»                                  │
│                                                                  │
│   Имя для отображения: [ Алиса Иванова                        ] │
│   (опционально; если пусто — будет показываться username)       │
│                                                                  │
│   Роль:                                                          │
│   ( ) Лидер группы                                               │
│   (●) Участник группы                                            │
│                                                                  │
│   Группа:  [ Apple Test                                       ▼] │ (только для role=Участник)
│   (или: «Группа будет создана автоматически» — для role=Лидер   │
│    без существующей группы)                                     │
│                                                                  │
│   [ Сохранить ]   [ Отмена ]                                     │
└──────────────────────────────────────────────────────────────────┘
```

- Submits as PATCH на `/api/admin/users/{id}` (или `POST` + `_method=PATCH`).
- Если super-admin меняет `role`/`group_id` — после save все сессии пользователя revoke'ются, frontend показывает flash «Пользователь обновлён. Все его активные сессии завершены.».

### 4.10 Tags list (`tags/list.html`)

Источник истины — [ADR-0017](./adr/ADR-0017-tags.md).

```
┌──────────────────────────────────────────────────────────────────┐
│  Ваши теги                                    [+ Добавить тег]   │
├──────────────────────────────────────────────────────────────────┤
│  ● DPLA.PLA              builtin   4 правила      [Изменить]     │
│  ● Диспут                builtin   2 правила      [Изменить]     │
│  ● Отменить подписку     builtin   2 правила      [Изменить]     │
│  ● Продление аккаунта    builtin   1 правило      [Изменить]     │
│  ● Important             custom    3 правила      [Изменить][×]  │
│  ● Newsletters           custom    1 правило      [Изменить][×]  │
└──────────────────────────────────────────────────────────────────┘
```

- "●" — circle SVG в цвете тега (16×16 px); рядом — `name`.
- "builtin" — бледный badge серого цвета (для встроенных тегов; `is_builtin=true`).
- Число правил — из `tags[i].rules.length`.
- "[×]" — кнопка delete; видна **только** для custom тегов; для builtin — отсутствует / disabled.
  - Реализация delete: `<form method="POST" action="/api/tags/{id}/delete" class="inline">` + `_method=DELETE` + csrf_input + confirm-диалог через JS.
- "[+ Добавить тег]" — `<a href="/tags/new">`.

### 4.11 Tag form (`tags/form.html`) — create/edit

```
┌────────────────────────────────────────────────────┐
│   Новый тег                                        │
│                                                    │
│   Имя:    [ Apple disputes                       ] │
│   Цвет:   [●][●][●][●][●][●][●][●]   ← swatches    │
│                                                    │
│   ── Условия (срабатывает любое) ──                │
│   ┌──────────────────────────────────────────┐     │
│   │ Тип: [ subject contains    ▼]            │     │
│   │ Шаблон: [ Apple Inc                    ] │ [×] │
│   └──────────────────────────────────────────┘     │
│   ┌──────────────────────────────────────────┐     │
│   │ Тип: [ sender exact        ▼]            │     │
│   │ Шаблон: [ AppStoreNotices@apple.com    ] │ [×] │
│   └──────────────────────────────────────────┘     │
│   [ + Добавить условие ]                           │
│                                                    │
│   ☐ Применить к существующим письмам               │
│                                                    │
│   [ Сохранить ]   [ Отмена ]                       │
└────────────────────────────────────────────────────┘
```

- "Имя" — `<input name="name" maxlength="64" required>`.
- "Цвет" — 8 swatch-вариантов из палитры (см. секцию 5.1). Реализация: `<input type="radio" name="color" value="#2563eb" id="color-c1" required>` для каждого слота палитры; `<label for="color-c1" class="color-swatch tag-color-c1">` (CSS-класс задаёт визуал из main.css — без inline-style, CSP-friendly). По умолчанию выбран первый цвет. Работает без JS как чистая radio-группа.
- Список условий — повторяющиеся блоки с парами `<select name="rule_type[]">` и `<input name="rule_pattern[]">`. Каждый блок имеет кнопку "[×]" для удаления (JS); без JS — пользователь оставляет поля пустыми, backend пропускает empty pairs.
- "[+ Добавить условие]" — JS добавляет новый rule-row. Без JS — template рендерит **5 пустых rule-row сразу** (явно: 5 — компромисс между «много для maxlength rules» и «не загромождает форму»; пользователь заполняет нужные).
- Типы условия (dropdown):
  - `subject_contains` — "Подстрока в subject"
  - `body_contains` — "Подстрока в теле"
  - `sender_contains` — "Подстрока в sender"
  - `sender_exact` — "Точное совпадение sender"
- Чекбокс "Применить к существующим письмам" — присутствует только в **create**-форме (в edit-форме — отдельная кнопка "Применить к существующим" вне формы, см. ниже).
- "[Сохранить]" → `<button type="submit">` → POST на `/api/tags` (create) или `POST /api/tags/{id}` + `_method=PATCH` (edit).
- "[Отмена]" → `<a href="/tags">`.

В edit-форме дополнительно:

```
┌────────────────────────────────────────────────────┐
│   Редактировать тег "Important"                    │
│   ...                                              │
│                                                    │
│   [ Сохранить имя/цвет ]                           │
│                                                    │
│   ── Управление правилами ──                       │
│   • subject_contains "TODO"     [Удалить правило]  │
│   • body_contains "ASAP"        [Удалить правило]  │
│                                                    │
│   ┌──────────────────────────────────────────┐     │
│   │ Тип: [ ▼]   Шаблон: [           ]        │     │
│   └──────────────────────────────────────────┘     │
│   [ Добавить правило ]                             │
│                                                    │
│   [ Применить тег к существующим письмам ]         │
└────────────────────────────────────────────────────┘
```

- В edit разделены 3 операции: `PATCH /api/tags/{id}` (имя+цвет), `POST /api/tags/{id}/rules` (add rule, отдельная форма), `DELETE /api/tags/{id}/rules/{rule_id}` (sibling-роут `.../delete` с `_method=DELETE`), `POST /api/tags/{id}/apply-to-existing` (отдельная кнопка-форма).
- Каждая `<form>` содержит свой `csrf_input()`.

### 4.12 Inbox с tag-фильтром и tag-badges

Дополняет 4.3 (Inbox). Изменения:

```
┌──────────────────────────────────────────────────────────────────────┐
│  [Compose]   Filter: [All accounts ▼]  [Tag: All ▼]  ☐ unread only   │
├──────────────────────────────────────────────────────────────────────┤
│  ● [acc:gmail]  John Doe        Subject of message  [● Important]    │
│  ○ [acc:yandex] Newsletter      Some news subject   [● News]         │
│  ● [acc:gmail]  Apple Inc       Receipt #1234       [● Диспут]       │
│  ...                                                                 │
└──────────────────────────────────────────────────────────────────────┘
```

- Filter "Tag" — `<select name="tag_id">` с `<option value="">All</option>` + список тегов пользователя (рендерится server-side).
- Tag-badges возле subject — небольшие chips: `<span class="tag-chip tag-color-{slot}">● Name</span>`.
- **Цветовое решение (CSP-friendly).** Колонка `tags.color` хранит hex `#RRGGBB`, но UI принуждённо выбирает значение строго из фиксированной палитры из 8 цветов (см. секцию 5.1). В `main.css` 8 предкомпилированных классов `.tag-color-c1` ... `.tag-color-c8` с фиксированными `background-color`. Helper в Jinja2-template маппит hex → имя класса (8 элементов в `dict`). Это исключает inline-`style` атрибут и не требует ослабления CSP `style-src 'self'`. Pure custom RGB — отдельный ADR, не в этом scope.

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

### 5.1 Палитра тегов

8 фиксированных цветов для тегов (выбор делается на форме `/tags/new`/`/tags/{id}/edit`; `tags.color` хранит выбранный hex):

| Slot | CSS-class | Hex | Семантика |
| --- | --- | --- | --- |
| c1 | `.tag-color-c1` | `#2563eb` | blue (default; общий) |
| c2 | `.tag-color-c2` | `#dc2626` | red (срочно / диспут) |
| c3 | `.tag-color-c3` | `#f59e0b` | amber (напоминание / подписка) |
| c4 | `.tag-color-c4` | `#16a34a` | green (готово / продление) |
| c5 | `.tag-color-c5` | `#7c3aed` | purple (важное) |
| c6 | `.tag-color-c6` | `#0891b2` | cyan (ссылка / интеграция) |
| c7 | `.tag-color-c7` | `#db2777` | pink (личное) |
| c8 | `.tag-color-c8` | `#475569` | slate (архив / шум) |

CSS-классы `.tag-color-cN` имеют `background-color: <hex>; color: white;` (с проверкой контраста для каждого hex). Класс `.tag-chip` задаёт padding, border-radius, font-size. Helper в Jinja2 (`_macros.html`) — `{{ tag_chip(tag) }}` — рендерит `<span class="tag-chip tag-color-{slot}">{{ tag.name }}</span>`.

Backend-валидация `color`: regex `^#[0-9A-Fa-f]{6}$` + проверка `IN (palette)` (8 hex-значений), на случай прямого API-вызова в обход формы. Если значение не из палитры — 400 `validation_error`.

---

## 6. Доступность (a11y) — минимум

- Все `<input>` имеют `<label>`.
- `<button>` для actions (не `<a>` для destructive).
- Контраст не ниже WCAG AA.
- Focus-стили видимы (не убирать `outline`).
- Семантический HTML (`<main>`, `<nav>`, `<header>`, `<form>`, `<table>`).

---

## 7. Локализация (ADR-0021)

Источник истины — [ADR-0021](./adr/ADR-0021-russian-localization.md). UI **полностью на русском**. Допустимы письма на любом языке (charset utf-8 в БД и шаблонах).

### 7.1 Принцип

- Все статичные тексты (заголовки, кнопки, labels, flash-success-сообщения) пишутся прямо в шаблонах на русском.
- Backend генерирует `error.code` в snake_case на английском (для логов и API). Преобразование code → RU-текст делается в Jinja2-macro `error_text(code)` в `_macros.html` (см. ADR-0021 §2 для полного mapping'а).
- `<html lang="ru">`.
- i18n-фреймворк (gettext/babel) не используется (overkill для одного языка).

### 7.2 `_macros.html` — обязательные macroses

Помимо существующих (`csrf_input`, `flash_messages`, `tag_chip`) добавляются:

```jinja2
{% macro error_text(code, default="Произошла ошибка") -%}
    {% set MAP = {
        # auth (полный список см. ADR-0021 §2)
        "invalid_credentials": "Неверный логин или пароль",
        "not_authenticated": "Требуется вход",
        "account_locked": "Слишком много попыток. Попробуйте позже",
        "rate_limited": "Слишком много запросов. Попробуйте позже",
        "csrf_failed": "Сессия устарела. Перезагрузите страницу и повторите",
        "validation_error": "Ошибка валидации формы",
        "method_override_not_allowed": "Запрос отклонён",
        "forbidden": "Доступ запрещён",
        "not_found": "Не найдено",
        "conflict": "Такая запись уже существует",
        "internal_error": "Внутренняя ошибка сервера. Попробуйте позже",
        "upstream_error": "Внешний сервис недоступен. Попробуйте позже",
        "dependency_unavailable": "Сервис временно недоступен",
        "imap_login_failed": "Не удалось подключиться по IMAP. Проверьте логин/пароль/настройки",
        "smtp_login_failed": "Не удалось подключиться по SMTP. Проверьте логин/пароль/настройки",
        "smtp_failed": "Не удалось отправить письмо",
        "cannot_select_inbox": "INBOX недоступен на сервере",
        "cannot_delete_admin": "Нельзя удалить супер-админа",
        "cannot_reset_admin": "Нельзя сбросить пароль супер-админа",
        "cannot_delete_builtin_tag": "Нельзя удалить встроенный тег",
        "tag_apply_too_many": "Слишком много писем для применения тега. Создайте тег без 'применить к существующим'",
        "group_leader_consistency_violation": "Несогласованность роли и группы",
        "group_id_must_be_null_for_new_leader": "Для нового лидера группа создаётся автоматически",
        "group_not_found": "Группа не найдена",
        "group_has_members": "В группе ещё есть участники. Сначала переведите их в другую группу или удалите.",
        "user_not_in_group_scope": "Пользователь вне области видимости вашей группы",
        "cannot_demote_lone_leader": "Нельзя понизить единственного лидера группы. Сначала назначьте другого лидера или удалите группу",
    } -%}
    {{ MAP.get(code, default) }}
{%- endmacro %}

{% macro effective_user_name(user) -%}
    {{ user.display_name if user.display_name else user.username }}
{%- endmacro %}

{% macro effective_account_label(account) -%}
    {{ account.display_name if account.display_name else account.email }}
{%- endmacro %}

{% macro role_label(role) -%}
    {%- if role == 'super_admin' -%}Супер-админ
    {%- elif role == 'group_leader' -%}Лидер группы
    {%- elif role == 'group_member' -%}Участник группы
    {%- else -%}{{ role }}
    {%- endif -%}
{%- endmacro %}
```

Helper'ы используются:
- `effective_user_name(user)` — везде в admin/users, admin/groups, owner-колонки.
- `effective_account_label(account)` — везде в inbox, message_view, accounts, compose.
- `role_label(role)` — везде где показывается role (admin/users, admin/groups).

Pydantic-validation сообщения переводятся в backend через helper `pydantic_msg_to_ru` (см. ADR-0021 §3); в шаблон передаются уже переведённые `form_errors`.

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
- Tags: list / create / edit / delete тегов (form-POST; для PATCH/DELETE — `_method=PATCH`/`_method=DELETE` на whitelist'е; rules add/remove работают через отдельные `<form>` элементы; "Применить к существующим" — отдельная form-POST кнопка; форма rule-row рендерит 5 пустых строк по умолчанию, JS дополнительно позволяет добавить/удалить, но без JS все 5 строк рабочие).

JS только улучшает UX, но не блокирует базовую функциональность.

Telegram WebApp adaptation — см. секцию 10 ниже.

---

## 9. Сводный чек-лист для frontend-исполнителя

- [ ] Все шаблоны из секции 2 созданы.
- [ ] Все JS-файлы из секции 3 созданы.
- [ ] CSS из секции 5 написан.
- [ ] CSP-совместимо: нет inline `<script>`, нет inline `<style>`. CSP — строгий, `style-src 'self' https://telegram.org` (без `'unsafe-inline'`); все стили только из `static/css/main.css`. Любые "стили в шаблонах" недопустимы. Единственный external script — `https://telegram.org/js/telegram-web-app.js` (см. секцию 10).
- [ ] CSRF-input вставлен во все form-POST.
- [ ] Email-адреса HTML-escape'ятся (Jinja2 `|e` default).
- [ ] Тела писем в `<pre>` с белым пробелом сохранением.
- [ ] Файлы вложений — Content-Disposition с правильным RFC 5987 encoding.
- [ ] Confirm-диалоги для destructive actions.
- [ ] Формы для DELETE-операций POST'ят на sibling-роут `.../delete` с `<input type="hidden" name="_method" value="DELETE">`; формы для PATCH POST'ят на канонический путь с `<input type="hidden" name="_method" value="PATCH">` (см. ADR-0015 + `04-api-contracts.md`).
- [ ] Flash-сообщения после redirect рендерятся через `flash_messages()` macro в `base.html`.
- [ ] Адаптивная вёрстка (mobile-first).
- [ ] Базовая a11y соблюдена.
- [ ] Шаблоны `tags/list.html`, `tags/form.html` созданы; `tags.js` подключается только на этих страницах через `{% block extra_js %}`. Без JS — форма с 5 пустыми rule-rows работает; цвет выбирается radio-buttons.
- [ ] Tag-chips на inbox и message_view используют `_macros.html → tag_chip(tag)` с CSS-классом `.tag-color-cN` из палитры (секция 5.1) — без inline-style.
- [ ] Tag-фильтр на inbox: `<select name="tag_id">` с list пользовательских тегов; query-param пробрасывается.
- [ ] Фильтр «по почте» на inbox: searchable typeahead-combobox (ARIA 1.2), клиентская фильтрация scoped-списка по email+display_name; hidden `account_id` проставляется выбором, `×`/«Все почты» сбрасывают. Без JS — `<select>`-fallback (combobox скрыт, hidden `account_id` disabled). CSP-safe: data-island `<script type="application/json">` + `data-*`/`addEventListener`. Серверный параметр `account_id` НЕ изменён.
- [ ] `tg.js` подключён в `base.html` (defer); открытие в обычном браузере — no-op; открытие через Telegram-бот — применяется тема и `body.tg-app` (см. секцию 10).
- [ ] CSS-правила `body.tg-app` добавлены в `main.css` (секция 10.3).
- [ ] Bottom-nav с 5 пунктами (Входящие / Почты / Теги / Админ* / Выйти) присутствует в `base.html` для авторизованных пользователей (секция 11). Видна в `tg-app` И на mobile (≤640px); скрыта на desktop browser. Пункт «Админ» — только для `request.state.session.role == 'super_admin'`.
- [ ] Logout-кнопка достижима на каждой авторизованной странице **в обоих режимах**: desktop (через topbar nav) и mobile/tg-app (через bottom-nav 5-й пункт «Выйти», который — `<form method="POST" action="/logout">` с `csrf_input()`).
- [ ] CSS bottom-nav (`.bottom-nav`, `.bottom-nav__item`, `.bottom-nav__form`, `.bottom-nav__button`) добавлены в `main.css` (секция 11.4); body padding-bottom учтён, чтобы контент не закрывался панелью.
- [ ] В каждом HTML-роуте устанавливается переменная `active` (значения: `'inbox'`/`'accounts'`/`'tags'`/`'admin'`/`None`) для подсветки активного пункта bottom-nav (секция 11.3).

---

## 10. Telegram WebApp adaptation

Источники истины — [ADR-0018](./adr/ADR-0018-telegram-launcher.md) (тема + body.tg-app) + [ADR-0022](./adr/ADR-0022-telegram-sso-and-notifications.md) (Persistent SSO + push-notif inline). Когда страница открыта внутри Telegram WebApp, мы:
1. Применяем тему Telegram и скрываем дублирующую navigation (ADR-0018).
2. Если страница рендерится анонимно (`<body data-anonymous>`), JS делает попытку Persistent SSO через `POST /api/telegram/auth` (ADR-0022 §1.3). При успехе и наличии линковки — auto-login без формы.
3. Если страница открыта с `?embed=tg`, backend скрывает секцию вложений в `message_view.html` (см. секцию 4.4 выше). **Примечание (Bug-fix #5):** push-кнопка «Посмотреть сообщение» **больше не открывает** эту страницу — она `callback_data "msg:{id}"`, и полное тело письма приходит сообщением в чат через webhook `callback_handler` (ADR-0022 §2.5/§2.6). Route `?embed=tg` остаётся residual web-страницей.

### 10.1 Подключение SDK

В `base.html` (см. секцию 2):

```html
<script src="https://telegram.org/js/telegram-web-app.js" defer></script>
<script src="/static/js/tg.js" defer></script>
```

CSP `script-src` расширен на `https://telegram.org` (см. `06-security.md` §6 — таблица обновлена). CDN отдаёт ровно один файл (`telegram-web-app.js`), без styles/img.

### 10.2 `tg.js` — поведение

```text
on DOMContentLoaded:
  if (!window.Telegram || !window.Telegram.WebApp) return;     // обычный браузер
  const tg = window.Telegram.WebApp;
  tg.ready();
  function applyTheme() {
    const p = tg.themeParams || {};
    const map = {
      bg_color: '--tg-bg', text_color: '--tg-text',
      hint_color: '--tg-hint', link_color: '--tg-link',
      button_color: '--tg-button', button_text_color: '--tg-button-text',
      secondary_bg_color: '--tg-secondary-bg',
    };
    for (const [k, v] of Object.entries(map)) {
      if (p[k]) document.documentElement.style.setProperty(v, p[k]);
    }
  }
  applyTheme();
  document.body.classList.add('tg-app');
  tg.onEvent('themeChanged', applyTheme);

  // ADR-0022 §1.3 — Persistent SSO попытка для анонимного посещения
  if (tg.initData && document.body.dataset.anonymous === '1') {
    fetch('/api/telegram/auth', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({init_data: tg.initData}),
      credentials: 'same-origin',
    }).then(r => r.json().then(b => ({status: r.status, body: b})))
      .then(({status, body}) => {
        if (status === 200 && body && body.linked) {
          window.location.replace(body.redirect || '/');
        }
        // linked=false / 401 / 429 — оставляем пользователя на странице (server-rendered /login)
      })
      .catch(() => { /* network — no-op, fallback на manual login */ });
  }
```

Никаких side-effects в обычном браузере (early return).

`<body data-anonymous="1">` устанавливается в `base.html` при отсутствии сессии (`{% if not session %}data-anonymous="1"{% endif %}`).

### 10.3 CSS-правила

В `main.css` добавляются:

```css
/* Telegram WebApp: применяются только когда запущено через бот */
body.tg-app {
  background-color: var(--tg-bg, #ffffff);
  color: var(--tg-text, #1a1a1a);
}
body.tg-app a { color: var(--tg-link, #2563eb); }
body.tg-app .btn-primary {
  background-color: var(--tg-button, #2563eb);
  color: var(--tg-button-text, #ffffff);
}
body.tg-app header.topbar nav { display: none; }   /* back-button даёт сам Telegram */
```

Mobile-CSS уже описан секцией 5 (`@media (max-width: 640px)`) — отдельной адаптации под Telegram WebApp не требуется (WebView отрисовывает страницу с шириной устройства).

### 10.4 Что НЕ меняется (после ADR-0022)

- Auth-flow для **без линковки**: пользователь видит обычную login-форму на `/login` — fallback всегда работает.
- Шаблоны страниц: только `message_view.html` получает условную ветку `{% if not embed_tg %}` для секции вложений (ADR-0022 §2.6). Остальные шаблоны — без изменений.
- CSRF, cookies, SameSite, mark-read, compose-send — поведение идентично браузерному.
- `mas_session` cookie остаётся HttpOnly — JS не читает его напрямую. Маркер для SSO попытки — `<body data-anonymous="1">` (server-rendered).

### 10.5 Тестирование

- Открыть страницу в обычном браузере: `body` не имеет класса `tg-app`, CSS-vars `--tg-*` не выставлены, всё рендерится по светлой теме.
- Открыть через бот в Telegram: `body.tg-app` присутствует, цвета соответствуют теме Telegram (light/dark в зависимости от системы), top-bar nav скрыт, bottom-nav (см. секцию 11) — виден.
- Переключение темы Telegram (light ↔ dark) на лету — UI обновляется без перезагрузки (через подписку `themeChanged`).
- Logout-кнопка достижима в обоих режимах: в desktop browser — через topbar nav; в `tg-app` или mobile (≤640px) — через bottom-nav 5-й пункт «Выйти».
- **Persistent SSO (ADR-0022 §1.3):**
  - Открыть бот без активной линковки → /start → нажать WebApp кнопку → попадает на /login (server-rendered HTML с `data-anonymous="1"`); `tg.js` делает POST `/api/telegram/auth` → 200 `{linked: false}` → cookie `mas_tg_pending` выставлен → пользователь видит /login форму → логинится → линковка создана.
  - Закрыть/открыть бот заново → /start → нажать WebApp кнопку → попадает на /login → `tg.js` делает POST `/api/telegram/auth` → 200 `{linked: true}` → `window.location.replace('/')` → пользователь сразу в Inbox без ввода логина/пароля.
  - **(round-43, ADR-0022 §1.5)** Logout в bottom-nav → POST /logout → **только** сессия revoked + cookies очищены → редирект на /login. Привязка `telegram_links` **НЕ** удаляется. Следующий запуск бота: `tg.js` POST `/api/telegram/auth` → 200 `{linked:true}` → автологин по сохранённой привязке (повторный ввод пароля **не** требуется). Чтобы отвязать Telegram — отдельная кнопка «Отвязать» в `/my/integrations` (`DELETE /api/telegram/links/{id}`).
  - **(round-43) Кнопка «Отвязать Telegram»** в `/my/integrations` — **уже реализована** (`telegram_links.js` рендерит per-row кнопку `.btn--danger` «Отвязать» с `window.confirm` → `DELETE /api/telegram/links/{telegram_user_id}` → обновление списка). Это единственный пользовательский путь отвязки. Frontend-правок round-43 **не требует**.

---

## 11. Bottom navigation (mobile + Telegram WebApp)

В Telegram WebApp topbar-nav скрывается (CSS-правило `body.tg-app header.topbar nav { display: none; }` — см. §10.3). На mobile-устройствах с узким экраном topbar-nav также частично «уползает» / тяжело тапается. Решение — **фиксированная bottom-navigation panel** с основными разделами и кнопкой выхода.

### 11.1 Когда показывается

Видна, когда выполнено хотя бы одно условие:

- `<body class="tg-app">` (открыто через Telegram WebApp).
- Экран `≤ 640 px` ширины (mobile breakpoint, который уже используется для адаптации в §5).

В desktop browser (`> 640 px` и не `tg-app`) — **скрыта** (используется обычный topbar-nav).

CSS-правила (в `main.css`, секция 11.4):
```css
.bottom-nav { display: none; }
body.tg-app .bottom-nav { display: flex; }
@media (max-width: 640px) {
  .bottom-nav { display: flex; }
  /* верхний topbar nav скрываем на мобиле, оставляем только бренд */
  header.topbar nav { display: none; }
}
```

### 11.2 Структура (Jinja2 — в `base.html`, после `<main>{% block content %}`)

5 пунктов фиксированно, в одном порядке, slot для `Админ` показывается только super_admin'у. На каждой странице один из пунктов отмечен `aria-current="page"` (логика подсветки активного — через сравнение `request.url.path` с эталонами).

```jinja2
{% if request.state.session %}
<nav class="bottom-nav" aria-label="Основная навигация">
  <a href="/"          class="bottom-nav__item {% if active == 'inbox'    %}is-active{% endif %}"
     {% if active == 'inbox' %}aria-current="page"{% endif %}>
    <span class="bottom-nav__icon" aria-hidden="true">📥</span>
    <span class="bottom-nav__label">Входящие</span>
  </a>
  <a href="/accounts"  class="bottom-nav__item {% if active == 'accounts' %}is-active{% endif %}"
     {% if active == 'accounts' %}aria-current="page"{% endif %}>
    <span class="bottom-nav__icon" aria-hidden="true">📮</span>
    <span class="bottom-nav__label">Почты</span>
  </a>
  <a href="/tags"      class="bottom-nav__item {% if active == 'tags'     %}is-active{% endif %}"
     {% if active == 'tags' %}aria-current="page"{% endif %}>
    <span class="bottom-nav__icon" aria-hidden="true">🏷️</span>
    <span class="bottom-nav__label">Теги</span>
  </a>
  {% if request.state.session.role == 'super_admin' %}
    <a href="/admin"   class="bottom-nav__item {% if active == 'admin'    %}is-active{% endif %}"
       {% if active == 'admin' %}aria-current="page"{% endif %}>
      <span class="bottom-nav__icon" aria-hidden="true">⚙️</span>
      <span class="bottom-nav__label">Админ</span>
    </a>
  {% endif %}
  <form method="POST" action="/logout" class="bottom-nav__item bottom-nav__form">
    {{ csrf_input() }}
    <button type="submit" class="bottom-nav__button">
      <span class="bottom-nav__icon" aria-hidden="true">🚪</span>
      <span class="bottom-nav__label">Выйти</span>
    </button>
  </form>
</nav>
{% endif %}
```

**Важные моменты**:
- Пункты «Входящие / Почты / Теги / Админ» — обычные `<a href>` (GET).
- Пункт «Выйти» — `<form method="POST">` с CSRF-input. Использовать GET для logout запрещено (CSRF + side-effects). Внутри формы — `<button>` стилизован под nav-item (CSS-класс `.bottom-nav__button` обнуляет button-defaults).
- Эмодзи как иконки — без зависимости от icon-фонтов; CSP строгий (`'self'` + `https://telegram.org`), не разрешает inline images. Если в будущем понадобится — SVG-spritesheet в `/static/img/icons.svg`.
- Слово «Аккаунты» сократилось до «Почты» — на узком экране 5 пунктов помещаются лучше, плюс в группе пользователь чаще говорит «почты» (общие ящики), чем «аккаунты». Внутренние пути (`/accounts`) и заголовок страницы (`Email-аккаунты`) не меняются — это только nav-label.

### 11.3 Подсветка активного пункта

В каждом HTML-роуте передаётся переменная `active` со значением одного из: `'inbox'` (для `/`), `'accounts'` (для `/accounts*`), `'tags'` (для `/tags*`), `'admin'` (для `/admin*`), либо `None` (для login/set-password — там session всё равно отсутствует и nav не рендерится).

Реализация — context-builder для `Jinja2Templates` в `backend/app/deps.py` или прямо в роутерах. Backend-агент сам выбирает паттерн (рекомендация: декоратор/dependency `set_active_nav("inbox")`).

### 11.4 CSS

В `main.css` (фрагмент):

```css
.bottom-nav {
  display: none;        /* default; включается через media-query или body.tg-app */
  position: fixed;
  left: 0; right: 0; bottom: 0;
  height: 56px;
  flex-direction: row;
  align-items: stretch;
  justify-content: space-around;
  background: var(--tg-secondary-bg, #ffffff);
  border-top: 1px solid var(--border-color, #e5e7eb);
  z-index: 100;
}
.bottom-nav__item {
  flex: 1 1 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  text-decoration: none;
  color: var(--tg-hint, #6b7280);
  font-size: 11px;
  padding: 6px 4px;
}
.bottom-nav__item.is-active {
  color: var(--tg-link, #2563eb);
}
.bottom-nav__icon { font-size: 20px; line-height: 1; }
.bottom-nav__label { font-size: 11px; line-height: 1; }
.bottom-nav__form { margin: 0; padding: 0; }
.bottom-nav__button {
  background: none;
  border: 0;
  padding: 0;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  color: inherit;
  font: inherit;
  width: 100%;
  height: 100%;
}
/* Убираем дабл-тап выделение в Safari */
.bottom-nav__button { -webkit-tap-highlight-color: transparent; }
/* На страницах со списком добавляем нижний padding, чтобы контент не закрывался bottom-nav */
@media (max-width: 640px) {
  body.has-bottom-nav main { padding-bottom: 72px; }
}
body.tg-app main { padding-bottom: 72px; }
```

Класс `body.has-bottom-nav` ставится в `<body>` тегом-условием `{% if request.state.session %}has-bottom-nav{% endif %}`. Без этого — на странице login (нет сессии) bottom-nav не виден и padding не нужен.

### 11.5 Локализация

Все labels по умолчанию на русском (см. ADR-0021): «Входящие», «Почты», «Теги», «Админ», «Выйти». EN-варианты не предусмотрены (RU-only продукт; если поменяется — TD-016).

### 11.6 Тестируемые инварианты

- На любой авторизованной странице (inbox, message_view, accounts, tags, compose, admin/*) при `body.tg-app` ИЛИ ширине ≤640px рендерится ровно один `<nav class="bottom-nav">` с минимум 4 пунктами (и 5-м для super_admin).
- 5-й пункт `nav` всегда — `<form method="POST" action="/logout">` с `csrf_input()`. GET `/logout` не используется.
- На странице `/login`, `/set-password` (нет сессии) bottom-nav **не** рендерится.
- На desktop browser (>640px, не tg-app) bottom-nav скрыт через CSS, topbar-nav виден и содержит logout-form (восстановлено в §2).
- Соответствие пунктов и URL: «Входящие» → `/`, «Почты» → `/accounts`, «Теги» → `/tags`, «Админ» → `/admin` (только super_admin).
