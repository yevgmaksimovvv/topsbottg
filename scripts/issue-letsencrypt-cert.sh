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

validate_domain() {
  if [[ -z "${APP_DOMAIN:-}" ]]; then
    echo "APP_DOMAIN is required" >&2
    exit 1
  fi

  if [[ "$APP_DOMAIN" == localhost || "$APP_DOMAIN" == 127.* || "$APP_DOMAIN" == 0.0.0.0 || "$APP_DOMAIN" == 0.0.0.1 || "$APP_DOMAIN" == 0.0.1 || "$APP_DOMAIN" == http://* || "$APP_DOMAIN" == https://* || "$APP_DOMAIN" == *"/"* || "$APP_DOMAIN" == *":"* || "$APP_DOMAIN" =~ [[:space:]] ]]; then
    echo "APP_DOMAIN must be a real domain without scheme, port, path, or whitespace" >&2
    exit 1
  fi
}

bootstrap_self_signed_cert() {
  local tmp_dir live_dir fullchain privkey

  if [[ -f "/etc/letsencrypt/live/$APP_DOMAIN/fullchain.pem" && -f "/etc/letsencrypt/live/$APP_DOMAIN/privkey.pem" ]]; then
    return 0
  fi

  if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required on the host to generate a bootstrap certificate" >&2
    exit 1
  fi

  tmp_dir="$(mktemp -d)"

  live_dir="/etc/letsencrypt/live/$APP_DOMAIN"
  fullchain="$tmp_dir/fullchain.pem"
  privkey="$tmp_dir/privkey.pem"

  openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "$privkey" \
    -out "$fullchain" \
    -days 2 \
    -subj "/CN=$APP_DOMAIN" \
    -addext "subjectAltName=DNS:$APP_DOMAIN" >/dev/null 2>&1

  tar -C "$tmp_dir" -cf - fullchain.pem privkey.pem | docker compose --profile certbot run --rm --no-deps --entrypoint python certbot -c "
import pathlib
import sys
import tarfile

live_dir = pathlib.Path('$live_dir')
live_dir.mkdir(parents=True, exist_ok=True)
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
    archive.extractall('$live_dir')
(live_dir / 'fullchain.pem').chmod(0o644)
(live_dir / 'privkey.pem').chmod(0o600)
  "
  rm -rf "$tmp_dir"
}

start_nginx() {
  docker compose up -d --force-recreate --no-deps nginx
  for _ in $(seq 1 20); do
    if curl -fsS http://127.0.0.1/ >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "nginx did not become ready on 127.0.0.1:80" >&2
  exit 1
}

create_acme_probe() {
  local acme_cmd
  PROBE_NAME="probe-${APP_DOMAIN//[^a-zA-Z0-9]/-}-$(date +%s)-$(openssl rand -hex 4).txt"
  PROBE_PAYLOAD="$(openssl rand -hex 16)"
  acme_cmd=$(cat <<'EOF'
set -eu
mkdir -p /var/www/certbot/.well-known/acme-challenge
printf '%s' "$PROBE_PAYLOAD" > "/var/www/certbot/.well-known/acme-challenge/$PROBE_NAME"
EOF
)

  docker compose --profile certbot run --rm --no-deps \
    -e PROBE_NAME="$PROBE_NAME" \
    -e PROBE_PAYLOAD="$PROBE_PAYLOAD" \
    --entrypoint sh certbot -c "$acme_cmd"
}

wait_for_acme_probe() {
  local probe_url response
  probe_url="http://${APP_DOMAIN}/.well-known/acme-challenge/${PROBE_NAME}"

  for _ in $(seq 1 30); do
    if response="$(curl -fsS "$probe_url")"; then
      if [[ "$response" == "$PROBE_PAYLOAD" ]]; then
        return 0
      fi
    fi
    sleep 2
  done

  echo "ACME webroot is not reachable for APP_DOMAIN=$APP_DOMAIN" >&2
  echo "Check DNS A record, public port 80, router/firewall, and nginx /.well-known/acme-challenge/ mapping." >&2
  if command -v dig >/dev/null 2>&1; then
    echo "dig +short \"$APP_DOMAIN\":" >&2
    dig +short "$APP_DOMAIN" >&2 || true
  fi
  if command -v curl >/dev/null 2>&1; then
    echo "curl -4 -fsS ifconfig.me:" >&2
    curl -4 -fsS ifconfig.me >&2 || true
    echo >&2
  fi
  exit 1
}

run_certbot() {
  local cert_name certbot_args

  if [[ "${LETSENCRYPT_STAGING:-0}" == "1" ]]; then
    cert_name="${APP_DOMAIN}-staging"
    certbot_args=(
      certonly
      --webroot
      -w /var/www/certbot
      -d "$APP_DOMAIN"
      --email "$LETSENCRYPT_EMAIL"
      --agree-tos
      --no-eff-email
      --non-interactive
      --staging
      --force-renewal
      --cert-name "$cert_name"
    )
  else
    cert_name="$APP_DOMAIN"
    certbot_args=(
      certonly
      --webroot
      -w /var/www/certbot
      -d "$APP_DOMAIN"
      --email "$LETSENCRYPT_EMAIL"
      --agree-tos
      --no-eff-email
      --non-interactive
      --keep-until-expiring
      --cert-name "$cert_name"
    )
  fi

  docker compose --profile certbot run --rm certbot "${certbot_args[@]}"
}

reload_nginx() {
  if ! docker compose exec -T nginx nginx -s reload; then
    docker compose up -d --force-recreate --no-deps nginx
  fi
}

final_https_check() {
  if curl -fsS "https://${APP_DOMAIN}/healthz" >/dev/null; then
    return 0
  fi

  if curl -fsSI "${MINI_APP_URL}" >/dev/null; then
    return 0
  fi

  echo "Warning: HTTPS final check failed for APP_DOMAIN=$APP_DOMAIN or MINI_APP_URL=$MINI_APP_URL" >&2
}

load_env
validate_domain

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required on the host" >&2
  exit 1
fi

: "${LETSENCRYPT_EMAIL:?LETSENCRYPT_EMAIL is required}"
export APP_DOMAIN LETSENCRYPT_EMAIL

: "${POSTGRES_DB:=topsbottg}"
: "${POSTGRES_USER:=topsbottg_app}"
: "${POSTGRES_PASSWORD:=placeholder}"
: "${DATABASE_URL:=postgresql+asyncpg://placeholder:placeholder@postgres:5432/topsbottg}"
: "${BOT_TOKEN:=placeholder}"
: "${ADMIN_TELEGRAM_IDS:=0}"
: "${MINI_APP_URL:=https://${APP_DOMAIN}/miniapp/}"
: "${BROADCAST_RATE_PER_SECOND:=1}"
: "${MINI_APP_INIT_DATA_MAX_AGE_SECONDS:=86400}"
: "${LOG_LEVEL:=INFO}"
export POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD DATABASE_URL BOT_TOKEN ADMIN_TELEGRAM_IDS MINI_APP_URL BROADCAST_RATE_PER_SECOND MINI_APP_INIT_DATA_MAX_AGE_SECONDS LOG_LEVEL

bootstrap_self_signed_cert
start_nginx
create_acme_probe
wait_for_acme_probe
run_certbot
reload_nginx
final_https_check
