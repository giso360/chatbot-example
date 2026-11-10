#!/usr/bin/env bash
# Runs ON the EC2 instance, from the GitHub Actions self-hosted runner's checkout.
#   usage: scripts/deploy-local.sh <git-sha>
# Builds the image from the checked-out code, swaps the container, waits until it is healthy
# and rolls back to the previous container if it is not.
set -euo pipefail

SHA="${1:-manual}"
NAME=bedrock-chat
IMAGE="bedrock-chat:${SHA:0:12}"
REGION="${AWS_REGION:-us-east-1}"

cd "$(dirname "$0")/.."

echo "==> Building $IMAGE"
sudo docker build -t "$IMAGE" -t bedrock-chat:latest .

echo "==> Replacing container $NAME"
sudo docker rm -f "${NAME}-old" >/dev/null 2>&1 || true
had_old=0
if sudo docker inspect "$NAME" >/dev/null 2>&1; then
  sudo docker rename "$NAME" "${NAME}-old"
  sudo docker stop "${NAME}-old" >/dev/null
  had_old=1
fi

# The chat-data volume keeps the database (and the generated session secret) across deployments.
# AWS credentials are not passed in: boto3 uses the instance's IAM role.
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
# Drop images no container uses any more (the stopped old container keeps its image for rollback).
sudo docker image prune -af >/dev/null
sudo docker ps --format '{{.Names}}  {{.Image}}  {{.Status}}'
