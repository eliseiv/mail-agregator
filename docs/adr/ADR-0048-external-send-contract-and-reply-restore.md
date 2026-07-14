# ADR-0048 — Контракт обобщённого send: ответ `{smtp_message_id}` (без `sent_id`), расщепление Фазы A2 на A2.1/A2.2, немедленное восстановление reply из CRM

| | |
| --- | --- |
| Статус | accepted |
| Дата | 2026-07-14 |

**Амендмент** [ADR-0043](./ADR-0043-strip-to-connector-push-to-crm.md) §3 (контракт ответа обобщённого send) и [ADR-0044](./ADR-0044-decommission-runbook.md) §2/§4 (Фаза A2 — порядок и объём). Парный CRM — `ADR-057`. Разрешает **живой прод-баг** (отправка ответа из CRM сломана) и **блокер Фазы A2** (источник `sent_id` не определён нигде).

## Context

### 1. Живой прод-баг: reply из CRM не работает с момента cut-over

CRM зовёт **`POST /api/external/mailboxes/{id}/send`** (`CRM backend/app/infra/mail_client.py:225` — `path = f"{_EXTERNAL_MAILBOXES_PATH}/{mailbox_id}/send"`). Такого маршрута в агрегаторе **нет**: полный перечень роутов `backend/app/external/router.py` — `GET /messages` (`:151`), `GET /mailboxes` (`:220`), `POST /messages/{message_id}/reply` (`:280`), `POST /mailboxes/test` (`:403`), `POST /mailboxes` (`:415`), `PATCH /mailboxes/{account_id}` (`:428`), `DELETE /mailboxes/{account_id}` (`:450`), `POST /mailboxes/{account_id}/sync` (`:462`), `POST /mailboxes/oauth/authorize` (`:551`), `GET /mailboxes/oauth/callback` (`:575`). Прод подтверждает: `404` на `/api/external/mailboxes/1/send`, `401` (= маршрут есть, ключ не предъявлен) на `/api/external/messages/1/reply`.

⇒ **С момента cut-over (10.07.2026) ответ на письмо из CRM не работает** — CRM получает от агрегатора `404` и маппит его в `404 mail_message_not_found` (`CRM backend/app/services/mail_service.py:1123-1124`). Дефект не был пойман, потому что кросс-репозиторный контракт отправки не покрыт ни одним контрактным тестом/смоуком (долг — `TD-059`, CRM `TD-062`).

### 2. Блокер Фазы A2: источник `sent_id` не определён

- [ADR-0043](./ADR-0043-strip-to-connector-push-to-crm.md) §3 задаёт ответ `200 { sent_id, smtp_message_id }`.
- [ADR-0044](./ADR-0044-decommission-runbook.md) §4 (Фаза A2) требует реализовать send **без записи в `sent_messages`**, а §1 дропает саму таблицу (Фаза D).
- Единственный производитель `sent_id` сегодня — `INSERT` в `sent_messages`: `backend/app/send/service.py:460` (`sent = await self._sent.insert(...)`, репозиторий подключён `:32`/`:284`).

⇒ После снятия writer'а у агрегатора **физически нет** источника целочисленного `sent_id`. Норма ADR-0043 §3 в этой части **невыполнима**, и исполнитель Фазы A2 законно заблокирован. Со стороны CRM поле при этом **обязательное**: `CRM backend/app/schemas/mail.py:128-129` (`sent_id: int`, `smtp_message_id: str`), и этой схемой CRM парсит **сырой** ответ агрегатора (`CRM mail_service.py:783` — `self._parse(MailReplyResponse, raw)`).

## Decision

### §1. Ответ обобщённого send сужается до `{ smtp_message_id }` — `sent_id` УДАЛЯЕТСЯ (амендмент ADR-0043 §3)

**Нормативный контракт (заменяет ответ ADR-0043 §3):**

| | |
| --- | --- |
| Метод / путь | `POST /api/external/mailboxes/{id}/send` (`{id}` — `mail_accounts.id`, `int ≥ 1`) |
| Авторизация | `EXTERNAL_API_KEY` (`X-API-Key` \| `Bearer`) + гейт `EXTERNAL_WRITE_ENABLED` + `LIMIT_EXTERNAL_WRITE` (ADR-0039 §1) |
| Запрос | `{ to: string[], cc?: string[] \| null, subject?: string \| null, body_text: string, in_reply_to?: string, refs?: string }` |
| **Ответ 200** | **`{ smtp_message_id: string }`** — и больше ничего |
| Коды | `200` / `400 validation_error` / `401` / `403 forbidden` (write off) / `404` (**ящик `{id}` не найден**) / `409` / `422` / `502 smtp_failed` |

Тело запроса — **ровно то, что CRM уже шлёт** (`CRM mail_client.py:218-226`: `to`, `cc`, `subject`, `body_text`, опционально `in_reply_to`, `refs`); правки клиента CRM в части запроса не требуются.

**Почему `sent_id` уходит, а не переносится:** durable-запись факта отправки — в CRM (`mail_sent_messages`, ADR-0043 §4). Агрегатор после A2 её не ведёт, значит любой `sent_id` в его ответе был бы **суррогатом, не ссылающимся ни на одну строку** — ложный идентификатор в контракте. Идентификатор отправки выдаёт **CRM** из собственной таблицы (её `id` — `uuid`, `CRM backend/app/models/mail_sent_message.py:35-37`); наружу, клиенту CRM, он и отдаётся (парная норма — CRM `ADR-057` §2). Это вариант (б) из постановки; вариант (а) (агрегатор продолжает писать `sent_messages`) отклонён — см. Alternatives.

**Валидация (переносится из ADR-0035, не теряется):** каждый адрес `to`+`cc` — валидный e-mail; суммарно `to+cc` ≤ 100; `subject` ≤ 998; `body_text` непустой (после `strip`), ≤ 1 MiB. Нарушение → `400`/`422`. Threading-заголовки (`In-Reply-To`/`References`) агрегатор **не сочиняет** — пишет в MIME ровно то, что пришло в `in_reply_to`/`refs` (их формирует CRM).

**Реюз ядра:** `send/mime.py` + SMTP-ядро `send/service.py::_send_core` (OAuth-token resolve → MIME → SMTP → best-effort IMAP APPEND в «Sent»). Единственное отличие от нынешнего пути — **`INSERT` в `sent_messages` не выполняется** (`send/service.py:460` не участвует в новом пути; см. §3).

### §2. Reply сводится к send **без потери функции** (проверено по коду обеих сторон)

Что делал message-scoped reply (`send/service.py:306-364`) и чем это закрыто:

| Функция reply (ADR-0035) | Где живёт после перехода |
| --- | --- |
| Резолв ящика-отправителя по письму (`original.mail_account_id`, `:338`) | CRM передаёт `mailbox_id` в пути (`CRM mail_service.py:775` — `send_message(message.mail_account_id, …)`) |
| Дефолт `to` = `[original.from_addr]`, `subject` = `"Re: " + …` (`:348-349`) | CRM (`CRM mail_service.py:797-825`, `_prepare_reply`) |
| Threading `In-Reply-To`/`References` из оригинала (`_resolve_threading`, `:350`) | CRM (`CRM mail_service.py:759-760` + `_build_references` `:827-835`) → передаёт в `in_reply_to`/`refs` |
| MIME + SMTP + best-effort IMAP APPEND в «Sent» (`_send_core`, `:366+`) | **Без изменений** — тот же `_send_core` |
| Запись `sent_messages` (`:460`) | CRM `mail_sent_messages` (`CRM mail_service.py:784-794`) |

**Потери функции нет.** Тело оригинала/цитирование агрегатор и раньше не подмешивал (`body` уходил как есть) — это забота UI CRM. Вложений нет by design (ADR-0043).

**Осознанное расширение поверхности (security-последствие, фиксируется явно):** reply мог отправить письмо **только** с ящика хранимого оригинала, обобщённый send — с **любого** ящика **любому** адресату под машинным ключом. Компенсация: тот же ключ + `EXTERNAL_WRITE_ENABLED` (default `false`) + `LIMIT_EXTERNAL_WRITE`; инициатором остаётся CRM под JWT/RBAC пользователя (`mail:view`). Сдвиг границы — тот же, что уже принят ADR-0043 §3; здесь он назван прямо.

### §3. Фаза A2 расщепляется: **A2.1 (аддитивная, деплоится немедленно)** и **A2.2 (снос reply — вместе с A1/A3-релизом)** (амендмент ADR-0044 §2/§4)

Прежняя формулировка Фазы A2 склеивала «реализовать send» и «удалить reply + writer `sent_messages`» в один шаг, из-за чего восстановление прод-функции оказывалось заложником всего демонтажа. Расщепляем:

**A2.1 — восстановление отправки (аддитивно, без единого DDL, откат бесплатен):**
1. Реализовать `POST /api/external/mailboxes/{id}/send` по §1 — **новый путь `_send_core` БЕЗ `INSERT` в `sent_messages`** (writer `send/service.py:460` не зовётся из этого пути; таблица ещё жива и её старый writer из session-`send` пока не тронут).
2. Ничего не удалять: `POST /api/external/messages/{id}/reply`, `EXTERNAL_REPLY_ENABLED`, `SentMessagesRepo` — **остаются** (откат = снять новый роут).
3. Гейт §9 ADR-0044 (import / mypy / тесты) — обязателен и здесь.
4. Парный релиз CRM (`ADR-057` §2): CRM перестаёт парсить `sent_id` из ответа агрегатора.

**A2.2 — снятие старого пути (в атомарном A1+A3-релизе, гейтится подтверждённой работой A2.1 на проде):**
1. Удалить `POST /api/external/messages/{id}/reply` (`external/router.py:280-355`) + `_parse_reply_body` (`:260`) + `ExternalReplyRequest`/`ExternalReplyResponse` (`external/schemas.py:152-200`) + `SendService.send_external_reply` (`send/service.py:306-364`).
2. Вывести из употребления `EXTERNAL_REPLY_ENABLED` (`shared/config.py:208`) и `EXTERNAL_REPLY_RATE_LIMIT*` (env-чистка — Фаза G).
3. Снять writer `sent_messages`: импорт `SentMessagesRepo` (`send/service.py:32`), поле `:284`, `INSERT` `:460` + session-visibility-методы. Только после этого `sent_messages` дропается (Фаза D).

**Инвариант порядка:** `reply` живёт, пока CRM фактически не переключена на `send` **на проде**; `sent_messages` дропается, только когда снят её последний writer. Это ужесточение прежней формулировки ADR-0044 §2, а не отступление от неё.

### §4. `404` от send означает «ЯЩИК не найден» (нормативно для маппинга на стороне CRM)

В message-scoped reply `404` значил «нет письма» (`send/service.py:335`). В обобщённом send письма нет в контракте вовсе: `404` = **ящика `{id}` нет в агрегаторе**. CRM обязана маппить его как рассинхрон каталога (`404 mail_mailbox_not_found`), а **не** как «письма нет» — норма и правка на стороне CRM (`ADR-057` §3).

### §5. Временный вызов `/messages/{id}/reply` из CRM — ЗАПРЕЩЁН (обоснование отказа от «быстрой заплатки»)

Соблазн «пока починить CRM, чтобы звала существующий reply» отклонён — он **опасен**, а не просто временен:

1. **`id` писем CRM ≠ `id` писем агрегатора после cut-over.** `preserve id` действовал только на ETL (CRM `ADR-044` §10). Новые письма CRM вставляет **своим** `BIGSERIAL` (`CRM backend/app/models/mail_message.py:45`) при приёме push'а через `INSERT … ON CONFLICT DO NOTHING` (`CRM backend/app/repositories/mail_message_repository.py:66`); Postgres расходует `nextval` **и на конфликтных** вставках, а повторные push'и штатны (`crm_push_recovery`, ADR-0043 §2) → последовательность CRM уходит вверх относительно агрегаторской. Совпадение id **не гарантировано** ⇒ ответ мог бы уйти **по чужому письму** (чужой адресат, чужой ящик). Недопустимо.
2. **Ретенция 30 дней** (`worker/app/cleanup.py:34-40`, `RETENTION_DAYS` default `30` — `shared/config.py:99`): по письму старше 30 дней оригинала в агрегаторе уже нет → reply физически невозможен (`404`), тогда как в CRM письмо есть.
3. **Гейт `EXTERNAL_REPLY_ENABLED` default `false`** (`shared/config.py:208`) — при выключенном флаге заплатка вернула бы `403` даже с валидным ключом.
4. Правка CRM под старый контракт по объёму сопоставима с правкой под целевой (иные путь и имя поля тела: `body` вместо `body_text` — `external/schemas.py:169`), но её потом пришлось бы снимать.

⇒ **Восстановление = fix-forward через A2.1** (аддитивный релиз, часы работы), а не заплатка. Отдельного TD на заплатку не заводится — её не будет.

## Consequences

- Прод-функция «ответ на письмо» восстанавливается **релизом A2.1**, независимо от остального демонтажа; до его деплоя reply из CRM не работает (`TD-059`).
- Фаза A2 больше не блокирована: источник `sent_id` определён — **его нет** в ответе агрегатора; идентификатор выдаёт CRM.
- Контракт ответа ADR-0043 §3 (`{sent_id, smtp_message_id}`) **отменён** в части `sent_id`; ADR-0044 §2/§4 (Фаза A2) — расщеплён на A2.1/A2.2.
- Парные правки CRM (обязательны, иначе CRM не распарсит ответ): схема ответа агрегатора, публичный `MailReplyResponse.sent_id` (`int` → `uuid`), маппинг `404` → `mail_mailbox_not_found`. Нормируются CRM `ADR-057`.
- Кросс-репозиторный разрыв прожил на проде 4 дня незамеченным → заведён `TD-059` (нет consumer-driven контрактной проверки внешнего API; парный CRM `TD-062`).

## Alternatives considered

- **(а) Агрегатор продолжает писать `sent_messages` и отдавать `sent_id`.** Отклонён: противоречит ADR-0043 §4 (durable-лог отправленного — в CRM) и §1 ADR-0044 (таблица под drop); означал бы **два** источника истории отправки и сохранение таблицы/репозитория/`sent_attachments`-связки в «тонком коннекторе» ради одного числа в ответе, которое CRM всё равно не использует.
- **Оставить `sent_id` в ответе как синтетическое число** (напр. `id` из счётчика Redis или `0`). Отклонён: идентификатор, не ссылающийся ни на что, — ложь в контракте; ловится только в момент, когда по нему попытаются что-то найти.
- **Временный вызов reply из CRM** — отклонён, §5 (риск ответа по чужому письму, ретенция, гейт).
- **Оставить message-scoped reply навсегда, отказавшись от send.** Отклонён (уже в ADR-0043 §3, здесь подтверждено кодом): reply резолвит оригинал в `messages` агрегатора (`send/service.py:331`), а тот — рабочий буфер с ретенцией 30 дней; система-запись писем — CRM. Threading по локальному письму в такой модели ненадёжен.
