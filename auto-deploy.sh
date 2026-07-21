#!/usr/bin/env bash
# Pull and deploy Sieve whenever origin/main advances. Invoked by sieve-deploy.timer.
set -euo pipefail

REPO_DIR=/opt/Sieve
cd "$REPO_DIR"

git fetch --quiet origin main

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

echo "New Sieve main commit: ${LOCAL:0:7} -> ${REMOTE:0:7}; deploying."

# Local secrets and override configuration are ignored, so they are preserved.
git reset --hard origin/main
docker compose up -d --build sieve
docker image prune -f >/dev/null 2>&1 || true

echo "Sieve deployment complete: $(git rev-parse --short HEAD)."
