#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  source "$PROJECT_ROOT/.env"
  set +a
fi

: "${APP_DOMAIN:=localhost}"
export APP_DOMAIN

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required on the host to generate a bootstrap certificate" >&2
  exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

LIVE_DIR="/etc/letsencrypt/live/$APP_DOMAIN"
FULLCHAIN="$LIVE_DIR/fullchain.pem"
PRIVKEY="$LIVE_DIR/privkey.pem"

openssl req -x509 -nodes -newkey rsa:2048 \
  -keyout "$TMP_DIR/privkey.pem" \
  -out "$TMP_DIR/fullchain.pem" \
  -days 365 \
  -subj "/CN=$APP_DOMAIN" \
  -addext "subjectAltName=DNS:$APP_DOMAIN" >/dev/null 2>&1

tar -C "$TMP_DIR" -cf - fullchain.pem privkey.pem | docker compose --profile certbot run --rm --no-deps --entrypoint python certbot -c "
import pathlib
import sys
import tarfile

live_dir = pathlib.Path('$LIVE_DIR')
live_dir.mkdir(parents=True, exist_ok=True)
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
    archive.extractall('$LIVE_DIR')
(live_dir / 'fullchain.pem').chmod(0o644)
(live_dir / 'privkey.pem').chmod(0o600)
"
