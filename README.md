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
