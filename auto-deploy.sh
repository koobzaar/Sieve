#!/usr/bin/env bash
# Pull and deploy Sieve whenever origin/main advances. Invoked by sieve-deploy.timer.
set -euo pipefail

REPO_DIR=/opt/Sieve
DOCKER_CONTEXT=rootless
OTHER_DOCKER_CONTEXT=default
cd "$REPO_DIR"

git fetch --quiet origin main

REMOTE=$(git rev-parse origin/main)
DEPLOYED_REVISION_FILE=$(git rev-parse --git-path sieve-deployed-revision)
DEPLOYED=""
if [ -r "$DEPLOYED_REVISION_FILE" ]; then
    IFS= read -r DEPLOYED < "$DEPLOYED_REVISION_FILE" || true
fi

if [ "$DEPLOYED" = "$REMOTE" ]; then
    exit 0
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Sieve deployment refused: tracked working-tree changes are present." >&2
    exit 1
fi

git merge --ff-only origin/main
LOCAL=$(git rev-parse HEAD)
if [ "$LOCAL" != "$REMOTE" ]; then
    echo "Sieve deployment refused: local main does not match origin/main." >&2
    exit 1
fi

DEPLOYED_SHORT=${DEPLOYED:0:7}
if [ -z "$DEPLOYED_SHORT" ]; then
    DEPLOYED_SHORT=unknown
fi
echo "New Sieve main commit: $DEPLOYED_SHORT -> ${REMOTE:0:7}; deploying."

OTHER_SIEVE_IDS=$(docker --context "$OTHER_DOCKER_CONTEXT" ps \
    --filter status=running \
    --filter label=com.docker.compose.project=sieve \
    --filter label=com.docker.compose.service=sieve \
    --format '{{.ID}}')
if [ -n "$OTHER_SIEVE_IDS" ]; then
    echo "Sieve deployment refused: a Sieve Compose service is already running in Docker context '$OTHER_DOCKER_CONTEXT' ($OTHER_SIEVE_IDS)." >&2
    exit 1
fi

# Ignored local secrets and Compose overrides remain outside Git and are preserved.
docker --context "$DOCKER_CONTEXT" compose up -d --build --wait --wait-timeout 240 sieve

REVISION_TMP="${DEPLOYED_REVISION_FILE}.tmp"
printf '%s\n' "$REMOTE" > "$REVISION_TMP"
mv "$REVISION_TMP" "$DEPLOYED_REVISION_FILE"
docker --context "$DOCKER_CONTEXT" image prune -f >/dev/null 2>&1 || true

echo "Sieve deployment complete: $(git rev-parse --short HEAD)."
