# ADR-0017: Теги для писем — rule-based авто-классификация и пользовательские правила

- **Статус:** **superseded by [ADR-0043](./ADR-0043-strip-to-connector-push-to-crm.md)** (2026-07-10) — теги целиком уходят из агрегатора в CRM (движок матчинга переносится ПОБУКВЕННО, CRM `ADR-044` §5). Ранее: accepted (владение/builtin частично изменены [ADR-0040](./ADR-0040-global-tags.md), 2026-07-09)
- **Дата:** 2026-05-07

> **⚠️ Частично superseded — [ADR-0040](./ADR-0040-global-tags.md) (2026-07-09).** Изменены: (а) **персональная** модель владения (`tags.user_id NOT NULL`, `UNIQUE(user_id, name)`) → тег может быть **глобальным** (`user_id IS NULL`, единый админский каталог); (б) создание builtin-тегов через **post-login hook** `TagsService.ensure_builtin_tags(user_id)` (§6) → **глобальное идемпотентное сидирование в lifespan** (`seed_builtin_tags`, по образцу `seed_super_admin`), ленивый per-login hook отменён (UI-логина в агрегаторе не будет — ADR-0041). Семантика матчинга (§4/§4.1/§4.2 — whole-word/case-sensitive/escape), набор правил и запрет DELETE на builtin — **в силе**. Актуальная модель — `03-data-model.md` секция `tags` + ADR-0040. Текст ниже сохранён как исторический record исходного решения.

## Context

Пользователи (включая супер-админа) хотят быстро визуально классифицировать входящие письма. Текущая модель имеет только бинарный признак `is_read` — недостаточно для разделения писем по бизнес-категориям (например, диспуты от Apple, уведомления о подписках, продление сертификатов).

Запрос продукта (TZ Sprint feature "tags"):

1. Четыре **встроенных** тега, срабатывающих по детерминированным правилам при синхронизации:
   - `DPLA.PLA` — `subject` ИЛИ `body_text` содержит `"DPLA"` или `"PLA"`.
   - `Диспут` — `subject` содержит `"Apple Inc"` ИЛИ `from_addr = "AppStoreNotices@apple.com"`.
   - `Отменить подписку` — `body_text` содержит `"cancel"` или `"subscription"`.
   - `Продление аккаунта` — `body_text` содержит `"Your Distribution Certificate will no longer be valid in 30 days"`.
2. UI для **пользовательских** тегов — кнопка `+ Добавить тег`, форма с именем тега и набором условий (keyword-в-subject / keyword-в-body / sender substring / sender exact). Чекбокс "Применить к существующим письмам" при создании.

Ключевые ограничения, под которые мы выбираем дизайн:
- Объёмы малые (≤ 500 mail-аккаунтов × 30 дней ретенции × ~50 писем/день ≈ 750k писем max в БД); см. `03-data-model.md` секция "Объёмные оценки".
- Стек уже зафиксирован — Postgres 16 + Redis + APScheduler-worker (см. ADR-0001, ADR-0003).
- Плагинная сложная фильтрация / regex-движок не нужны; правила — простые substring-матчи.
- Sync — `apply_tags` должен встраиваться в существующий цикл (`worker.sync_cycle.save_message`), не блокируя пакетную обработку (см. ADR-0008, ADR-0013).
- Безопасность — пользователь A не может прицепить свой тег к письму пользователя B, и не может видеть теги пользователя B.

## Decision

### 1. Per-user изоляция тегов

Все теги — **per-user**. Тег `DPLA.PLA` создаётся **отдельно для каждого пользователя**; admin тоже видит свои четыре builtin-тега и может создавать собственные. У двух разных пользователей могут быть теги с одинаковыми именами — это два разных объекта в БД (разные `user_id`).

Обоснование:
- Изоляция «из коробки» через FK на `users(id)` + JOIN при чтении.
- Нет глобального namespace — нет конфликтов имён между пользователями.
- Builtin-теги создаются автоматически при первом login пользователя (post-login hook в `auth.AuthService.login`); не data-миграция, чтобы не плодить мёртвых записей для пользователей, которые никогда не логинились.

### 2. Схема БД

Три новые таблицы (DDL — см. `03-data-model.md`):

- `tags` — `(id, user_id, name, color, is_builtin, created_at, updated_at)`. UNIQUE `(user_id, name)`.
- `tag_rules` — `(id, tag_id, type, pattern, created_at)`. Поле `type` — enum-string: `subject_contains | body_contains | sender_contains | sender_exact`. Несколько rules для одного тега соединяются логическим **OR** (см. ниже).
- `message_tags` — many-to-many link `(message_id, tag_id, created_at)`. PK = `(message_id, tag_id)`.

Каскадные удаления:
- `tags.user_id` → `users(id)` `ON DELETE CASCADE`. При удалении пользователя его теги исчезают; `message_tags` каскадятся через `tag_id`.
- `tag_rules.tag_id` → `tags(id)` `ON DELETE CASCADE`.
- `message_tags.message_id` → `messages(id)` `ON DELETE CASCADE` (retention cleanup автоматически чистит links при удалении messages).
- `message_tags.tag_id` → `tags(id)` `ON DELETE CASCADE`.

Индексы:
- `tags`: PK `(id)`, UNIQUE `(user_id, name)`, INDEX `(user_id)` (для list).
- `tag_rules`: PK `(id)`, INDEX `(tag_id)` (для load-all-rules-for-tag).
- `message_tags`: PK `(message_id, tag_id)`, дополнительный INDEX `(tag_id, message_id)` (для list-messages-with-tag, см. inbox filter `tag_id`).

### 3. Логика между rules — OR

В рамках одного тега несколько rules объединяются по **OR**. То есть тег прикладывается к письму, если **хотя бы одно** правило сработало.

Обоснование:
- Соответствует mental model пользователя: «прицепи этот тег если письмо про X **или** про Y».
- AND-логика выражается множеством отдельных тегов (если очень нужна — не блокер, но и не ясный пользовательский запрос).
- Не плодит дополнительных полей (group / boolean) в `tag_rules`; UI остаётся простым (плоский список).

### 4. Сравнение — whole-word, case-SENSITIVE, по нормализованному тексту (без пользовательского regex)

> **Историческая заметка.** В первой редакции ADR (2026-05-07) §4 описывал
> `ILIKE '%' || pattern || '%'` — substring, case-INsensitive. Это
> переписано в round-23/27 (см. раздел «История изменений §4/§5/§7» ниже).
> Актуальная семантика — ниже; старый ILIKE более **не используется** ни в коде,
> ни в `05-modules.md`.

Три типа `*_contains` (`subject_contains` / `body_contains` / `sender_contains`)
выполняются как **whole-word, case-SENSITIVE** матч через POSIX-оператор `~`
(case-sensitive; `~*` НЕ используется) над **regex-экранированным** паттерном.
`sender_exact` без изменений — `LOWER(from_addr) = LOWER(pattern)` (адреса/домены
де-факто регистронезависимы).

**4.1. Границы слова — явные граничные классы, НЕ `\y`.**
round-23 обернул паттерн в `\y … \y` (word-boundary). Это оказалось багом
(round-27): `\y` — граница «словесный↔несловесный символ». Если паттерн
**начинается или заканчивается на пунктуацию**, обёртка не матчит — после
финальной точки/`!` идёт пробел/конец строки, обе стороны несловесные → границы
нет → нет матча. Это ломало реальные пользовательские теги:
- `body_contains = "We noticed an issue with your submission that requires your attention."` (точка на конце) — `…attention\.\y` не матчился.
- `body_contains = "Congratulations!"` (`!` на конце) — то же.
При `match_mode='all'` один несработавший rule снимает весь тег.

Актуальная обёртка — **явные граничные классы** вместо `\y`:

```
value ~ ( '(^|[^[:alnum:]_])' || <escaped_pattern> || '([^[:alnum:]_]|$)' )
```

Граница = «начало строки **или** не-буквенно-цифровой-и-не-`_` символ» слева и
«не-буквенно-цифровой-и-не-`_` символ **или** конец строки» справа. Это
сохраняет ровно те же гарантии, что и `\y`, для паттернов из букв/цифр
(`PLA` внутри `DPLA` не матчит как слово; `pla` ≠ `PLA`), но дополнительно
**корректно работает с паттернами, обрамлёнными пунктуацией** — потому что
сам пунктуационный символ паттерна (экранированный) и так является
не-словесным, а граничный класс смотрит на соседа за пределами паттерна.

**4.2. Нормализация пробелов — ОБЯЗАТЕЛЬНА (решение round-27, на фактах из БД).**
`messages.body_text` формируется из `text/plain` либо из `html2text(html)`
(`worker/app/imap_fetcher.py`). Оба источника вставляют **внутрь логически
одного предложения** жёсткие переносы строк, последовательности из 2+ пробелов
(артефакты таблиц/обёрток) и неразрывные пробелы U+00A0. Замер на локальной БД
(148 писем): **148/148** содержат `\n`, **108/148** — прогоны из 2+ пробелов,
**17/148** — U+00A0. Реальный фрагмент (msg id 466, оператор `escape`):

```
Dear Bybit Card holder,   Congratulations! Your Bybit card application…
                       ^^^ три пробела между словами
```

Эмпирическая проверка на этом теле (psql):
- паттерн `holder, Congratulations` (один пробел) против сырого `body_text` → **НЕ матч** (`f`);
- тот же паттерн против `regexp_replace(body_text, '\s+', ' ', 'g')` → **матч** (`t`).

Вывод: даже после фикса границ (§4.1) многословные паттерны `*_contains`
молча не сработают на реальных телах. **Нормализация необходима.** Решение:
сравнивать **нормализованный текст** против **нормализованного паттерна**:

```
norm(x) := regexp_replace( translate(x, chr(160), ' '), '\s+', ' ', 'g' )
```

— сначала `translate(chr(160) → ' ')` (U+00A0 → обычный пробел), затем
схлопывание любых прогонов whitespace в один пробел. Порядок важен: в локали
этого деплоя Postgres `\s` / `[[:space:]]` **НЕ** считают U+00A0 пробелом
(проверено: `E'a b' ~ 'a\sb'` → `f`), поэтому nbsp нужно перевести
**явно, до** `\s+`-схлопывания. Zero-width символы (U+200B/U+FEFF) сюда не
попадают — они срезаются upstream в `strip_invisible_padding`
(`imap_fetcher.py`). `norm()` применяется к обеим сторонам: к
`subject`/`body_text`/`from_addr`/`COALESCE(from_name,'')` и к
`regexp_replace(r.pattern, …)` (экранированный паттерн).

`subject` и `from_addr`/`from_name` нормализуются той же `norm()` для
единообразия (subject из IMAP-заголовка обычно чистый, но RFC 2047
encoded-words и folding изредка дают двойные пробелы — нормализация безвредна).

**4.3. `body_contains` матчит И `body_text`, И текст из `body_html`
(решение round-29, на фактах из прода).**

Письма хранятся в двух телах (см. `03-data-model.md` секция `messages`):
`body_text` (TEXT NOT NULL — `text/plain`-часть письма либо `html2text(html)`
если `text/plain` нет) и `body_html` (TEXT NULL — сырой HTML из `text/html`-части,
как пришёл от отправителя). **UI рендерит `body_html`** (`message_view.html`) —
то есть пользователь видит глазами HTML-версию. До round-29 `body_contains`
матчил **только** по `body_text`.

Проблема (подтверждена на проде, аккаунт `achilles.alex3611@aol.com`, реальные
письма id 265/381): MIME-письма Apple несут **разный текст** в `text/plain` и
`text/html`-частях одного письма. Конкретно письмо «реджект»:
- `body_text` (`text/plain`): «During our review, we noticed an issue with your submission.» — паттерна тега «Реджект» **НЕ содержит**.
- `body_html` (то, что видит пользователь): «We noticed an issue with your submission that requires your attention.» — паттерн **СОДЕРЖИТ**.

Поэтому тег не навешивался на письмо, в котором пользователь **глазами видит**
триггерную фразу. Эмпирическая проверка на проде (read-only SELECT, письмо id 265):
- `norm(body_text) ~ boundary(pattern)` → `f` (не матчит);
- `norm(strip_tags(body_html)) ~ boundary(pattern)` → `t` (**матчит**).

Решение: предикат `body_contains` матчит, если паттерн найден в `body_text`
**ИЛИ** в тексте, извлечённом из `body_html` снятием тегов. Каноническая форма
`body_contains` (применяется в обоих запросах, в обеих ветках any/all):

```
  norm(body_text)                      ~ boundary(norm(escaped_pattern))
OR
  norm( strip_tags(COALESCE(body_html,'')) ) ~ boundary(norm(escaped_pattern))

где:
  strip_tags(x) = regexp_replace(x, '<[^>]+>', ' ', 'g')   -- HTML-тег → пробел
  boundary(p)   = '(^|[^[:alnum:]_])' || p || '([^[:alnum:]_]|$)'  -- см. §4.1
  norm(x)       = regexp_replace(translate(x, chr(160), ' '), '\s+', ' ', 'g')  -- см. §4.2
```

Порядок: `strip_tags` снимает теги (превращая `<p>foo</p>` в ` foo `), затем
`norm()` схлопывает образовавшиеся прогоны пробелов в один — иначе на стыках
тегов многословный паттерн не сматчился бы. `body_html` оборачивается в
`COALESCE(…, '')`, т.к. колонка nullable (NULL `~` всегда NULL → строка
выпала бы; пустая строка просто никогда не матчит).

**Затрагивается только `body_contains`.** `subject_contains` остаётся по
`subject`, `sender_contains`/`sender_exact` — по `from_addr`/`from_name`. HTML
есть только у тела, поэтому html-ветку получает только тело.

**Почему двойной матч (`body_text` OR stripped `body_html`), а не «строить
`body_text` всегда из `html2text(body_html)`».** Альтернатива A10 (см. ниже)
делает `body_text` и UI консистентными по одному полю, но (а) требует backfill
всех существующих писем (re-fetch из IMAP или data-миграция, что нарушает
schema-only-правило `03-data-model.md`), и (б) **не чинит существующие письма**,
пока их не перезагрузят. Двойной матч чинит проблему **немедленно для уже
лежащих в БД писем** через `apply-to-existing` (пользователь пересоздаёт/применяет
тег и старые письма получают тег), и одинаково — для новых писем через worker-hook.
Поэтому выбран двойной матч; A10 отвергнута.

**Ограничение — HTML-entities (TD-024).** `strip_tags` снимает только теги
`<…>`; HTML-сущности (`&amp;`→`&`, `&#39;`→`'`, `&nbsp;`→неразрывный пробел и т.п.)
он **не декодирует**. Если триггерная фраза в `body_html` содержит сущности
(например, пользовательский паттерн `AT&T` против html `AT&amp;T`), html-ветка
её пропустит. Для текущего Apple-кейса фраза чистая (без сущностей), поэтому фикс
работает; общий случай зафиксирован как TD-024 (см. `100-known-tech-debt.md`).
`&nbsp;` частично смягчается тем, что `norm()` переводит U+00A0 в пробел —
**но только если** сущность уже декодирована в символ U+00A0; сырой текст
`&nbsp;` в `body_html` останется буквальной строкой и `norm()` его не тронет.

Обоснование общей модели:
- **Безопасность.** Пользователь не вводит regex — каждый метасимвол его
  паттерна экранируется (`regexp_replace`), поэтому до движка доходит только
  литеральная строка + наши фиксированные граничные классы и `\s+`.
  Catastrophic backtracking невозможен (ReDoS-инвариант §A2 сохранён): структура
  анкерована, без вложенных квантификаторов над пользовательскими данными.
- **Регистр контролирует пользователь.** Паттерн `DPLA` (капс) ловит только
  капсовое слово; `dpla`/`pla` — нет. Двойная защита от ложных срабатываний:
  неверный регистр **и** подстрока-внутри-слова.
- **`%`/`_` в паттерне** теперь — обычные литеральные символы (не wildcard).
  UI-примечание про «`%`/`_` как wildcard» убирается; добавляется «совпадение
  регистрозависимое, по целым словам, пробелы внутри паттерна сопоставляются с
  любым пробельным разделителем в тексте».

### 5. Apply tags при синхронизации (worker)

В `worker.sync_one_account.save_message`, **в той же транзакции**, что и `INSERT INTO messages ... ON CONFLICT (mail_account_id, uidvalidity, uid) DO NOTHING RETURNING id`, выполняется:

Актуальная форма (round-24 match_mode + round-25 sender_name + round-27
границы/нормализация + round-29 body_html-ветка; canonical-текст —
`backend/app/tags/sql.py`, для каждого `*_contains` плеча предикат имеет вид
`norm(value) ~ '(^|[^[:alnum:]_])' || norm(escaped_pattern) ||
'([^[:alnum:]_]|$)'`, где `norm(x) = regexp_replace(translate(x, chr(160), ' '),
'\s+', ' ', 'g')`; для `body_contains` — дополнительная html-ветка
`norm(strip_tags(COALESCE(:body_html,''))) ~ boundary(...)`, см. §4.3):

```sql
INSERT INTO message_tags (message_id, tag_id)
SELECT :message_id, t.id
FROM tags t
JOIN users u ON u.id = t.user_id
JOIN mail_accounts ma ON ma.id = :mail_account_id
WHERE (
        u.id = ma.user_id
        OR (ma.group_id IS NOT NULL AND u.group_id = ma.group_id)
        OR u.role = 'super_admin'          -- round-28; см. §5.1
    )
  AND ( /* match_mode 'any': EXISTS(rule matches);  'all': >=1 rule AND NOT EXISTS(rule fails)
           subject/sender-предикат: norm(value) ~ boundary(norm(escaped_pattern));
           body_contains: norm(:body) ~ boundary(...) OR norm(strip_tags(COALESCE(:body_html,''))) ~ boundary(...) — §4.3 */ )
ON CONFLICT (message_id, tag_id) DO NOTHING;
```

**Bind-параметры worker-hook (round-29).** Hook принимает тело письма как
bind-параметры (резолвятся воркером из `FetchedMessage`), а не из колонок `m.*`.
До round-29 передавался только `:body` (= `body_text`). Чтобы html-ветка §4.3
работала и для **нового** письма, добавлен второй bind `:body_html`
(= сырой HTML письма, тот же, что уйдёт в колонку `messages.body_html`).
Конкретные правки (минимально-инвазивный путь — HTML уже под рукой на call-site,
`fmsg.body_html` уже пишется в `insert_message_idempotent`):
- `worker/app/sync_cycle.py`: dataclass `_TagInputMessage` получает поле
  `body_html: str | None`; на call-site `apply_tags_to_message(...)` оно
  заполняется из `fmsg.body_html` (уже доступно, без доп. round-trip к БД).
- `backend/app/tags/service.py`: Protocol `_MessageLike` получает свойство
  `body_html: str | None`; метод `apply_tags_to_message` добавляет в bind-dict
  `"body_html": message.body_html`.
- `backend/app/tags/sql.py`: в `APPLY_TAGS_TO_MESSAGE` (обе ветки any/all)
  плечо `body_contains` получает html-альтернативу
  `… OR norm(strip_tags(COALESCE(CAST(:body_html AS TEXT), ''))) ~ boundary(...)`.
  CAST нужен по той же причине, что и `:sender_name` (asyncpg не выводит тип
  bare-параметра для nullable-bind).

В `APPLY_TAG_TO_EXISTING` проще — `m.body_html` доступен из колонки напрямую,
bind не нужен; в `body_contains` добавляется
`… OR norm(strip_tags(COALESCE(m.body_html, ''))) ~ boundary(...)`. Обе точки
навешивания (auto-tagging новых писем и apply-to-existing) после правки матчат
**одинаково** — по `body_text` ИЛИ по тексту из `body_html`.

Один SQL-запрос на одно письмо — все теги всех видящих письмо пользователей
проверяются за один round-trip. Транзакционность: если apply упал — message тоже
откатывается (избегаем orphan messages без тегов). При `INSERT messages ... ON
CONFLICT DO NOTHING` без RETURNING (когда письмо уже было — не добавилось)
`apply_tags` пропускается (нет нового `message_id`).

#### 5.1. Видимость в worker-hook включает super_admin (round-28)

Worker-hook навешивает тег пользователя на новое письмо, если этот пользователь
**видит** письмо. Изначально видимость = владелец ящика **или** одногруппник
(`ma.group_id = u.group_id`). super_admin (`role='super_admin'`,
`group_id = NULL`, не владелец чужих ящиков) под оба условия не попадал → его
теги **не навешивались на письма чужих команд** → `message_tags` пусто →
recipient-SQL уведомлений (`telegram_notifications.list_recipients_for_message`,
там уже есть ветка `u.role='super_admin'`) ничего не слал. То есть super_admin не
получал TG-уведомление по своему тегу на чужом письме.

Решение: добавить в видимость worker-hook ветку `OR u.role = 'super_admin'`. Это
**симметрично round-26**, который уже дал super_admin полный охват в
`APPLY_TAG_TO_EXISTING` (флаг `:is_super_admin` форсит фильтр в TRUE). Теперь обе
точки навешивания тегов согласованы со scope чтения super_admin
(`MessageService.visible_user_ids` → `None` = «видит все»).

**Scope round-28.** Расширение затрагивает **только** два потока:
(1) таблицу `message_tags` (строки персональных тегов super_admin появляются на
письмах чужих команд) и (2) **TG-нотификации** super_admin'у (`telegram`-канал).
Расширение **НЕ** затрагивает webhook-канал команд (ADR-0023): персональные теги
super_admin **не** должны попадать ни в trigger webhook'а команды, ни в его
payload (см. «Webhook-вектор» ниже и ADR-0023 §3.2). Пользователь запросил
именно TG-уведомление super_admin — не webhook.

**Почему утечки в INBOX чужих команд нет (корректное обоснование).**
inbox read-path показывает на письме **только теги владельца ящика**:
`JOIN message_tags mt → tags t` идёт с условием `t.user_id = ma.user_id` (тег —
атрибут владельца сообщения, не зрителя; см. ADR-0019 §7.4 и `05-modules.md` §10
«Tag-aware fields в DTO», предикат `t.user_id = ma.user_id`). super_admin **не
владелец** чужих ящиков (`ma.user_id ≠ super_admin.id`), поэтому его
`message_tags`-строка на чужом письме **существует**, но JOIN по
`t.user_id = ma.user_id` её **отсекает** — ни лидер, ни участник чужой команды
super_admin-тег в inbox не увидят. Утечки в read-path нет.

> Предыдущая редакция этого пункта ошибочно ссылалась на webhook-SQL
> (`LIST_TAGS_FOR_TEAM` / `FIND_ACTIVE_WEBHOOK_FOR_MESSAGE`, предикат
> `u.role='super_admin' OR u.group_id=:gid`) как на «inbox read-path». Это было
> неверно вдвойне: (а) тот предикат относится к webhook-каналу, не к inbox;
> (б) он super_admin-теги **включает**, а не исключает — то есть аргумент
> доказывал обратное. Исправлено: inbox-обоснование строится на
> `t.user_id = ma.user_id` (выше).

**Webhook-вектор (новый, честный анализ; решение — изолировать).**
До round-28 у super_admin не было `message_tags`-строк на письмах чужих команд →
webhook-EXISTS (`find_active_for_message`, ADR-0023 §3.2) по его тегу не
срабатывал. round-28 вводит **реальную межкомандную утечку во внешнюю систему**,
если webhook-SQL оставить как был (`OR u.role='super_admin'`):
- (а) **ложный trigger.** webhook команды A начал бы срабатывать на письме, у
  которого есть **только** super_admin-тег (новая строка `message_tags`), хотя ни
  один член команды A тег на это письмо не повесил.
- (б) **утечка имени/цвета.** `name`/`color` персонального тега super_admin ушли
  бы в JSON-payload (`message.tags[]`) на **внешний** receiver команды A через
  `list_tags_for_team` (там был `OR u.role='super_admin'`). Это раскрытие
  персональных данных super_admin внешней системе чужой команды.

**Решение (принято в main chat): изолировать webhook от персональных тегов
super_admin.** webhook-канал должен учитывать только теги в пределах видимости
**самой команды** — теги участников группы и владельца ящика, **но не**
персональные теги super_admin. Конкретно (ADR-0023 §3.2, `05-modules.md` §19,
`backend/app/webhooks/sql.py`): в обоих webhook-запросах
(`FIND_ACTIVE_WEBHOOK_FOR_MESSAGE` EXISTS-предикат и `LIST_TAGS_FOR_TEAM`
WHERE-предикат) условие на принадлежность тега меняется

```
(u.role = 'super_admin' OR u.group_id = ma.group_id)     -- было: включает super_admin
        ↓
(u.group_id = ma.group_id OR u.id = ma.user_id)          -- стало: только команда + владелец ящика
```

Ветка `u.id = ma.user_id` сохраняет теги владельца ящика на случай ящика,
владелец которого вне группы (defensive; в норме владелец — член группы).
super_admin (`group_id` иной/NULL, не владелец чужого ящика) под оба условия не
попадает → его персональный тег webhook команды не триггерит и в payload не
утекает. Подробности и DDL-неизменность — в ADR-0023 §3.2.

**Побочные эффекты round-28 и почему они безопасны:**
- Персональные теги super_admin начнут навешиваться на **все** письма системы
  (строки в `message_tags`) — это и есть желаемое поведение (super_admin видит
  весь inbox; TG-уведомление — единственный канал, ради которого расширение
  делается).
- **Inbox чужих команд** super_admin-тег не показывает (см. обоснование через
  `t.user_id = ma.user_id` выше).
- **Webhook чужих команд** super_admin-тег не триггерит и в payload не включает
  (см. «Webhook-вектор» выше — предикат изменён в ADR-0023 §3.2).
- **TG-нотификация** super_admin'у срабатывает корректно: recipient-SQL
  (`telegram_notifications.list_recipients_for_message`, ADR-0022 §2.2)
  джойнит тег per-recipient (`t.user_id = u.id`) и имеет ветку
  `u.role='super_admin'` — super_admin получает уведомление по **своему** тегу.
  Это и есть цель расширения.
- **Дедуп** уведомлений уже обеспечен таблицей `telegram_notifications`
  (idempotency по `(message_id, user_id)`) — повторных отправок нет.
- **Объём.** `message_tags` растёт на (число builtin/custom-тегов super_admin) ×
  (новые письма). super_admin один, тегов у него единицы — линейно и ничтожно на
  нашем масштабе (см. §Consequences «DB-storage растёт линейно»).
- **Стоимость JOIN** не меняется (та же одна вставка-select на письмо).

Риск-оценка: **низкий** при условии изоляции webhook-канала (выше). Без неё риск
был бы **средним** — межкомандная утечка персонального тега во внешнюю систему.
С изменённым webhook-предикатом: read-path (inbox по `t.user_id=ma.user_id`) и
webhook-path (по принадлежности тега команде) super_admin-теги не раскрывают;
открыт только намеренный TG-канал super_admin'а.

Стоимость: для 10 тегов × 3 правил пользователя — Postgres делает 1 indexed scan по `tag_rules` per tag (тривиально, объёмы ничтожны). Для 50 писем в пакете — 50 таких запросов. Управляемо при текущих объёмах.

`worker.save_message` НЕ должен fall-back'аться на «message без тегов» при ошибке apply-tags — иначе invariant ломается. Если что-то падает — let it crash (worker retry per next sync cycle).

### 6. Builtin-теги — post-login hook

Builtin-теги создаются один раз для каждого пользователя — при его **первом успешном login**. Реализуется как часть `auth.AuthService.login` (после успешного `argon2.verify`, перед возвратом session): вызов `TagsService.ensure_builtin_tags(user_id)`.

`ensure_builtin_tags`:
- Проверяет `SELECT id FROM tags WHERE user_id=:uid AND is_builtin=true LIMIT 1`. Если есть — return (идемпотент).
- Иначе — INSERT 4 builtin tags + tag_rules в одной транзакции. Список тегов и правил фиксирован в коде (`backend/app/tags/builtin.py`).
- Также вызывается из `auth.AuthService.complete_set_password` (set-password flow завершает первый «нормальный» login).

Альтернатива — data-миграция / on-create-user — отвергнута: лишние записи для never-logged-in пользователей (создаются админом, могут не залогиниться), плюс data-миграции противоречат принципу «миграции = только schema» (см. `03-data-model.md`). Post-login hook — простой, идемпотентный, всегда работает для активных пользователей.

Список builtin-тегов (формирование — детерминированное; реализация в `backend/app/tags/builtin.py`):

| Имя | Цвет | Rules |
| --- | --- | --- |
| `DPLA.PLA` | `#2563eb` (blue) | `subject_contains: DPLA`, `subject_contains: PLA`, `body_contains: DPLA`, `body_contains: PLA` |
| `Диспут` | `#dc2626` (red) | `subject_contains: Apple Inc`, `sender_exact: AppStoreNotices@apple.com` |
| `Отменить подписку` | `#f59e0b` (amber) | `body_contains: cancel`, `body_contains: subscription` |
| `Продление аккаунта` | `#16a34a` (green) | `body_contains: Your Distribution Certificate will no longer be valid in 30 days` |

`is_builtin=true` — пользователь не может удалить такой тег (см. API `DELETE /api/tags/{id}` — 400 на builtin). Но может **переименовать**, **изменить цвет** и **добавлять/удалять rules** к нему (согласовано: builtin — это только защита от удаления). Это упрощает развитие — пользователь может расширить покрытие правил без потери ID-стабильности.

### 7. "Apply to existing" — синхронно в API endpoint create_tag

При создании тега с `apply_to_existing=true` API endpoint `POST /api/tags` после INSERT тега и rules выполняет один SQL-запрос:

Актуальная форма (round-26 super_admin full-reach + round-27
границы/нормализация + round-29 body_html-ветка; canonical-текст —
`backend/app/tags/sql.py`):

```sql
INSERT INTO message_tags (message_id, tag_id)
SELECT m.id, :tag_id
FROM messages m
JOIN mail_accounts ma ON ma.id = m.mail_account_id
WHERE (
        CAST(:is_super_admin AS BOOLEAN)            -- round-26: super_admin → все письма
        OR ma.user_id = :user_id
        OR (CAST(:user_group_id AS BIGINT) IS NOT NULL AND ma.group_id = CAST(:user_group_id AS BIGINT))
    )
  AND ( /* match_mode 'any'/'all';
           subject/sender-предикат: norm(value) ~ '(^|[^[:alnum:]_])' || norm(escaped_pattern) || '([^[:alnum:]_]|$)';
           body_contains: norm(m.body_text) ~ boundary(...) OR norm(strip_tags(COALESCE(m.body_html,''))) ~ boundary(...) — §4.3;
           где norm(x) = regexp_replace(translate(x, chr(160), ' '), '\s+', ' ', 'g'),
               strip_tags(x) = regexp_replace(x, '<[^>]+>', ' ', 'g') — см. §4 */ )
ON CONFLICT (message_id, tag_id) DO NOTHING;
```

`sender_exact` остаётся `LOWER(m.from_addr) = LOWER(r.pattern)` без нормализации
(адрес — единый токен без пробелов). `body_contains` дополнительно матчит текст
из `m.body_html` со снятыми тегами (§4.3) — здесь `m.body_html` берётся из
колонки напрямую, без bind-параметра. Это **немедленно чинит существующие
письма** (главное обоснование выбора двойного матча против A10 — см. §4.3).

Стоимость на максимуме: ~150k messages per user (5 пользователей × ~30k мессаджей с учётом 30-day retention; см. `03-data-model.md` — суммарно 750k delà'd на 5 человек) × 1 indexed seq scan по `tag_rules`. Postgres выполняет это за **доли секунды на тёплом кэше; верхняя граница ~5 секунд** на современном дешёвом VM. Synchronous вызов в API — приемлемо.

round-29 добавляет на `body_contains`-плечо второй `regexp_replace`
(`strip_tags` по `body_html`, до ~1 MiB на строку) поверх `norm()`. При
super_admin full-scan apply-to-existing это удваивает per-row CPU тела
(strip_tags + norm на каждой проверке `body_contains`-rule). Runaway-guard
100k остаётся главной защитой; стоимость отмечена в TD-022 (обновлён).

Защитный лимит для синхронного path:
- Перед INSERT'ом — `SELECT count(*) FROM messages m JOIN mail_accounts ma ... WHERE ma.user_id=:uid` с уровнем `count > 100000` → возвращаем ошибку `tag_apply_too_many` (429 / 422; см. `04-api-contracts.md`) и предлагаем создать тег без `apply_to_existing`. Worker подхватит applying на новых письмах, а на старых — пользователь подождёт следующего ретенционного cleanup или вручную пересоздаст тег.
- HTTP timeout endpoint'а `POST /api/tags` — 30 секунд (общий backend default). Запрос свыше падает 504/500.

Альтернатива — фоновое применение через worker / Redis queue — рассматривалась (см. Alternatives), но отвергнута для первой итерации: synchronous простота на нашем масштабе достаточна, plus отдельный async-флоу = новый failure mode и UI status polling (overengineering под `~5 пользователей × ~100 ящиков`).

### 8. Inbox filter by tag

`GET /` и `GET /api/messages` принимают опциональный query-параметр `tag_id` (BIGINT). Backend дополнительно проверяет ownership (`tags.user_id == current_user.id`) и присоединяет JOIN `message_tags mt ON mt.message_id = m.id AND mt.tag_id = :tag_id`. Фильтр совмещаем с уже существующими `account_id`, `unread`, `cursor`.

Tag-фильтр НЕ ломает keyset-pagination (cursor по `(internal_date DESC, id DESC)` остаётся стабильным).

### 9. Ownership / Authorization

Все endpoints `/api/tags/...` и `/api/tags/{id}/rules/...` обязательно проверяют:
- `tag.user_id == request.state.session.user_id`. Чужой `tag_id` → 404 (не 403, чтобы не утечкой существование чужого).
- При `tag_id` в filter inbox — то же. Невалидный/чужой → 404.

`is_builtin=true` → запрет на DELETE (`400 cannot_delete_builtin_tag`). Изменение name/color/rules — разрешено.

## Consequences

### Положительные
- **Маленькая поверхность изменений.** Добавляется один service-модуль, три таблицы, ~7 endpoints. Существующая worker pipeline получает один SQL hook.
- **Без новых внешних зависимостей.** Работает на уже выбранном Postgres + FastAPI + APScheduler.
- **Производительность приемлемая.** При наших объёмах (≤ 750k писем максимум, ≤ 5 пользователей) ILIKE-сканы по `messages` и tag-checks при sync укладываются в текущий sync-cycle window (5 минут).
- **Полная транзакционность.** `INSERT message + apply_tags` — атомарно; нет orphan-записей без тегов. Retention cleanup чистит `message_tags` через CASCADE — нет orphan tag-links.
- **Backward-compatible.** Существующие endpoints `/`, `/api/messages`, `/messages/{id}` дополняются опциональными полями (`tags: [...]`); отсутствие `tag_id` в query не меняет поведение.
- **Безопасно.** Пользователь не вводит regex — паттерн экранируется, до движка доходит только литерал + наши анкеры → нет ReDoS (см. §4, §A2). Pattern параметризован → нет SQL-инъекции. Per-user изоляция через FK + JOIN.

### Отрицательные / компромиссы
- **`apply_to_existing` синхронен.** На объёмах сильно выше текущих (>100k messages per user) endpoint начнёт упираться в таймаут. Защищены лимитом `100000` (TD-011, ниже).
- **Стоимость нормализации.** `norm()` (`translate` + `regexp_replace`) применяется к `subject`/`body_text`/`from_*` на каждой проверке rule — лёгкий per-row CPU-оверхед, без индекса по нормализованному тексту. На нашем масштабе (≤750k писем, batch ≤50) приемлемо; при росте — рассмотреть материализованную колонку `body_text_normalized` или GIN-индекс (TD-022, ниже).
- **`body_contains` теперь по двум телам (round-29).** Фикс «тег не навешивается на письмо, в котором пользователь видит фразу глазами» (MIME plain≠html у Apple). Цена — второй `regexp_replace` (`strip_tags` по `body_html`) на каждой проверке `body_contains`-rule, сверху `norm()`. На нашем масштабе приемлемо; учтено в TD-022. **Чинит существующие письма немедленно** через apply-to-existing (в отличие от A10).
- **HTML-entities в `body_html` не декодируются (TD-024).** `strip_tags` снимает только `<…>`-теги; `&amp;`/`&#39;`/`&nbsp;` остаются буквальными. Если триггерная фраза содержит сущности — html-ветка её пропустит. Для текущего Apple-кейса фраза чистая. См. TD-024.
- **Нет AND / NOT / приоритетов между rules одного tag.** Только OR. Покрывает 95% запросов; для оставшихся — несколько отдельных тегов.
- **DB-storage растёт линейно.** ~50 байт на link `message_tags`. Для 750k messages × среднем 3 теги/message = 2.25M строк ≈ 110 MB. Приемлемо.
- **Нет UI для bulk-tagging вручную.** Tag прикладывается только через rules. Мануальный «tag this message» — отдельная функция, не в этом scope (если потребуется — отдельный ADR).
- **Builtin-теги создаются на login, а не на user_create.** Админ создаёт пользователя → тегов нет, пока тот не залогинится. Безопасно, но если admin захочет посмотреть «какие у user'а будут теги» через `/admin` — увидит пустой список. Документируем в UX.

### Tech debt items, привнесённые этим решением
- **TD-011** (новый): `apply_to_existing` синхронен, лимит 100k messages. Если масштаб вырастет — переделать на background worker job + UI status polling. См. `100-known-tech-debt.md`.
- ~~**TD-012**~~ — снят round-23: `*_contains` больше не использует ILIKE, `%`/`_` теперь литералы (экранируются). См. `100-known-tech-debt.md`.
- **TD-021** (round-23): семантика `*_contains` изменена substring→whole-word case-sensitive. Формализована этим ADR-update (§4). Считается закрытой.
- **TD-022** (round-27, обновлён round-29): `norm()`-нормализация — а теперь и `strip_tags` по `body_html` для `body_contains` — выполняются на каждой проверке rule без индекса по нормализованному тексту. На текущем масштабе приемлемо; при росте — материализованная колонка/индекс. См. `100-known-tech-debt.md`.
- **TD-024** (новый, round-29): `strip_tags(body_html)` не декодирует HTML-сущности (`&amp;`/`&#39;`/`&nbsp;`); паттерн, совпадающий с фразой, содержащей сущности, через html-ветку не сматчится. Для текущего Apple-кейса фраза чистая. См. `100-known-tech-debt.md`.

## История изменений §4/§5/§7 (matching-семантика)

Семантика матчинга эволюционировала пост-acceptance; ниже — хронология, чтобы
ADR оставался источником истины (код: `backend/app/tags/sql.py`):

- **round-23** — `*_contains` переведён с substring-ILIKE (case-insensitive) на
  whole-word **case-sensitive** через `~` + `\y`. Закрыл ложные срабатывания
  (`PLA` ⊄ «template»). См. TD-021.
- **round-24** — добавлен `tags.match_mode ∈ {any, all}` (миграция
  `20260521_015`). `any` (default) — OR по rules (обратная совместимость); `all`
  — AND (тег имеет ≥1 rule И ни один не fails).
- **round-25** — `sender_contains` матчит также display-name отправителя
  (`COALESCE(from_name,'')`), не только адрес. Нужно для App Store Connect
  (`no_reply@email.apple.com` / name «App Store Connect»).
- **round-26** — `APPLY_TAG_TO_EXISTING` получил флаг `:is_super_admin`,
  форсящий видимость в TRUE → super_admin применяет тег ко всем письмам.
- **round-27** — два фикса (этот ADR-update):
  1. **Границы слова `\y` → явные граничные классы** `(^|[^[:alnum:]_]) … ([^[:alnum:]_]|$)`.
     `\y` не матчил паттерны, обрамлённые пунктуацией (`…attention.`,
     `Congratulations!`), ломая теги «Реджект»/«Релиз» (особенно при `match_mode='all'`).
  2. **Whitespace-нормализация `norm()`** обеих сторон сравнения. Решение принято
     **на фактах из БД** (см. §4.2): тела `body_text` содержат `\n`, прогоны
     пробелов и U+00A0 внутри предложений; многословные паттерны иначе молча не
     матчатся. `norm(x) = regexp_replace(translate(x, chr(160), ' '), '\s+', ' ', 'g')`.
- **round-28** — видимость worker-hook (`APPLY_TAGS_TO_MESSAGE`) расширена веткой
  `OR u.role = 'super_admin'` (симметрично round-26), чтобы теги super_admin
  навешивались на чужие письма и срабатывали **TG**-уведомления. Scope изменения —
  `message_tags` + TG-канал. **Webhook-канал (ADR-0023) при этом изолирован от
  персональных тегов super_admin**: webhook-SQL (`FIND_ACTIVE_WEBHOOK_FOR_MESSAGE`
  EXISTS + `LIST_TAGS_FOR_TEAM`) перестал учитывать `u.role='super_admin'` и
  смотрит только на теги команды/владельца ящика
  (`u.group_id = ma.group_id OR u.id = ma.user_id`) — иначе персональный тег
  super_admin триггерил бы webhook чужой команды и утекал бы в её внешний
  payload. См. §5.1 «Webhook-вектор» и ADR-0023 §3.2.
- **round-29** — `body_contains` матчит **И `body_text`, И текст из `body_html`**
  (снятие тегов `strip_tags(x)=regexp_replace(x,'<[^>]+>',' ','g')`, далее `norm()`).
  Решение принято **на фактах из прода** (§4.3): Apple шлёт MIME с разным текстом
  в `text/plain` и `text/html`; UI рендерит `body_html`, а матчинг шёл только по
  `body_text` → тег не навешивался на письмо с видимой глазами фразой (письма id
  265/381, аккаунт `achilles.alex3611@aol.com`). Правки: `backend/app/tags/sql.py`
  (html-альтернатива в `body_contains` обоих запросов, обе ветки any/all),
  `worker/app/sync_cycle.py` (`_TagInputMessage.body_html` + заполнение из
  `fmsg.body_html`), `backend/app/tags/service.py` (`_MessageLike.body_html` + bind
  `:body_html`). Worker-hook получает `:body_html` как bind; apply-to-existing
  берёт `m.body_html` из колонки. Ограничения: HTML-сущности не декодируются
  (TD-024), удвоение CPU тела (TD-022 обновлён). Альтернатива A10 (строить
  `body_text` из `html2text(body_html)`) отвергнута — требует backfill и не чинит
  существующие письма.

## Alternatives considered

### A1. Глобальные теги (shared across users)
Отвергнуто. Создаёт coupling: один пользователь меняет правило → влияет на других. Сложнее authz: нужны permissions «кому видно/кому управлять». Per-user проще и достаточно.

### A2. Пользовательский regex как паттерн правила
Отвергнуто. ReDoS — реальная угроза (см. ATT&CK CWE-1333). Если бы паттерн
интерпретировался как regex — пользователь мог бы ввести структуру с
catastrophic backtracking. Поэтому паттерн всегда **экранируется**
(`regexp_replace` каждого метасимвола), и до движка доходит только литеральная
строка + наши фиксированные анкеры (граничные классы из §4.1 и `\s+` из норм.).
Инвариант сохранён и в round-23 (`~`+`\y`), и в round-27 (граничные классы +
`norm()`): структура regex от пользователя в движок не попадает, сложность
анкерованно-линейна. (Историческая заметка: до round-23 использовался ILIKE; он
тоже был линеен, но давал ложные substring-срабатывания — см. TD-021.)

### A3. AND/OR/NOT логика между rules + group_id в tag_rules
Отвергнуто на старт. UI становится «query-builder» — большой scope. Текущий запрос продукта решается множеством отдельных тегов (тег = одна категория). Если придёт явный фидбэк — новый ADR.

### A4. Async фоновое применение `apply_to_existing` через worker
Отвергнуто на старт. На текущем масштабе синхронное выполнение (≤5 секунд на верхней границе) приемлемо. Async добавляет:
- Redis queue или DB-table «pending tag-apply jobs».
- Worker handler для job'ов.
- UI с прогресс-баром или polling-эндпоинтом.
- Race conditions (что если пользователь удалит тег пока job в очереди).

При N=5 пользователей × ≤ 30k мессаджей это overengineering. См. TD-011 — пере-оценить при росте масштабов.

### A5. Data-миграция для builtin-тегов (создаются один раз для всех существующих + при INSERT user)
Отвергнуто. Миграции — schema-only по правилу `03-data-model.md`. Плюс плодит мёртвые записи: admin создал user → builtin-теги сразу есть → user никогда не залогинился → теги мёртвый груз. Post-login hook идемпотентен и elegant.

### A6. Tag-color picker свободного RGB
Отвергнуто как overengineering. Используем фиксированный набор из 8 цветов (chips/swatches в UI) — palette в `08-frontend.md` секция 5.1. Сужает выбор, но 8 цветов покрывают все типовые семантики (важное / срочное / спам / etc). Колонка `tags.color` хранит hex — backend дополнительно валидирует, что hex входит в whitelist из 8 значений палитры (см. `08-frontend.md` сек. 5.1). Это исключает inline-style в HTML и сохраняет CSP `style-src 'self'` без ослаблений.

### A7. Хранить tag-applied-flag в `messages` (boolean column)
Отвергнуто. Не масштабируется на N тегов; нарушает 1NF. Many-to-many через `message_tags` — каноническое решение.

### A8. Применять теги ТОЛЬКО на новых письмах (без `apply_to_existing`)
Отвергнуто. Прямой запрос продукта — чекбокс есть. Лишает пользователя возможности «нашёл паттерн → пометить весь существующий inbox».

### A9. Builtin-теги создаются `seed_super_admin` (как и сам admin)
Отвергнуто. `seed_super_admin` касается только super-admin'а. Builtin-теги нужны всем пользователям, и time-of-creation у обычных пользователей — `auth.complete_set_password` (первый login), а не seed.

### A10. Вместо матча по двум телам — всегда строить `body_text` из `html2text(body_html)` (один консистентный источник)
Отвергнуто (round-29). Идея: если `text/html`-часть есть, worker формирует
`body_text` как `html2text(body_html)` — тогда UI и matching консистентны по
**одному** полю, и §4.3 (двойной матч) не нужен. Минусы перевесили:
- **Требует backfill существующих писем.** Уже лежащие в БД письма имеют
  «старый» `body_text` (из `text/plain`). Их пришлось бы либо re-fetch'ить из
  IMAP (UID мог уже истечь, ретенция 30 дней), либо переписывать data-миграцией
  по сохранённому `body_html` — а data-миграции запрещены правилом
  schema-only (`03-data-model.md`).
- **Не чинит проблему немедленно.** Пользователь, у которого письмо-реджект уже
  в инбоксе, не получит тег, пока письмо не перезагрузят. Двойной матч (§4.3) +
  apply-to-existing чинят это **сразу** на существующих письмах.
- **Меняет данные, а не только matching.** Перестройка `body_text` затрагивает
  и то, что показывается в местах, где UI использует `body_text` (например,
  webhook payload — `body_text` усечён до 16384, см. `05-modules.md` §19),
  расширяя blast radius фикса далеко за пределы тегов.

Двойной матч локализован в `body_contains`-предикате двух запросов, ничего не
переписывает в данных и чинит существующие письма — поэтому выбран он.
