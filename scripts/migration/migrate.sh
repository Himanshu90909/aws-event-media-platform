#!/usr/bin/env bash
set -Eeuo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
IMAGE="${IMAGE:-event-media-platform:${GIT_COMMIT_SHA:-local}}"
PREVIOUS_IMAGE="${PREVIOUS_IMAGE:-}"

check() { printf '[migration] %s\n' "$*"; }
http_check() { curl --fail --silent --show-error --max-time 5 "$1" >/dev/null; }

case "${1:-preflight}" in
  preflight)
    check "checking required tools and inputs"
    command -v docker >/dev/null || { echo 'docker is required'; exit 2; }
    command -v curl >/dev/null || { echo 'curl is required'; exit 2; }
    check "image=$IMAGE base_url=$BASE_URL"
    ;;
  build)
    check "building immutable image"
    docker build --pull -t "$IMAGE" -f docker/Dockerfile .
    docker image inspect "$IMAGE" >/dev/null
    ;;
  smoke)
    check "checking health and readiness"
    http_check "$BASE_URL/health"
    http_check "$BASE_URL/ready"
    curl --fail --silent --show-error -X POST "$BASE_URL/jobs" -H 'Content-Type: application/json' -d '{"fileName":"smoke.mp4","contentType":"video/mp4"}' | grep -q 'jobId'
    ;;
  rollback)
    test -n "$PREVIOUS_IMAGE" || { echo 'PREVIOUS_IMAGE is required'; exit 2; }
    check "rollback target recorded: $PREVIOUS_IMAGE"
    check "for ECS, redeploy the previous task definition/image and rerun smoke"
    ;;
  all)
    "$0" preflight && "$0" build && "$0" smoke
    ;;
  *) echo "usage: $0 {preflight|build|smoke|rollback|all}"; exit 2 ;;
esac
