#!/usr/bin/env bash
#
# Deploy LiveScoring from git.  Run on the server, as root.
#
#   deploy/deploy.sh [ref]        ref defaults to origin/main
#
# Idempotent: re-running with no new commits and a clean tree exits 0 without
# touching the services.  Serialised with flock, so two overlapping CI runs
# cannot interleave a checkout with a migration.
#
# ON FAILURE the code is rolled back to the previous commit and the services
# are restarted on it.  DATABASE MIGRATIONS ARE NOT REVERSED -- Django
# migrations are forward-only here, so a deploy that fails *after* migrate
# leaves the schema ahead of the code.  That is the one case needing hands.
set -euo pipefail

REPO_DIR=${REPO_DIR:-/srv/livescoring}
ENV_FILE=${ENV_FILE:-/etc/livescoring.env}
HEALTH_URL=${HEALTH_URL:-http://127.0.0.1:8100/health}
SERVICE_USER=${SERVICE_USER:-livescoring}
REF=${1:-origin/main}
SERVICES=(livescoring-api.service livescoring-scorer.service)
DEPLOY_GIT_SSH_KEY=${DEPLOY_GIT_SSH_KEY:-}

log() { printf '[deploy %s] %s\n' "$(date -Is)" "$*"; }

# Serialise deploys.  Re-exec under flock on first entry.
LOCK=/var/lock/livescoring-deploy.lock
if [ "${_DEPLOY_LOCKED:-}" != "1" ]; then
    exec env _DEPLOY_LOCKED=1 flock -w 600 "$LOCK" "$0" "$@"
fi

cd "$REPO_DIR"

PREV=$(git rev-parse HEAD)
log "repo $REPO_DIR at $PREV, requested $REF"

if [ -n "$DEPLOY_GIT_SSH_KEY" ] && [ -r "$DEPLOY_GIT_SSH_KEY" ]; then
    export GIT_SSH_COMMAND="ssh -i $DEPLOY_GIT_SSH_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
fi

git fetch --prune --quiet origin
TARGET=$(git rev-parse "$REF")

if [ "$PREV" = "$TARGET" ] && [ -z "$(git status --porcelain --untracked-files=no)" ]; then
    log "already at $TARGET with a clean tree; nothing to do"
    exit 0
fi

restarted=0
rollback() {
    log "DEPLOY FAILED"
    if [ "$(git rev-parse HEAD)" != "$PREV" ]; then
        log "restoring code to $PREV"
        git reset --hard --quiet "$PREV"
        chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_DIR"
    fi
    if [ "$restarted" = 1 ]; then
        log "restarting services on $PREV"
        systemctl restart "${SERVICES[@]}" || true
    fi
    log "NOTE: any migration that already ran has NOT been reversed"
    exit 1
}
trap rollback ERR

log "checking out $TARGET"
git reset --hard --quiet "$TARGET"
chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_DIR"

# The services read their settings from here, and so must migrate/collectstatic
# -- without it Django falls through to the unused local sqlite file.
set -a; . "$ENV_FILE"; set +a
export DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE:-live_django.settings}

PYTHON=${PYTHON:-$REPO_DIR/venv/bin/python}
PIP=${PIP:-$REPO_DIR/venv/bin/pip}
if [ ! -x "$PYTHON" ]; then
    PYTHON=python3
fi
if [ ! -x "$PIP" ]; then
    PIP=pip3
fi

if ! git diff --quiet "$PREV" "$TARGET" -- requirements.txt; then
    log "requirements.txt changed; installing"
    "$PIP" install --quiet -r requirements.txt
else
    log "requirements.txt unchanged; skipping install"
fi

log "running engine self-checks"
"$PYTHON" -m tests > /tmp/livescoring-deploy-tests.log 2>&1 \
    || { log "engine checks FAILED (see /tmp/livescoring-deploy-tests.log)"; tail -20 /tmp/livescoring-deploy-tests.log; false; }
log "engine checks passed"

log "applying migrations"
"$PYTHON" manage.py migrate --noinput

log "collecting static files"
"$PYTHON" manage.py collectstatic --noinput --clear > /dev/null

chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_DIR"

log "restarting ${SERVICES[*]}"
restarted=1
systemctl restart "${SERVICES[@]}"

log "waiting for health at $HEALTH_URL"
for i in $(seq 1 30); do
    if curl -fsS --max-time 5 "$HEALTH_URL" > /dev/null 2>&1; then
        for s in "${SERVICES[@]}"; do
            systemctl is-active --quiet "$s" || { log "$s is not active"; false; }
        done
        trap - ERR
        log "healthy after ${i}s"
        log "DEPLOYED $(git log --oneline -1)"
        exit 0
    fi
    sleep 1
done

log "health check did not pass within 30s"
rollback
