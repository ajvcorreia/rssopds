#!/usr/bin/env bash
# Deploy the working tree to the Docker host and restart the container.
#
# Uses rsync --delete rather than shipping a tar, because a tar extracted over
# an existing directory only ever *adds* files: a module deleted here lingers
# there forever. That actually happened -- tools/imagetest.py survived on the
# server for a day after being removed locally, importing a dependency that no
# longer existed.
#
# Excluded from the transfer, and therefore never deleted on the far side:
#   data/   the database, EPUBs and cached images live only on the server
#   .env    credentials, deliberately server-only and gitignored
#
# Configure the target with environment variables (no defaults are baked in,
# since this script is checked into the repo):
#
#   RSSOPDS_HOST=user@your-server tools/deploy.sh
#   RSSOPDS_HOST=user@your-server RSSOPDS_SSH_KEY=~/.ssh/id_ed25519 tools/deploy.sh
#   RSSOPDS_HOST=user@your-server tools/deploy.sh --dry-run    # show, touch nothing
#   RSSOPDS_HOST=user@your-server tools/deploy.sh --no-tests   # skip the test run
set -euo pipefail

: "${RSSOPDS_HOST:?set RSSOPDS_HOST, e.g. RSSOPDS_HOST=user@your-server}"
HOST="$RSSOPDS_HOST"
REMOTE_DIR="${RSSOPDS_REMOTE_DIR:-rssopds}"
PROJECT="${RSSOPDS_COMPOSE_PROJECT:-rssopds}"
SSH_KEY="${RSSOPDS_SSH_KEY:-$HOME/.ssh/id_rsa}"

DRY=""
RUN_TESTS=1
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY="--dry-run" ;;
    --no-tests) RUN_TESTS=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."
SSH="ssh -i $SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new"

echo "==> syncing $(pwd) -> $HOST:$REMOTE_DIR/"
rsync -az --delete $DRY --itemize-changes \
  --exclude 'data/' \
  --exclude '.env' \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  -e "$SSH" \
  ./ "$HOST:$REMOTE_DIR/"

if [ -n "$DRY" ]; then
  echo "==> dry run only; nothing was changed"
  exit 0
fi

echo "==> rebuilding and restarting"
$SSH "$HOST" "cd $REMOTE_DIR && docker compose -p $PROJECT build 2>&1 | tail -1 \
  && docker compose -p $PROJECT up -d 2>&1 | tail -1 \
  && sleep 12 \
  && docker ps --filter name=$PROJECT --format '    {{.Names}} {{.Status}}'"

if [ "$RUN_TESTS" = "1" ]; then
  echo "==> running the test suites inside the deployed image"
  $SSH "$HOST" "cd $REMOTE_DIR \
    && docker exec $PROJECT rm -rf /app/tests \
    && docker cp tests $PROJECT:/app/tests >/dev/null \
    && docker exec -e RSSOPDS_DATA_DIR=/tmp/deploy-t1 $PROJECT \
         python /app/tests/test_pipeline.py 2>&1 | tail -2 \
    && docker exec -e RSSOPDS_DATA_DIR=/tmp/deploy-t2 $PROJECT \
         python /app/tests/test_output.py 2>&1 | tail -2"
fi

echo "==> done"
