#!/usr/bin/env bash
# 에이전트 이미지 build·push (+선택적 즉시 반영).
#
# 사용법:
#   tools/push_agent.sh <defender|attacker> <team-id> <context-dir> [--reload]
#
#   tools/push_agent.sh defender 9 ../defense-agent          # 다음 pull 시점 반영
#   tools/push_agent.sh attacker 9 ../attack-agent --reload  # 즉시 반영
#
# 대회 재발 방지 절차와 동일하게 platform 고정 빌드 후 pull·inspect 로 검증한다.
# --reload 는 오케스트레이터에 즉시 pull·재생성을 요청한다(테스트 편의, 라운드
# 경계를 기다리지 않음). 대회 절차 연습에는 --reload 없이 다음 pull 을 기다린다.
set -euo pipefail
cd "$(dirname "$0")/.."

ROLE="${1:?role(defender|attacker) 필요}"
TEAM="${2:?team-id 필요}"
CTX="${3:?context-dir 필요}"
RELOAD="${4:-}"

PLATFORM="${NODE_PLATFORM:-linux/$(docker version --format '{{.Server.Arch}}')}"
DASH_PORT="${DASHBOARD_PORT:-4100}"
# config(로컬/원격 모드)에 맞는 push 대상 ref 를 해석
REF="$(python3 tools/ref.py "$TEAM" "$ROLE")"

if [ ! -f "$CTX/Dockerfile" ]; then
  echo "✗ $CTX/Dockerfile 없음 — 컨텍스트 경로를 확인하세요." >&2
  exit 1
fi

echo "▶ team$TEAM $ROLE 빌드·push  ($CTX → $REF, platform=$PLATFORM)"
docker buildx build --platform "$PLATFORM" --provenance=false --sbom=false \
  -t "$REF" --push "$CTX"

echo "▶ 검증(재Pull·아키텍처)"
docker pull --platform "$PLATFORM" "$REF" >/dev/null
ARCH="$(docker image inspect "$REF" --format '{{.Os}}/{{.Architecture}}')"
echo "  실행 노드 기대 : $PLATFORM"
echo "  이미지         : $ARCH"
if [ "$ARCH" != "$PLATFORM" ]; then
  echo "  ⚠ 불일치 — 실행 노드에서 반영되지 않습니다(리허설 arm64 사고와 동일)."
fi

if [ "$RELOAD" = "--reload" ]; then
  echo "▶ 즉시 반영 요청(orchestrator /control/pull)"
  curl -fsS -X POST "http://localhost:$DASH_PORT/control/pull" \
    -H 'Content-Type: application/json' \
    -d "{\"team\": $TEAM, \"role\": \"$ROLE\"}" \
    && echo "  ✓ pull·재생성 요청 완료" \
    || echo "  ⚠ 요청 실패(오케스트레이터 실행 중·ALLOW_CONTROL=1 확인)"
else
  echo "▶ 다음 라운드 pull 시점에 반영됩니다(참가자 페이지에서 확인)."
fi
