#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

load_env() {
  if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
    echo ".env is required" >&2
    exit 1
  fi

  set -a
  source "$PROJECT_ROOT/.env"
  set +a
}

require_tools() {
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required on the host" >&2
    exit 1
  fi
}

validate_required_env() {
  : "${APP_DOMAIN:?APP_DOMAIN is required}"
  : "${LETSENCRYPT_EMAIL:?LETSENCRYPT_EMAIL is required}"
  : "${BOT_TOKEN:?BOT_TOKEN is required}"
  : "${POSTGRES_DB:?POSTGRES_DB is required}"
  : "${POSTGRES_USER:?POSTGRES_USER is required}"
  : "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
  : "${DATABASE_URL:?DATABASE_URL is required}"
  : "${MINI_APP_URL:?MINI_APP_URL is required}"
  : "${ADMIN_TELEGRAM_IDS:?ADMIN_TELEGRAM_IDS is required}"
  : "${BROADCAST_RATE_PER_SECOND:?BROADCAST_RATE_PER_SECOND is required}"
}

validate_mini_app_url() {
  case "$MINI_APP_URL" in
    "https://${APP_DOMAIN}/"*) ;;
    *)
      echo "MINI_APP_URL must start with https://${APP_DOMAIN}/" >&2
      exit 1
      ;;
  esac
}

wait_for_app_health() {
  echo "docker compose ps"
  docker compose ps

  for _ in $(seq 1 30); do
    if docker compose exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done

  echo "app health check failed before certificate bootstrap" >&2
  docker compose ps >&2 || true
  exit 1
}

run_cert_bootstrap() {
  if [[ "${SKIP_STAGING:-0}" != "1" ]]; then
    LETSENCRYPT_STAGING=1 ./scripts/issue-letsencrypt-cert.sh
  fi

  ./scripts/issue-letsencrypt-cert.sh
}

final_checks() {
  curl -fsS "https://${APP_DOMAIN}/healthz" >/dev/null
  curl -fsSI "${MINI_APP_URL}" >/dev/null
}

load_env
require_tools
validate_required_env
validate_mini_app_url

: "${MINI_APP_INIT_DATA_MAX_AGE_SECONDS:=86400}"
: "${LOG_LEVEL:=INFO}"
export APP_DOMAIN LETSENCRYPT_EMAIL BOT_TOKEN POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD DATABASE_URL MINI_APP_URL ADMIN_TELEGRAM_IDS BROADCAST_RATE_PER_SECOND MINI_APP_INIT_DATA_MAX_AGE_SECONDS LOG_LEVEL

docker compose pull || true
docker compose up -d --build postgres app nginx
wait_for_app_health
run_cert_bootstrap
docker compose up -d --build
wait_for_app_health
final_checks

echo "Production bootstrap complete"
echo "Domain: https://${APP_DOMAIN}"
echo "Mini App: ${MINI_APP_URL}"
