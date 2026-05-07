# 100. Known Tech Debt

Реестр осознанных компромиссов и пунктов, отложенных на будущие итерации. Каждый item имеет ID `TD-NNN`, контекст, влияние и приемлемость.

| ID | Краткое название | Контекст | Impact | Severity | Когда адресовать |
| --- | --- | --- | --- | --- | --- |
| **TD-003** | Нет Prometheus-метрик | На первой итерации только structlog JSON в stdout. Подсчёт SLI — grep по логам. | Низкий: scope маленький. | low | Когда понадобится дашборд / алертинг. |
| **TD-004** | Нет orphan-scan для MinIO | Cleanup по retention или delete-cascade — best-effort; возможны "осиротевшие" объекты в MinIO. | Низкий: накопление < 1% данных в год. | low | Когда disk usage начнёт расти быстрее ожидаемого. |
| **TD-005** | UI отправки не поддерживает аттачи | Таблица `sent_attachments` зарезервирована, но в compose-форме нет upload-поля. | Средний: пользователи могут хотеть прикреплять файлы. | medium | Sprint 2+ (если будет запрос пользователей). |
| **TD-006** | Single worker — single point of failure | При падении worker'а sync останавливается. Restart=always покрывает crash, но не bug-loop. | Низкий: scope; ручной recovery приемлем. | low | Если масштабирование потребует двух+ worker — отдельный ADR с координацией задач. |
| **TD-007** | Нет автоматического failover Postgres | Single-host. При падении хоста — manual recovery из backup. | Средний: RTO ~ часы. | medium | Когда требования к доступности повысятся. |
| **TD-008** | Нет CAPTCHA на login | rate-limit + lockout есть, но distributed brute-force с пула IP теоретически возможен. | Низкий: закрытый сервис, фиксированный набор пользователей. | low | Если станет публичным или появится реальный риск. |
| **TD-010** | Нет сохранения IMAP-флагов (read/seen back-sync) | UI mark-read локальный, не синхронизируется обратно в IMAP. | Средний: пользователи могут ожидать sync с другими клиентами. | medium | Если придёт явный фидбэк. |
| **TD-011** | `apply_to_existing` для тегов — синхронный, лимит 100k messages | См. ADR-0017 §7. На текущем масштабе (≤30k messages/user) синхронное применение приемлемо (<5 сек), но при росте упрётся в HTTP-таймаут 30 сек. Лимит 100k защитит от висящих запросов. | Низкий на старте, средний при росте. | low | При росте до >50k messages у любого user'а — переделать на background worker job + UI status polling. |
| **TD-012** | Pattern в tag_rules не escape'ит `%`/`_` (ILIKE wildcards) | См. ADR-0017 §4. Пользователь может ввести `%` или `_` и получить unexpected match (намеренный фактический wildcard). На UI документируется как фича. | Низкий: обычный пользователь не использует эти символы; security impact отсутствует. | low | Если пользователи начнут жаловаться — добавить escape (`pattern ESCAPE '\\'` + `replace(pattern, '%', '\\%')`) в `TagsService.add_rule`. |
| **TD-013** | Нет push-уведомлений в Telegram о новых письмах | См. ADR-0018. Бот — только launcher (открывает обычную login-страницу), без линковки `telegram_user_id ↔ user_id` невозможно слать пуши конкретному user'у. | Низкий: пользователь явно отверг линковку на текущем этапе. | low | Когда пользователь захочет пуши — отдельный ADR (схема линковки + opt-in flow в settings UI + worker-job на отправку). |
| **TD-014** | Имя env-var для bot-токена расходится между кодом и docs (`BOT_TOKEN` vs `TELEGRAM_BOT_TOKEN`) | Operator deploy уже использует `BOT_TOKEN` (его кладёт BotFather copy-paste UX), prod `.env` тоже на `BOT_TOKEN`. Code (`shared/config.py`) использует `BOT_TOKEN`. ADR-0018, `docs/05-modules.md` §18, `docs/06-security.md` §1.8, `docs/07-deployment.md` §4 ссылаются на `TELEGRAM_BOT_TOKEN`. | Низкий: redact-list покрывает оба имени, функционально работает. Confusion для нового разработчика. | low | Architect должен синхронизировать docs на `BOT_TOKEN` (или, если нужно "TELEGRAM_" prefix, перепровизионить prod `.env` и обновить код). |

## Закрытые / отозванные пункты

| ID | Причина |
| --- | --- |
| ~~TD-001~~ | Закрыт. Смена пароля супер-админа выполняется через `.env` + restart `api`/`worker` (см. `07-deployment.md` sec. 11.1); `seed_super_admin` upsert'ит `users.password_hash`. UI для одного супер-админа сознательно не предусмотрен. |
| ~~TD-002~~ | Закрыт. Использован non-root service account: `MINIO_APP_*` создаются init-контейнером `minio-bootstrap` с политикой только на bucket `mail-attachments`. См. `07-deployment.md` sec. 12, `06-security.md` sec. 12. |
| ~~TD-009~~ | Отозван — это design-choice, а не tech-debt. `sent_messages` сознательно не подпадают под 30-дневную ретенцию (ADR-0011). |

Регистр обновляется при каждом ADR/тех-решении, которое сознательно откладывает работу. Закрытые items не удаляются физически, а помещаются в раздел "Закрытые / отозванные".
