# topsbottg
Telegram-сервис для рассылки уведомлений о выплатах, сбора платежных данных и ручного закрытия выплат после внешнего перевода.

![Python](https://img.shields.io/badge/Python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-API-009688)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED)
![License](https://img.shields.io/badge/License-MIT-green)

## Что делает сервис

- создает выплаты и список получателей;
- отправляет уведомления через Telegram Bot API;
- собирает или подтверждает платежный профиль пользователя;
- дает администратору Mini App для контроля, CSV и ручного закрытия выплат.

Банковские переводы сервис не выполняет: статус выплаты закрывается администратором вручную после внешней оплаты.

## Скриншоты

> TODO: добавить скриншоты Mini App после финального UI.

## Архитектура

```text
Telegram users/admins
        │
        ▼
topsbottg app ──► Telegram Bot API
  ├─ Bot handlers
  ├─ FastAPI admin API
  └─ Background worker
        │
        ▼
PostgreSQL
Admin Mini App ──► Nginx ──► FastAPI
```

- app запускает bot, API и worker в одном Python process;
- Nginx публикует 80/443, отдает Mini App и проксирует API;
- PostgreSQL хранит пользователей, выплаты, получателей, профили и audit log;
- Redis, Celery, RabbitMQ и Google Sheets не используются.

## Стек

| Layer | Technology |
|---|---|
| Bot | aiogram 3 |
| API | FastAPI |
| DB | PostgreSQL 16, SQLAlchemy 2, Alembic |
| Frontend | Static Telegram Mini App |
| Runtime | Docker Compose, Nginx |
| Package manager | uv |

## Быстрый запуск локально

```bash
cp .env.example .env
chmod +x scripts/generate-local-certs.sh
./scripts/generate-local-certs.sh
docker compose up -d --build
```

Открыть:

- `https://localhost/miniapp/`
- `https://localhost/healthz`

- `APP_DOMAIN=localhost`;
- `MINI_APP_URL=https://localhost/miniapp/`;
- `scripts/generate-local-certs.sh` кладет bootstrap cert в volume `certbot`;
- для production нужен реальный домен с доверенным HTTPS, а не `localhost`.

## Production

Одна команда на production-сервере:

```bash
chmod +x scripts/prod-bootstrap.sh scripts/issue-letsencrypt-cert.sh
./scripts/prod-bootstrap.sh
```

Что нужно до запуска:

- DNS `A` record для `APP_DOMAIN` должен указывать на публичный IPv4 сервера;
- на сервере должны быть открыты порты `80` и `443`;
- HTTP-01 validation требует публичный порт `80`;
- если ACME preflight не проходит, скрипт остановится до реального Let's Encrypt attempt.

Если нужно отдельно проверить только issuance flow:

```bash
LETSENCRYPT_STAGING=1 ./scripts/issue-letsencrypt-cert.sh
```

### Renew

```bash
chmod +x scripts/renew-letsencrypt-cert.sh
./scripts/renew-letsencrypt-cert.sh
```

Пример cron или systemd timer можно сделать поверх этого скрипта.

### Контур Nginx

- реальный runtime-конфиг лежит в `nginx/templates/default.conf.template`;
- официальный nginx image подхватывает template через envsubst на старте;
- `docker-compose.yml` передает `APP_DOMAIN` в nginx;
- `app:8000` наружу не публикуется.
- `/healthz` публичный и не требует `initData`.

## Переменные окружения

| Variable | Required | Description |
|---|---:|---|
| `APP_DOMAIN` | yes | Домен сервиса |
| `LETSENCRYPT_EMAIL` | no | Email для Let’s Encrypt issuance script |
| `BOT_TOKEN` | yes | Telegram bot token |
| `ADMIN_TELEGRAM_IDS` | yes | Telegram ID администраторов через запятую |
| `DATABASE_URL` | yes | Async SQLAlchemy URL |
| `POSTGRES_DB` | yes | PostgreSQL database |
| `POSTGRES_USER` | yes | PostgreSQL user |
| `POSTGRES_PASSWORD` | yes | PostgreSQL password |
| `MINI_APP_URL` | yes | Public Mini App URL |
| `BROADCAST_RATE_PER_SECOND` | yes | Лимит отправки сообщений |
| `MINI_APP_INIT_DATA_MAX_AGE_SECONDS` | yes | Max age Telegram Mini App initData |
| `LOG_LEVEL` | no | Logging level |
| `POSTGRESQL_MAX_CONNECTIONS` | no | PostgreSQL connection tuning |

- `.env` не коммитить;
- `DATABASE_URL` должен соответствовать `POSTGRES_*`;
- `BOT_TOKEN` и `POSTGRES_PASSWORD` не должны попадать в логи.

## Команды

```bash
# checks
uv run ruff check .
uv run pytest -q
uv run mypy .

# migrations
uv run alembic upgrade head

# docker
docker compose config
docker compose up -d --build
docker compose logs -f app
docker compose logs -f nginx

# backup
./scripts/backup.sh
```

## Безопасность

- backend проверяет Telegram Mini App initData;
- admin API доступен только ID из `ADMIN_TELEGRAM_IDS`;
- платежные данные хранятся без CVV, срока действия карты и фото карты;
- audit log не должен содержать платежные реквизиты;
- банковская оплата выполняется вне сервиса.

## Автоматический production deploy

После merge PR каждый push в `main` проходит workflow `CI`. Job `Deploy production` запускается только после успешных проверок, использует тот же SHA и подключается к VPS как `deploy` в `/home/deploy/apps/topsbottg`. В GitHub создайте Environment `production` и добавьте настройки именно в него:

- Secret `DEPLOY_SSH_PRIVATE_KEY` — отдельный приватный OpenSSH ключ CI.
- Variables `DEPLOY_HOST`, `DEPLOY_PORT`, `DEPLOY_USER` — адрес VPS, SSH порт и пользователь `deploy`.
- Variable `DEPLOY_KNOWN_HOSTS` — доверенная строка ключа хоста из `known_hosts`. Для нестандартного порта формат записи: `[host]:port ssh-ed25519 AAAA...`.

Однократная настройка с Windows PowerShell (укажите реальный адрес VPS и порт):

```powershell
ssh-keygen -t ed25519 -C "topsbottg-github-actions-production" -f "$env:USERPROFILE\.ssh\topsbottg-production" -N ""
Get-Content "$env:USERPROFILE\.ssh\topsbottg-production.pub" | ssh deploy@VPS "umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys"
ssh-keygen -lf "$env:USERPROFILE\.ssh\topsbottg-production.pub"
ssh-keyscan -p 22 -t ed25519 VPS
```

Сверьте отпечаток `ssh-keyscan` с доверенным отпечатком VPS (на сервере: `sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`) и сохраните выданную строку в `DEPLOY_KNOWN_HOSTS`. Не добавляйте приватный файл `topsbottg-production` в репозиторий. В **Settings → Environments → New environment** создайте `production` и задайте Secret/Variables выше.

Workflow не запускает параллельные deploy и откажется выкладывать SHA, если `origin/main` успел продвинуться. VPS должен иметь Git, Docker Compose plugin, `curl`, GNU `timeout`, доступ к GitHub по HTTPS и production `.env`. Скрипт проверяет БД, создаёт SQL backup до изменения кода/миграций, применяет только fast-forward, ждёт здоровья app, проверяет Alembic, Mini App и заголовки JavaScript, затем обновляет только Compose-сервис `nginx`. Успешный SHA хранится в `.deployed-sha`.

При ошибке workflow покажет последние логи `app` и `nginx`. Сначала сохраните логи и выясните причину; не запускайте автоматический `alembic downgrade`. Для восстановления проверьте `.deployed-sha`, рабочее дерево и файлы `backups/topsbottg-*.sql`; восстановление БД выполняйте только после согласования с оператором БД и выбора подходящего backup. Исправленную версию поставляйте новым PR в `main`. Возврат приложения к прежней версии допустим отдельным forward commit после проверки совместимости схемы. Автоматического отката миграций или восстановления БД нет.

## Operational notes

- app делает миграции на старте контейнера;
- worker работает внутри app process;
- `app:8000` не публикуется наружу;
- PostgreSQL и Adminer не должны быть публичными;
- `/healthz` - readiness endpoint; он проверяет PostgreSQL и Telegram Bot API и может вернуть `503` при outage;
- startup приложения падает, если PostgreSQL или Telegram Bot API недоступны;
- подробный контракт переходов покрыт тестами в `tests/`.

## Структура проекта

```text
src/topsbottg/      bot, API, worker, services, models
frontend/           static Mini App
migrations/         Alembic migrations
nginx/              reverse proxy config and template
scripts/            cert bootstrap, renew and backup scripts
tests/              contract and regression tests
```

## License

MIT
