#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
  echo ".env is required" >&2
  exit 1
fi

set -a
source "$PROJECT_ROOT/.env"
set +a

: "${POSTGRES_DB:=topsbottg}"
: "${POSTGRES_USER:=topsbottg_app}"
: "${POSTGRES_PASSWORD:=placeholder}"
: "${DATABASE_URL:=postgresql+asyncpg://placeholder:placeholder@postgres:5432/topsbottg}"
: "${BOT_TOKEN:=placeholder}"
: "${ADMIN_TELEGRAM_IDS:=0}"
: "${MINI_APP_URL:=https://${APP_DOMAIN}/miniapp/}"
: "${BROADCAST_RATE_PER_SECOND:=1}"
: "${MINI_APP_INIT_DATA_MAX_AGE_SECONDS:=86400}"
export POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD DATABASE_URL BOT_TOKEN ADMIN_TELEGRAM_IDS MINI_APP_URL BROADCAST_RATE_PER_SECOND MINI_APP_INIT_DATA_MAX_AGE_SECONDS

docker compose up -d --no-deps nginx
docker compose --profile certbot run --rm certbot renew --webroot -w /var/www/certbot
docker compose exec nginx nginx -s reload
