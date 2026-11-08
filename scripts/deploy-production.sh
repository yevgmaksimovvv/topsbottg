#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

PROJECT_DIR="${PROJECT_DIR:-/home/deploy/apps/topsbottg}"
DEPLOY_SHA="${1:-}"
COMPOSE_TIMEOUT_SECONDS="${COMPOSE_TIMEOUT_SECONDS:-240}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-90}"
PROJECT_NAME=""
RELEASE_ROOT="${DEPLOY_RELEASE_ROOT:-$(dirname "$PROJECT_DIR")/.topsbottg-deploy/releases}"
RELEASE_DIR="$RELEASE_ROOT/$DEPLOY_SHA"
RELEASE_CREATED=0
NGINX_SWITCH_STARTED=0
DEPLOY_PHASE="preflight"
FAILURE_LOG="$PROJECT_DIR/.deploy-failures.log"

log() { printf '[deploy] %s\n' "$*"; }
fail() {
  log "ERROR: phase=$DEPLOY_PHASE $*" >&2
  if [[ -d "$PROJECT_DIR" ]]; then
    touch "$FAILURE_LOG" 2>/dev/null || true
    chmod 600 "$FAILURE_LOG" 2>/dev/null || true
    printf '%s sha=%s phase=%s message=%s\n' "$(date -u +%FT%TZ)" "$DEPLOY_SHA" "$DEPLOY_PHASE" "$*" >>"$FAILURE_LOG" 2>/dev/null || true
  fi
  exit 1
}
compose() {
  docker compose --project-name "$PROJECT_NAME" --project-directory "$RELEASE_DIR" \
    --env-file "$PROJECT_DIR/.env" --file "$RELEASE_DIR/docker-compose.yml" "$@"
}
on_error() {
  local status=$?
  log "FAILED phase=$DEPLOY_PHASE exit=$status; detailed diagnostics are retained on VPS in .deploy-failures.log" >&2
  if [[ -d "$PROJECT_DIR" ]]; then
    touch "$FAILURE_LOG" 2>/dev/null || true
    chmod 600 "$FAILURE_LOG" 2>/dev/null || true
    {
      printf '%s sha=%s phase=%s exit=%s line=%s\n' "$(date -u +%FT%TZ)" "$DEPLOY_SHA" \
        "$DEPLOY_PHASE" "$status" "${BASH_LINENO[0]}"
      (cd "$PROJECT_DIR" && timeout 20s docker compose --project-name "$PROJECT_NAME" \
        --project-directory "$PROJECT_DIR" --env-file "$PROJECT_DIR/.env" \
        --file "$PROJECT_DIR/docker-compose.yml" logs --tail=80 app nginx) || true
    } >>"$FAILURE_LOG" 2>&1
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
  fail "Tracked production files have local modifications. Resolve them before deploying."
fi

DEPLOY_PHASE="lock"
exec 9>"$PROJECT_DIR/.deploy.lock"
flock -n 9 || fail "Another production deployment is active; this attempt made no changes."

DEPLOY_PHASE="compose-project-discovery"
if ! project_config="$(docker compose --project-directory "$PROJECT_DIR" --env-file "$PROJECT_DIR/.env" \
  --file "$PROJECT_DIR/docker-compose.yml" config --format json 2>>"$FAILURE_LOG")"; then
  fail "Unable to resolve the existing production Compose project configuration."
fi
PROJECT_NAME="$(printf '%s\n' "$project_config" | sed -n 's/^[[:space:]]*"name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
[[ -n "$PROJECT_NAME" ]] || fail "Compose did not report the existing production project name."
unset project_config

DEPLOY_PHASE="postgres-readiness"

log "Checking PostgreSQL readiness."
# These environment variables are intentionally expanded by the shell inside the postgres container.
# shellcheck disable=SC2016
timeout 20s docker compose --project-name "$PROJECT_NAME" --project-directory "$PROJECT_DIR" \
  --env-file "$PROJECT_DIR/.env" --file "$PROJECT_DIR/docker-compose.yml" \
  exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

DEPLOY_PHASE="backup"
log "Creating a database backup before any code update or migration."
backup_output="$(timeout 120s ./scripts/backup.sh)" || fail "Backup script failed; migration and deploy were not started."
printf '%s\n' "$backup_output"
backup_path="$(printf '%s\n' "$backup_output" | sed -n 's/^Backup completed: //p' | tail -n 1)"
[[ -n "$backup_path" && -s "$backup_path" ]] || fail "Backup script did not produce a non-empty SQL backup."
log "Verified SQL backup: $backup_path"

DEPLOY_PHASE="sha-validation"
log "Checking requested SHA is still the latest main commit."
git fetch --quiet origin main
latest_main="$(git rev-parse refs/remotes/origin/main)"
[[ "$latest_main" == "$DEPLOY_SHA" ]] || fail "Refusing stale deploy: requested $DEPLOY_SHA, current origin/main is $latest_main."
git cat-file -e "$DEPLOY_SHA^{commit}" || fail "Requested SHA is not a commit in this repository."
current="$(git rev-parse HEAD)"
git merge-base --is-ancestor "$current" "$DEPLOY_SHA" || fail "Requested SHA is not a fast-forward from current production HEAD."

if [[ ! -e "$RELEASE_DIR" ]]; then
  DEPLOY_PHASE="release-worktree"
  mkdir -p "$RELEASE_ROOT"
  log "Preparing an isolated release worktree for $DEPLOY_SHA."
  git worktree add --detach "$RELEASE_DIR" "$DEPLOY_SHA"
  RELEASE_CREATED=1
else
  [[ "$(git -C "$RELEASE_DIR" rev-parse HEAD)" == "$DEPLOY_SHA" ]] || fail "Existing release staging directory has the wrong SHA."
  log "Reusing isolated release worktree for $DEPLOY_SHA."
fi

DEPLOY_PHASE="frontend-permissions"
frontend_dir="$RELEASE_DIR/frontend"
[[ -d "$frontend_dir" ]] || fail "Release frontend directory is missing."
log "Making only the release frontend readable by the Nginx container."
chmod -R a+rX -- "$frontend_dir"
[[ -f "$frontend_dir/index.html" && -r "$frontend_dir/index.html" ]] \
  || fail "Release frontend index.html is missing or unreadable."
js_file="$(find "$frontend_dir" -type f \( -name '*.js' -o -name '*.mjs' \) -print -quit)"
[[ -n "$js_file" && -r "$js_file" ]] || fail "Release frontend has no readable JavaScript module."
unreadable_file="$(find "$frontend_dir" -type f ! -perm -o+r -print -quit)"
unsearchable_dir="$(find "$frontend_dir" -type d ! -perm -o+rx -print -quit)"
[[ -z "$unreadable_file" && -z "$unsearchable_dir" ]] \
  || fail "Release frontend permissions do not allow the Nginx user to read static assets."

DEPLOY_PHASE="compose-validation"
log "Validating Docker Compose configuration from the isolated release."
compose config --quiet
DEPLOY_PHASE="app-build"
log "Building app image from the isolated release."
timeout "$COMPOSE_TIMEOUT_SECONDS"s docker compose --project-name "$PROJECT_NAME" \
  --project-directory "$RELEASE_DIR" --env-file "$PROJECT_DIR/.env" \
  --file "$RELEASE_DIR/docker-compose.yml" build app
DEPLOY_PHASE="app-replacement-and-health"
log "Replacing the app container and waiting for health; this Compose project runs one app/polling instance."
timeout "$COMPOSE_TIMEOUT_SECONDS"s docker compose --project-name "$PROJECT_NAME" \
  --project-directory "$RELEASE_DIR" --env-file "$PROJECT_DIR/.env" \
  --file "$RELEASE_DIR/docker-compose.yml" \
  up -d --no-deps --wait --wait-timeout "$HEALTH_TIMEOUT_SECONDS" app

DEPLOY_PHASE="alembic-verification"
log "Verifying Alembic reached head."
timeout 30s docker compose --project-name "$PROJECT_NAME" --project-directory "$RELEASE_DIR" \
  --env-file "$PROJECT_DIR/.env" --file "$RELEASE_DIR/docker-compose.yml" exec -T app alembic current \
  | grep -q '(head)' || fail "Alembic is not at head. Do not run downgrade automatically."

DEPLOY_PHASE="checkout-fast-forward"
log "Fast-forwarding the production checkout after the new app is healthy."
git merge --ff-only "$DEPLOY_SHA"

log "Recreating only the Compose nginx service after the new app passed health checks."
DEPLOY_PHASE="nginx-recreate"
NGINX_SWITCH_STARTED=1
timeout 90s docker compose --project-name "$PROJECT_NAME" --project-directory "$RELEASE_DIR" \
  --env-file "$PROJECT_DIR/.env" --file "$RELEASE_DIR/docker-compose.yml" \
  up -d --no-deps --force-recreate --wait \
  --wait-timeout "$HEALTH_TIMEOUT_SECONDS" nginx

DEPLOY_PHASE="endpoint-verification"
app_domain="$(sed -n 's/^APP_DOMAIN=//p' .env | tail -n 1 | tr -d '\r"' | sed "s/^'//; s/'$//")"
[[ -n "$app_domain" ]] || fail "APP_DOMAIN is missing from production .env."
base_url="http://127.0.0.1:18080"
log "Checking app and Mini App through local Nginx."
endpoint_deadline=$((SECONDS + 60))
endpoint_body="$(mktemp)"
endpoint_headers="$(mktemp)"
endpoint_error="$(mktemp)"
trap 'rm -f "$endpoint_body" "$endpoint_headers" "$endpoint_error"' EXIT
check_endpoint() {
  local url="$1" attempt=0 http_code curl_exit request_timeout remaining
  while (( attempt < 5 && SECONDS < endpoint_deadline )); do
    ((attempt += 1))
    remaining=$((endpoint_deadline - SECONDS))
    request_timeout=$((remaining < 5 ? remaining : 5))
    : >"$endpoint_body"
    : >"$endpoint_headers"
    : >"$endpoint_error"
    if http_code="$(curl --noproxy '*' --http1.1 --silent --show-error \
      --connect-timeout 3 --max-time "$request_timeout" -o "$endpoint_body" -D "$endpoint_headers" \
      -w '%{http_code}' -H "Host: $app_domain" "$url" 2>"$endpoint_error")"; then
      curl_exit=0
    else
      curl_exit=$?
    fi

    if (( curl_exit == 0 )) && [[ "$http_code" == 200 ]]; then
      return 0
    fi

    if (( curl_exit != 0 )); then
      case "$curl_exit" in
        7|28|52|56) ;;
        *) fail "Endpoint failed: URL=$url HTTP=${http_code:-000} curl_exit=$curl_exit." ;;
      esac
    elif [[ "$http_code" != 502 && "$http_code" != 503 && "$http_code" != 504 ]]; then
      fail "Endpoint failed: URL=$url HTTP=${http_code:-000} curl_exit=0."
    fi

    log "Transient endpoint failure: URL=$url HTTP=${http_code:-000} curl_exit=$curl_exit attempt=$attempt/5."
    if (( attempt < 5 && SECONDS < endpoint_deadline )); then
      remaining=$((endpoint_deadline - SECONDS))
      sleep "$((remaining < 2 ? remaining : 2))"
    fi
  done
  fail "Endpoint failed after bounded retries: URL=$url HTTP=${http_code:-000} curl_exit=${curl_exit:-0}."
}

check_endpoint "$base_url/healthz"
check_endpoint "$base_url/miniapp/"
html="$(cat "$endpoint_body")"
check_endpoint "$base_url/miniapp/styles.css"
js_path="$(printf '%s' "$html" | grep -Eo 'src="/miniapp/[^" ]+\.js([?][^" ]*)?"' | head -n 1 | sed 's/^src="//; s/"$//')"
[[ -n "$js_path" ]] || fail "Mini App HTML did not reference a JavaScript module."
check_endpoint "$base_url$js_path"
grep -Eiq '^Content-Type: (application|text)/javascript([;[:space:]]|$)' "$endpoint_headers" \
  || fail "Endpoint failed: URL=$base_url$js_path HTTP=200 curl_exit=0; invalid JavaScript MIME type."
grep -Eiq '^X-Content-Type-Options: nosniff[[:space:]]*$' "$endpoint_headers" \
  || fail "Endpoint failed: URL=$base_url$js_path HTTP=200 curl_exit=0; missing nosniff."

printf '%s\n' "$DEPLOY_SHA" > .deployed-sha.tmp
mv .deployed-sha.tmp .deployed-sha
DEPLOY_PHASE="complete"
log "SUCCESS: deployed $DEPLOY_SHA"
