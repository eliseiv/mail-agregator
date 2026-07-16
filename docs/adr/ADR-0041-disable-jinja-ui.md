# ADR-0041 — Отключение собственного Jinja-UI агрегатора (headless-режим)

Статус: `accepted` — **сужен [ADR-0043](./ADR-0043-strip-to-connector-push-to-crm.md)** (2026-07-10) и финализирован [ADR-0044](./ADR-0044-decommission-runbook.md); **реализован на проде 2026-07-15** (Фаза A3) · Дата: 2026-07-09

> **⚠️ Сужен ADR-0043 / финализирован ADR-0044 — перечень §Decision УСТАРЕЛ. Читатель этого ADR в отрыве не должен принимать его за текущий.**
>
> **Выполнено (2026-07-15, Фаза A3 ADR-0044):** HTML-роутеры (`auth`/`messages`/`accounts`/`tags`/`admin`/`groups` UI + `send` form-fallback), `StaticFiles /static`, `templates/`+`static/`, friendly-redirect `→ /login` — **удалены**; все HTML-URL отдают `404`.
>
> **Отличие от §Decision этого ADR:** здесь перечень остающихся роутеров — `health_router` + `external_router` + **`telegram_router`**. `telegram_router` **тоже снят** ADR-0043 (Telegram целиком переехал в CRM). Фактически в `main.py::create_app` смонтированы **только два** роутера — `external_router` и `health_router` (`backend/app/main.py:99-100`).
>
> **Q-0041-1 решён:** session-`oauth_router` снят ([ADR-0044](./ADR-0044-decommission-runbook.md) §7); consent-flow восстановлен headless-роутами в `external/router.py` ([ADR-0045](./ADR-0045-external-outlook-oauth-headless.md)). CRUD `webhooks`/`forwarding` — не в `/api/external/*`, а **сняты целиком** (подсистемы демонтированы).

Extends [ADR-0001](./ADR-0001-tech-stack.md) (Jinja2 в стеке) / [ADR-0015](./ADR-0015-no-js-fallback.md) / [ADR-0021](./ADR-0021-russian-localization.md). Парный CRM `ADR-038` (headless-интеграция).

## Context

После headless-интеграции (CRM `ADR-038`, агрегатор ADR-0039/0040) единственный UI — CRM. Собственный веб-интерфейс агрегатора (`postapp.store`, Jinja-шаблоны + `static/`) пользователем **не используется** (подтверждено). Его поддержка при изменении модели тегов (ADR-0040) и владельца ящика (ADR-0039) — лишняя работа и поверхность атаки. Агрегатор превращается в headless mail-connector: движок IMAP/SMTP/тегов/доставки + интеграционный API, без человеко-обращённого HTML.

## Decision

В `backend/app/main.py::create_app` (`main.py:155-174`) HTML/UI-поверхность демонтируется, остаётся операционная + интеграционная:

**Остаются (нормативный список):**
- `health_router` — `/healthz`, `/readyz` (liveness/readiness, деплой/оркестрация).
- `external_router` — `/api/external/*` (pull ADR-0029, reply ADR-0035, teams/mailboxes/filters ADR-0037, write mailboxes+tags ADR-0039). Единственный контур для CRM.
- `telegram_router` — `/api/telegram/*` (persistent SSO ADR-0022 + webhook'и push-ботов ADR-0027). Telegram-боты/SSO используются.

**Демонтируются:**
- HTML-роутеры и человеко-обращённые страницы: `auth_router` (логин/сессия Jinja), `messages_router`, `accounts_router`, `tags_router`, `admin_router`, `groups_router`, `send_router` (в части UI-form-fallback) — как поверхность собственного UI.
- `app.mount("/static", StaticFiles(...))` (`main.py:155`), каталог `backend/app/static/` (css/js) и `backend/app/templates/` (Jinja) — удаляются из образа. Проверка `_dir_is_empty(templates_dir)` в prod (`main.py:140-143`) снимается.
- Friendly-redirect `NotAuthenticatedError → /login` (`main.py:178-182`) — удаляется (HTML-страниц нет; все ответы `/api/*` → `401 JSON`).

**Worker (`worker/app/*`) не затрагивается** — движок синхронизации/доставки остаётся полностью.

### Q-0041-1 (решается в дизайне S5, до реализации): судьба CRUD webhooks/forwarding/oauth-consent

Часть роутеров обслуживает не только Jinja-UI, но и реальные интеграционные/фоновые сценарии — их демонтаж требует явного решения в S5:
- `oauth_router` (`/oauth/*`, Outlook consent ADR-0025) — **consent-флоу Outlook человеко-обращён** (сайт+OctoBrowser). Если Outlook-ящики используются, роутер (или его consent-часть) **остаётся** либо переносится под интеграционный контур. Нельзя удалить вместе с Jinja без потери Outlook-онбординга.
- `webhooks_router` / `forwarding_router` — CRUD конфигов webhooks (ADR-0023) и forwarding (ADR-0034). Сегодня управляются из собственного UI. Если ими должен управлять CRM — нужны соответствующие `/api/external/*`-эндпоинты (отдельный будущий ADR); если конфигурация ручная/через БД — роутеры можно демонтировать. Диспетчеры (worker) в любом случае остаются.

S5 обязан зафиксировать точный итоговый список include_router до реализации; данный ADR фиксирует **направление** (headless, минимальная человеко-обращённая поверхность) и безусловный минимум (health/external/telegram).

## Consequences

- Меньше кода/поверхности атаки; изменения модели тегов/владельца (ADR-0039/0040) не нужно отражать в собственном UI.
- Все `/api/*`-ответы агрегатора — JSON (нет redirect на `/login`).
- Jinja2 остаётся в зависимостях лишь если worker/письмо-MIME его используют (проверить в S5); иначе — кандидат на удаление из `pyproject.toml` (не обязательно).
- Реализация — **строго последним спринтом**, после подтверждённо работающей CRM-страницы, чтобы откат интеграции не остался без всякого UI.

## Alternatives considered

- **Оставить Jinja-UI как «резервный».** Отклонён: не используется, требует синхронизации с ADR-0039/0040, дублирует CRM.
- **Удалить UI сразу вместе с функциональными ADR (в S1).** Отклонён: рискованно — до готовности CRM-страницы система осталась бы без UI; поэтому S5, отдельным ADR.
