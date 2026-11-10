#!/usr/bin/env bash
# Runs ON the EC2 instance (piped over SSH by .github/workflows/deploy.yml).
#   usage: ECR_TOKEN=... remote-deploy.sh <image-uri> <aws-region>
# Pulls the image, swaps the container, waits for it to be healthy and rolls back if it is not.
set -euo pipefail

IMAGE="$1"
REGION="${2:-us-east-1}"
REGISTRY="${IMAGE%%/*}"
NAME=bedrock-chat

# Throw-away docker config so the ECR token is never stored on the instance.
CFG="$(mktemp -d)"
trap 'rm -rf "$CFG"' EXIT
docker_() { sudo docker --config "$CFG" "$@"; }

echo "==> Pulling $IMAGE"
printf '%s' "$ECR_TOKEN" | docker_ login --username AWS --password-stdin "$REGISTRY" >/dev/null
docker_ pull "$IMAGE"

echo "==> Replacing container $NAME"
sudo docker rm -f "${NAME}-old" >/dev/null 2>&1 || true
had_old=0
if sudo docker inspect "$NAME" >/dev/null 2>&1; then
  sudo docker rename "$NAME" "${NAME}-old"
  sudo docker stop "${NAME}-old" >/dev/null
  had_old=1
fi

# chat-data volume keeps the database (and the generated session secret) across deployments.
sudo docker run -d --name "$NAME" --restart unless-stopped \
  -p 8000:8000 -e AWS_REGION="$REGION" -v chat-data:/data "$IMAGE" >/dev/null

status=starting
for _ in $(seq 1 30); do
  status="$(sudo docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo missing)"
  [ "$status" = healthy ] && break
  sleep 3
done

if [ "$status" != healthy ]; then
  echo "::error::New container is $status; rolling back" >&2
  sudo docker logs --tail 40 "$NAME" >&2 || true
  sudo docker rm -f "$NAME" >/dev/null 2>&1 || true
  if [ "$had_old" = 1 ]; then
    sudo docker rename "${NAME}-old" "$NAME"
    sudo docker start "$NAME" >/dev/null
    echo "Rolled back to the previous container." >&2
  fi
  exit 1
fi

echo "==> Healthy"
curl -fsS http://127.0.0.1:8000/health && echo
# Drop images no container uses any more (the old container keeps its own image for rollback).
sudo docker image prune -af >/dev/null
sudo docker ps --format '{{.Names}}  {{.Image}}  {{.Status}}'
