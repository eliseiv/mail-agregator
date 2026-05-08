# ADR-0021: Полная RU-локализация UI без i18n-фреймворка

- **Статус:** accepted
- **Дата:** 2026-05-08

## Context

В `08-frontend.md` секция 7 ранее зафиксировано «UI на английском, локализация — отдельный ADR». Запрос продукта в текущей итерации: **весь UI должен быть на русском языке**. Пользовательский язык — единственный (никакого переключения en/ru). При этом backend генерирует error-codes (`invalid_credentials`, `validation_error`, `imap_login_failed`, `tag_apply_too_many` и т. д.) в snake_case на английском — для логов, аудита и API-консистентности (см. `04-api-contracts.md` секция «Унифицированный формат ошибок»).

## Decision

### 1. Все user-facing тексты в Jinja2-шаблонах — на русском

Все статичные тексты (заголовки страниц, кнопки, labels форм, заголовки таблиц, flash-success-сообщения, текст пустых состояний, текст confirm-диалогов) пишутся **прямо в шаблонах** на русском языке. Никаких переключателей языка, никаких placeholder'ов вроде `{{ _('Sign in') }}`.

### 2. Error-codes остаются английскими; mapping → русский текст в Jinja-macro

Backend продолжает возвращать `error.code` в snake_case на английском (см. `04-api-contracts.md`). Для отображения пользователю используется helper-macro в `_macros.html`:

```jinja2
{# templates/_macros.html #}
{% macro error_text(code, default="Произошла ошибка") -%}
    {% set MAP = {
        # auth
        "invalid_credentials": "Неверный логин или пароль",
        "not_authenticated": "Требуется вход",
        "account_locked": "Слишком много попыток. Попробуйте позже",
        "rate_limited": "Слишком много запросов. Попробуйте позже",
        "csrf_failed": "Сессия устарела. Перезагрузите страницу и повторите",
        # validation
        "validation_error": "Ошибка валидации формы",
        "method_override_not_allowed": "Запрос отклонён",
        # generic
        "forbidden": "Доступ запрещён",
        "not_found": "Не найдено",
        "conflict": "Такая запись уже существует",
        "internal_error": "Внутренняя ошибка сервера. Попробуйте позже",
        "upstream_error": "Внешний сервис недоступен. Попробуйте позже",
        "dependency_unavailable": "Сервис временно недоступен",
        # mail-accounts
        "imap_login_failed": "Не удалось подключиться по IMAP. Проверьте логин/пароль/настройки",
        "smtp_login_failed": "Не удалось подключиться по SMTP. Проверьте логин/пароль/настройки",
        "smtp_failed": "Не удалось отправить письмо",
        "cannot_select_inbox": "INBOX недоступен на сервере",
        # admin
        "cannot_delete_admin": "Нельзя удалить супер-админа",
        "cannot_reset_admin": "Нельзя сбросить пароль супер-админа",
        # tags (ADR-0017)
        "cannot_delete_builtin_tag": "Нельзя удалить встроенный тег",
        "tag_apply_too_many": "Слишком много писем для применения тега. Создайте тег без 'применить к существующим'",
        # groups (ADR-0019)
        "group_leader_consistency_violation": "Несогласованность роли и группы",
        "group_id_must_be_null_for_new_leader": "Для нового лидера группа создаётся автоматически",
        "cannot_delete_group_with_super_admin_target": "Нельзя выполнить эту операцию над супер-админом",
        "group_not_found": "Группа не найдена",
        "user_not_in_group_scope": "Пользователь вне области видимости вашей группы"
    } -%}
    {{ MAP.get(code, default) }}
{%- endmacro %}
```

Использование в шаблонах:

```jinja2
{# inbox.html / форма с error-context #}
{% if form_error %}
    <div class="error">{{ error_text(form_error.code) }}</div>
{% endif %}

{# flash messages в base.html #}
{% for f in flashes %}
    <div class="flash {{ f.category }}">{{ f.text }}</div>
{% endfor %}
```

`flash`-сообщения backend пишет **сразу на русском** (см. `04-api-contracts.md` секция «Redirect targets для form-success» — все строки уже на русском). Это делается там, где backend знает контекст («Тег создан», «Email-аккаунт добавлен»). Для error-кодов мы используем mapping — чтобы не дублировать перевод одной и той же ошибки в 10 endpoint'ах.

### 3. Динамические тексты валидации (per-field, Pydantic)

Pydantic возвращает сообщения вида `"field required"` / `"value is not a valid email address"` на английском. Для form-rerender backend конвертирует через дополнительный mapping (можно прямо в `error_text`-стиле, но per-field). Минимальный набор:

| Pydantic msg | RU |
| --- | --- |
| `field required` | «Поле обязательно для заполнения» |
| `value is not a valid email address` | «Некорректный email-адрес» |
| `string does not match regex ...` | «Недопустимый формат» |
| `ensure this value has at least N characters` | «Минимум {N} символов» |
| `ensure this value has at most N characters` | «Максимум {N} символов» |
| `value is not a valid integer` | «Должно быть целым числом» |
| `value error, ...` | «Ошибка валидации» (если конкретного нет) |

Реализация — helper `pydantic_msg_to_ru(msg, ctx)` в `backend/app/exceptions.py` (или `_macros.html` фильтр Jinja2 — на усмотрение реализации). Полный список — backend-агент собирает по факту тестов; mapping выше — стартовый набор.

### 4. Никаких i18n-фреймворков

- **gettext/babel/Flask-Babel** — отвергнуто. Один язык; накладные расходы на `.po`/`.mo`-файлы и их CI-flow не оправданы.
- **Локализация дат/чисел** — Python `babel` для дат **не подключаем**; используем готовые форматы:
  - Дата: `dd.mm.YYYY` (см. Jinja-фильтр `local_dt` в `_macros.html`, формат `{{ dt.strftime('%d.%m.%Y %H:%M') }}`).
  - Time-ago («3 минуты назад») — простая Jinja-функция в `_macros.html`, реализованная на if/elif (минут / часов / дней; формы числительных «1 минуту», «2 минуты», «5 минут» — поддерживаются).

### 5. Kept-as-is (английский)

Эти строки остаются **на английском** намеренно:

- `error.code` в логах и audit (`details`-jsonb).
- Системные имена: `INBOX`, `IMAP`, `SMTP`, `STARTTLS`, `SSL`, `Re:`, `Fwd:`.
- Headers email-сообщений (`From:`, `To:`, `Subject:` — RFC).
- API-схемы (`role`, `group_leader` — backend-сторона).
- HTML `<title>` страницы — на русском («Mail Aggregator | Входящие»), но `<meta name="application-name">` — на английском.

### 6. lang-атрибут и заголовки

`<html lang="ru">` (был `lang="en"`). Header `Content-Language: ru` не обязателен.

## Consequences

### Положительные
- Минимальный объём работы: пере-писать тексты в ~15 шаблонах + добавить один macro.
- Никаких CI-шагов на extraction/compilation `.po`-файлов.
- Простая поддержка: при добавлении нового error-code разработчик дописывает строку в `error_text`-MAP.

### Отрицательные / компромиссы
- Если когда-то понадобится EN-вариант (например, продукт выйдет на международный рынок) — придётся ретроактивно поднимать i18n-инфраструктуру и переписывать шаблоны (TD-016 в `100-known-tech-debt.md`).
- Backend-error-codes на английском, UI-текст на русском — разработчик при чтении логов делает мысленный mapping. На малой команде (1–3 разработчика) приемлемо.
- Form-validation тексты Pydantic частично русифицированы (см. §3); полное покрытие — итеративно.

## Alternatives considered

### A1. gettext / babel
Отвергнуто. См. §4 — overkill для одного языка.

### A2. JSON-словарь `i18n.ru.json` + Jinja-extension `{{ t('key') }}`
Отвергнуто. Лишний уровень indirection: поддерживать `i18n.ru.json` (с cross-file ключами) сложнее, чем писать «Войти» прямо в шаблоне. Macro `error_text(code)` — единственное оправданное исключение, потому что error-code уже есть как ключ.

### A3. Возвращать локализованные сообщения с backend (`error.message_ru`)
Отвергнуто. Сейчас `error.message` — generic «human-readable» на английском (см. `04-api-contracts.md`). Дублировать `message_ru` × всех ошибок = duplication; UI и так делает mapping в одном месте (macro). Кроме того, backend не знает локаль клиента (нет header `Accept-Language` обработки на старте).

### A4. Frontend-side i18n (JS-словарь, `data-i18n` атрибуты)
Отвергнуто. Server-rendered страницы; JS-i18n требовал бы flash on render → flicker при загрузке.
