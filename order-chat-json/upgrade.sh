#!/usr/bin/env bash
# 이미 배포된 order-chat의 MinIO/Ollama 설정을 유지하면서 이미지만 교체합니다.
set -euo pipefail
if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo '사용법: ./upgrade.sh docker.io/본인계정/order-chat:2.0.1 [namespace]'
  exit 2
fi
IMAGE="$1"
NAMESPACE="${2:-default}"
cd -- "$(dirname -- "$0")"
kubectl -n "$NAMESPACE" get deployment/order-chat >/dev/null
CONTAINERS="$(kubectl -n "$NAMESPACE" get deployment/order-chat -o jsonpath='{.spec.template.spec.containers[*].name}')"
if [[ " $CONTAINERS " != *' chat '* ]]; then
  echo 'order-chat Deployment의 컨테이너 이름이 chat인지 확인하세요.'
  exit 2
fi
docker build -t "$IMAGE" .
docker push "$IMAGE"
kubectl -n "$NAMESPACE" set image deployment/order-chat "chat=$IMAGE"
kubectl -n "$NAMESPACE" rollout status deployment/order-chat --timeout=180s
