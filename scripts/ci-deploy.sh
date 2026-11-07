#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SHA="${1:-}"
REMOTE_RUN_ID="${GITHUB_RUN_ID:-}"
REMOTE_RUN_ATTEMPT="${GITHUB_RUN_ATTEMPT:-1}"

[[ "$DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "A full commit SHA is required." >&2; exit 2; }
[[ "$REMOTE_RUN_ID" =~ ^[0-9]+$ && "$REMOTE_RUN_ATTEMPT" =~ ^[0-9]+$ ]] || {
  echo "GitHub Actions run identifiers are missing or invalid." >&2
  exit 2
}
: "${DEPLOY_HOST:?DEPLOY_HOST is required}"
: "${DEPLOY_PORT:?DEPLOY_PORT is required}"
: "${DEPLOY_USER:?DEPLOY_USER is required}"
: "${DEPLOY_KNOWN_HOSTS:?DEPLOY_KNOWN_HOSTS is required}"
: "${SSH_PRIVATE_KEY:?SSH_PRIVATE_KEY is required}"
[[ "$DEPLOY_PORT" =~ ^[0-9]+$ ]] && (( DEPLOY_PORT > 0 && DEPLOY_PORT < 65536 )) || {
  echo "DEPLOY_PORT must be between 1 and 65535." >&2
  exit 2
}

key_file="$(mktemp)"
known_hosts_file="$(mktemp)"
remote_script=".topsbottg-deploy-${REMOTE_RUN_ID}-${REMOTE_RUN_ATTEMPT}.sh"
cleanup() { rm -f "$key_file" "$known_hosts_file"; }
trap cleanup EXIT
umask 077
printf '%s\n' "$SSH_PRIVATE_KEY" > "$key_file"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$known_hosts_file"
chmod 600 "$key_file" "$known_hosts_file"

ssh_options=(
  -i "$key_file"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$known_hosts_file"
  -o ConnectTimeout=15
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=3
)
target="$DEPLOY_USER@$DEPLOY_HOST"
echo "Transferring deploy script from CI-verified commit $DEPLOY_SHA."
timeout 90s scp -q "${ssh_options[@]}" -P "$DEPLOY_PORT" \
  "$SCRIPT_DIR/deploy-production.sh" "$target:$remote_script"

echo "Starting production deploy for $DEPLOY_SHA."
timeout 12m ssh "${ssh_options[@]}" -p "$DEPLOY_PORT" "$target" \
  "bash -s -- '$remote_script' '$DEPLOY_SHA'" <<'REMOTE_SCRIPT'
set +e
chmod 700 -- "$1" && bash "$1" "$2"
deploy_status=$?
rm -f -- "$1"
exit "$deploy_status"
REMOTE_SCRIPT
echo "Production deploy completed for $DEPLOY_SHA."
