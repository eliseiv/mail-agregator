# ADR-0032 — Выделенный prod-сервер + host-level TLS (certbot на хосте, не в контейнере)

- **Статус:** accepted
- **Дата:** 2026-07-01
- **Контекст-документы:** `07-deployment.md` (sec. 1, 2, 6, 9, 15), `SERVER-SETUP.md`, `01-architecture.md` (deployment diagram), `docker-compose.yml`, `.github/workflows/deploy.yml`

## Context

До 2026-07-01 прод mail-agregator работал на **общем** сервере `132.243.113.117`, где сосуществовал с чужими сервисами (в т.ч. чужой Traefik — см. инцидент в operator-memory). Это создавало риски: конкуренция за порты 80/443, непредсказуемая маршрутизация TLS, отсутствие контроля над хостом.

Параллельно в документации накопился **дрейф** относительно фактической TLS-модели. `07-deployment.md` (§2/§6) и `SERVER-SETUP.md` (Part B) описывали TLS как отдельный контейнер `certbot/certbot` с named-volume `mas_certbot_certs`/`mas_certbot_webroot` и renewal-циклом внутри compose. В реальности `docker-compose.yml` **никогда** не содержал сервиса `certbot`: единственный prod-only сервис — `nginx`, который получает `/etc/letsencrypt` и `/var/www/certbot` **bind-mount'ом с хоста** (RO). Сертификаты обслуживает системный `certbot` (пакет) + `certbot.timer`.

Также в docs фигурировали идеализированные имена GitHub Secrets и путей (`DEPLOY_KEY`, пользователь `deploy`, путь `/opt/mail-aggregator`), не совпадающие с реальными (`DEPLOY_KEY_B64`, `root`, `/opt/mail-agregator`).

## Decision

1. **Выделенный сервер.** Prod переезжает на выделенный Hetzner-хост `49.12.189.77` (Ubuntu 26.04), полностью подконтрольный команде. Домен `postapp.store` (A-запись у reg.ru) перенацелен на новый IP.

2. **TLS — host-level certbot (закрепляем как норму, не контейнер).** Сертификаты Let's Encrypt выпускает и обновляет системный `certbot` на хосте:
   - каталог `/etc/letsencrypt` (cert + account keys) и webroot `/var/www/certbot` живут на **хосте**;
   - контейнер `nginx` монтирует оба каталога **bind-mount'ом RO** (`docker-compose.yml`, сервис `nginx`);
   - renewal — `certbot.timer` (systemd), метод **webroot** (nginx постоянно слушает `:80`), без остановки nginx;
   - после renewal `nginx -s reload` выполняется renewal deploy-hook'ом (`/etc/letsencrypt/renewal-hooks/deploy/`), с еженедельным host-cron как fallback;
   - в docker-compose **нет** сервиса `certbot` и **нет** named-volume `mas_certbot_certs`/`mas_certbot_webroot`.

3. **GitHub Secrets — фактические имена.** Деплой (`.github/workflows/deploy.yml`) использует ровно: `DEPLOY_HOST` (`49.12.189.77`), `DEPLOY_USER` (`root`), `DEPLOY_KEY_B64` (**base64** приватного ключа, декодируется `base64 -d`), `DEPLOY_PATH` (`/opt/mail-agregator`). При переезде изменён **только** `DEPLOY_HOST`.

4. **GHCR-образы публичные.** `docker compose pull` тянет образы анонимно; `docker login` на сервере не требуется (файла `~/.docker/config.json` нет).

5. **Документация приводится в соответствие с `docker-compose.yml`** (источник истины по compose-топологии) — правки §2/§6/§9 `07-deployment.md`, Part A/B/C/F/G/H `SERVER-SETUP.md`, deployment-диаграмма `01-architecture.md`.

6. **Runbook переезда** фиксируется как `07-deployment.md` секция 15: провижининг, останов записи на старом хосте (anti split-brain), `pg_dump`/`pg_restore`, MinIO volume tar, перенос `.env` вербатим (тот же `MAIL_ENCRYPTION_KEY`) и `/etc/letsencrypt`, cutover (смена `DEPLOY_HOST` + A-записи, полный останов старого прода).

## Consequences

**Плюсы:**
- Полный контроль над хостом; нет конкуренции за 80/443 с чужими сервисами.
- Документация соответствует реальному `docker-compose.yml` — устранён источник противоречий (нет фантомного certbot-контейнера/volume).
- Меньше движущихся частей в compose: renewal — стандартный `certbot.timer`, наблюдаемый через `systemctl`/`journalctl`.
- Данные перенесены 1:1, `MAIL_ENCRYPTION_KEY` сохранён — зашифрованные пароли ящиков и OAuth refresh-токены остаются расшифровываемыми.

**Минусы / риски:**
- TLS теперь зависит от хостовой конфигурации (`certbot.timer` + deploy-hook), а не самодостаточен внутри compose — при провижининге нового хоста этот шаг нужно не забыть (закрыто runbook'ом sec. 15 и Part A.3/B.2/G.1).
- Деплой под `root` слабее модели с dedicated `deploy`-пользователем — компромисс осознанный; в docs отмечено как рекомендуемое ужесточение.
- `/etc/letsencrypt` не входит в docker volume backup — при миграции требуется отдельный tar-перенос (описано в sec. 15.4 и F.4).

## Alternatives considered

- **certbot-контейнер + named volume (как ошибочно описывалось в docs).** Отклонено: это не соответствует фактическому `docker-compose.yml`; вводить контейнер certbot ради «симметрии» — усложнение без выгоды, плюс хрупкая межконтейнерная сигнализация reload nginx. Приводим docs к реальности вместо изменения кода.
- **Traefik/Caddy с автоматическим ACME.** Отклонено: избыточно для single-host монолита; nginx уже настроен и покрывает все требования (§6). Автоматический reload не окупает миграцию reverse-proxy.
- **Остаться на общем сервере.** Отклонено: инцидент с чужим Traefik и отсутствие контроля над хостом.
- **Хранить raw-PEM в `DEPLOY_KEY`.** Отклонено (и не соответствует workflow): GitHub Secrets UI искажает многострочный PEM; base64 в `DEPLOY_KEY_B64` — надёжный single-line транспорт.
