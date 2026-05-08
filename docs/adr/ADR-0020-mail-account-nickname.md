# ADR-0020: Никнейм (display_name) у mail-аккаунтов

- **Статус:** accepted
- **Дата:** 2026-05-08

## Context

В группах (см. ADR-0019) несколько участников видят одни и те же mail-аккаунты. Чтобы быстро ориентироваться («это какой ящик?»), нужен короткий человекочитаемый ярлык, отличный от email-адреса. Например, два аккаунта `apple-account-12@gmail.com` и `apple-account-13@gmail.com` пользователь хочет видеть как «Apple Test 1» и «Apple Test 2».

## Decision

Добавить колонку `mail_accounts.display_name`:

```sql
ALTER TABLE mail_accounts
    ADD COLUMN display_name TEXT NULL
        CHECK (display_name IS NULL OR char_length(display_name) BETWEEN 1 AND 100);
```

- **Опциональное** поле (`NULL` допустим).
- **Не уникально** — два аккаунта могут иметь одинаковый ярлык (это display, а не key).
- Не индексируется (поиск по ярлыку не предполагается).

UI-логика везде, где раньше показывался `mail_account.email`:

```text
effective_account_label(account) = account.display_name if account.display_name else account.email
```

Места применения:
- Inbox (`inbox.html`) — колонка с указанием почтового аккаунта в строке письма.
- Message view (`message_view.html`) — поле «Аккаунт».
- Compose (`compose.html`) — dropdown «От кого» (`<option>` показывает label, `value` остаётся `id` аккаунта; full email — в `<option title="...">`-tooltip).
- Accounts list (`accounts/list.html`) — основной заголовок строки = label, под ним мелким шрифтом — full email.
- Admin — list пользователей с раскрытием mail-аккаунтов: label + email.

Hex-валидаций / regex / нормализаций — **нет**. Любая UTF-8 строка длины 1..100 (включая русский текст, эмодзи). Trim leading/trailing whitespace на backend перед сохранением; пустая строка после trim → `NULL`.

### API

`PATCH /api/mail-accounts/{id}` принимает опциональное поле `display_name: str | null`:

- Не передан — не меняем.
- Передан как непустая строка — сохраняем (после trim).
- Передан пустой строкой `""` или `null` — затираем в `NULL`.

`POST /api/mail-accounts` (create) тоже принимает `display_name` опционально (default `null`).

`GET /api/mail-accounts` и `GET /api/mail-accounts/{id}` возвращают `display_name` в DTO. Frontend использует helper `effective_account_label(account)`.

Form-encoded fallback (см. ADR-0015): поле `display_name` — обычная строка, пустая трактуется как `null`.

## Consequences

### Положительные
- Одна колонка, одна валидация, нулевые runtime-затраты.
- UX в группах резко улучшается — участники быстрее ориентируются в общих ящиках.
- Ничего не ломается: `NULL` = старое поведение (показывается email).

### Отрицательные / компромиссы
- Дубль ярлыков допустим (два аккаунта «Apple Test»). Если станет проблемой — добавить UNIQUE (user_id, display_name) WHERE NOT NULL; пока overkill.
- Поиск по ярлыку не работает (нет индекса). Не требуется на текущем масштабе (≤ 500 аккаунтов).

## Alternatives considered

### A1. Хранить ярлык в отдельной таблице `mail_account_labels` per-user (личные ярлыки, разные у разных участников группы)
Отвергнуто. Overengineering для текущего сценария — лидер группы единожды задаёт ярлык, все видят одинаково. Если кто-то захочет «свой» ярлык поверх — отдельный ADR.

### A2. Использовать `mail_accounts.email` как display, добавить только короткое `slug`
Отвергнуто. `slug` — машинно-читаемое, а пользователю нужен человекочитаемый текст с пробелами/русским/etc.

### A3. Заменить `email` на `display_name` (сделать email опциональным)
Отвергнуто. `email` — функциональное поле (IMAP login, From: header). Удалить нельзя.
