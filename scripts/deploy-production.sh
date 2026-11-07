#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/deploy/apps/topsbottg}"
DEPLOY_SHA="${1:-}"
COMPOSE_TIMEOUT_SECONDS="${COMPOSE_TIMEOUT_SECONDS:-240}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-90}"
PROJECT_NAME="$(basename "$PROJECT_DIR")"
RELEASE_ROOT="${DEPLOY_RELEASE_ROOT:-$(dirname "$PROJECT_DIR")/.topsbottg-deploy/releases}"
RELEASE_DIR="$RELEASE_ROOT/$DEPLOY_SHA"
RELEASE_CREATED=0
NGINX_SWITCH_STARTED=0

log() { printf '[deploy] %s\n' "$*"; }
fail() { log "ERROR: $*" >&2; exit 1; }
compose() {
  docker compose --project-name "$PROJECT_NAME" --project-directory "$RELEASE_DIR" \
    --env-file "$PROJECT_DIR/.env" "$@"
}
on_error() {
  local status=$?
  log "FAILED at line ${BASH_LINENO[0]} (exit ${status}). Recent app/nginx logs:" >&2
  if [[ -d "$PROJECT_DIR" ]]; then
    (cd "$PROJECT_DIR" && timeout 20s docker compose --project-name "$PROJECT_NAME" \
      --project-directory "$PROJECT_DIR" --env-file "$PROJECT_DIR/.env" logs --tail=80 app nginx 2>&1) \
      | sed -E 's#(https?://[^:/[:space:]]+):[^@/[:space:]]+@#\1:[REDACTED]@#g; s/[0-9]{6,}:[A-Za-z0-9_-]{20,}/[REDACTED_BOT_TOKEN]/g' >&2 || true
  fi
  if (( RELEASE_CREATED == 1 && NGINX_SWITCH_STARTED == 0 )); then
    log "Removing incomplete staging worktree $RELEASE_DIR."
    git -C "$PROJECT_DIR" worktree remove "$RELEASE_DIR" || true
  fi
  exit "$status"
}
trap on_error ERR

[[ "$DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "Expected a full 40-character lowercase commit SHA."
[[ -d "$PROJECT_DIR/.git" ]] || fail "Project directory is missing its .git repository: $PROJECT_DIR"
cd "$PROJECT_DIR"
[[ "$(git rev-parse --show-toplevel)" == "$PROJECT_DIR" ]] || fail "Git root does not match the configured production directory."
[[ "$(git remote get-url origin)" == "https://github.com/yevgmaksimovvv/topsbottg.git" ]] || fail "Unexpected origin URL."

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  git status --short >&2
  fail "Tracked production files have local modifications. Resolve them before deploying."
fi

log "Checking PostgreSQL readiness."
timeout 20s docker compose exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

log "Creating a database backup before any code update or migration."
backup_output="$(timeout 120s ./scripts/backup.sh)" || fail "Backup script failed; migration and deploy were not started."
printf '%s\n' "$backup_output"
backup_path="$(printf '%s\n' "$backup_output" | sed -n 's/^Backup completed: //p' | tail -n 1)"
[[ -n "$backup_path" && -s "$backup_path" ]] || fail "Backup script did not produce a non-empty SQL backup."
log "Verified SQL backup: $backup_path"

log "Checking requested SHA is still the latest main commit."
git fetch --quiet origin main
latest_main="$(git rev-parse refs/remotes/origin/main)"
[[ "$latest_main" == "$DEPLOY_SHA" ]] || fail "Refusing stale deploy: requested $DEPLOY_SHA, current origin/main is $latest_main."
git cat-file -e "$DEPLOY_SHA^{commit}" || fail "Requested SHA is not a commit in this repository."
current="$(git rev-parse HEAD)"
git merge-base --is-ancestor "$current" "$DEPLOY_SHA" || fail "Requested SHA is not a fast-forward from current production HEAD."

if [[ ! -e "$RELEASE_DIR" ]]; then
  mkdir -p "$RELEASE_ROOT"
  log "Preparing an isolated release worktree for $DEPLOY_SHA."
  git worktree add --detach "$RELEASE_DIR" "$DEPLOY_SHA"
  RELEASE_CREATED=1
else
  [[ "$(git -C "$RELEASE_DIR" rev-parse HEAD)" == "$DEPLOY_SHA" ]] || fail "Existing release staging directory has the wrong SHA."
  log "Reusing isolated release worktree for $DEPLOY_SHA."
fi

log "Validating Docker Compose configuration from the isolated release."
compose config --quiet
log "Building app image from the isolated release."
timeout "$COMPOSE_TIMEOUT_SECONDS"s docker compose --project-name "$PROJECT_NAME" \
  --project-directory "$RELEASE_DIR" --env-file "$PROJECT_DIR/.env" build app
log "Starting the new app and waiting for its health check; the live frontend is unchanged."
timeout "$COMPOSE_TIMEOUT_SECONDS"s docker compose --project-name "$PROJECT_NAME" \
  --project-directory "$RELEASE_DIR" --env-file "$PROJECT_DIR/.env" \
  up -d --no-deps --wait --wait-timeout "$HEALTH_TIMEOUT_SECONDS" app

log "Verifying Alembic reached head."
timeout 30s compose exec -T app alembic current | grep -q '(head)' || fail "Alembic is not at head. Do not run downgrade automatically."

log "Fast-forwarding the production checkout after the new app is healthy."
git merge --ff-only "$DEPLOY_SHA"

log "Recreating only the Compose nginx service after the new app passed health checks."
NGINX_SWITCH_STARTED=1
timeout 90s docker compose --project-name "$PROJECT_NAME" --project-directory "$RELEASE_DIR" \
  --env-file "$PROJECT_DIR/.env" up -d --no-deps --force-recreate --wait \
  --wait-timeout "$HEALTH_TIMEOUT_SECONDS" nginx

app_domain="$(sed -n 's/^APP_DOMAIN=//p' .env | tail -n 1 | tr -d '\r"' | sed "s/^'//; s/'$//")"
[[ -n "$app_domain" ]] || fail "APP_DOMAIN is missing from production .env."
base_url="http://127.0.0.1:18080"
log "Checking app and Mini App through local Nginx."
curl --silent --show-error --fail --max-time 10 -H "Host: $app_domain" "$base_url/healthz" >/dev/null
html="$(curl --silent --show-error --fail --max-time 10 -H "Host: $app_domain" "$base_url/miniapp/")"
js_path="$(printf '%s' "$html" | grep -Eo 'src="/miniapp/[^" ]+\.js([?][^" ]*)?"' | head -n 1 | sed 's/^src="//; s/"$//')"
[[ -n "$js_path" ]] || fail "Mini App HTML did not reference a JavaScript module."
headers="$(mktemp)"
trap 'rm -f "$headers"' EXIT
curl --silent --show-error --fail --max-time 10 -D "$headers" -o /dev/null -H "Host: $app_domain" "$base_url$js_path"
grep -Eiq '^Content-Type: (application|text)/javascript([;[:space:]]|$)' "$headers" || fail "Mini App JavaScript response has an invalid MIME type."
grep -Eiq '^X-Content-Type-Options: nosniff[[:space:]]*$' "$headers" || fail "Mini App JavaScript response is missing nosniff."

printf '%s\n' "$DEPLOY_SHA" > .deployed-sha.tmp
mv .deployed-sha.tmp .deployed-sha
log "SUCCESS: deployed $DEPLOY_SHA"
