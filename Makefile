COMPOSE := docker compose
PY := python3
.DEFAULT_GOAL := help

export NODE_PLATFORM ?= linux/$(shell docker version --format '{{.Server.Arch}}' 2>/dev/null)

.PHONY: help compose build seed up down logs creds status push-defender clean check

help:  ## 명령 목록
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  일반 순서:  make up   (compose 생성 → 시드 → 인프라 기동)"
	@echo "  실행 노드 platform: $(NODE_PLATFORM)"

.env:
	@test -f .env || (cp .env.example .env && echo "→ .env 생성(.env.example 복사)")

compose:  ## config → docker-compose.yml 생성
	$(PY) tools/gen_compose.py

build: compose  ## 인프라 이미지 빌드(에이전트 제외)
	$(COMPOSE) build litellm-gw orchestrator sla-checker \
	  $(shell $(PY) tools/list_services.py --build)

seed:  ## 역할 기반 팀 에이전트 이미지 시드
	$(PY) tools/seed.py

up: .env compose  ## 전체 기동 (compose → 시드 → 인프라)
	$(COMPOSE) build litellm-gw orchestrator sla-checker \
	  $(shell $(PY) tools/list_services.py --build)
	$(PY) tools/seed.py
	$(COMPOSE) up -d
	@echo "→ 참가자 페이지 http://localhost:$${DASHBOARD_PORT:-4100}"
	@echo "→ 토큰 확인:  make creds"

down:  ## 스택 중지 및 볼륨 삭제
	$(COMPOSE) down -v

logs:  ## 오케스트레이터 로그
	$(COMPOSE) logs -f --tail=120 orchestrator

creds:  ## 팀 토큰·계정 출력
	@test -f data-export/credentials.json 2>/dev/null || true
	$(COMPOSE) exec orchestrator cat /data/credentials.json 2>/dev/null \
	  || $(COMPOSE) logs orchestrator | sed -n '/====/,/====/p' | tail -30

status:  ## 현재 대회 상태(JSON)
	@curl -fsS http://localhost:$${DASHBOARD_PORT:-4100}/api/scoreboard | $(PY) -m json.tool

TEAM ?= 9

push-defender:  ## 내 방어 push (CTX=../defense-agent TEAM=9 [RELOAD=1])
	bash tools/push_agent.sh defender $(TEAM) $(or $(CTX),../defense-agent) $(if $(RELOAD),--reload,)

push-attacker:  ## 팀원 공격 push (CTX=../attack-agent TEAM=9 [RELOAD=1])
	bash tools/push_agent.sh attacker $(TEAM) $(or $(CTX),../attack-agent) $(if $(RELOAD),--reload,)

reload:  ## 이미 push된 이미지를 즉시 반영 (ROLE=defender|attacker|both TEAM=9)
	@curl -fsS -X POST http://localhost:$${DASHBOARD_PORT:-4100}/control/pull \
	  -H 'Content-Type: application/json' \
	  -d '{"team": $(TEAM), "role": "$(or $(ROLE),both)"}' | $(PY) -m json.tool

skip:  ## 현재 대기/라운드를 즉시 넘김(빠른 반복)
	@curl -fsS -X POST http://localhost:$${DASHBOARD_PORT:-4100}/control/skip | $(PY) -m json.tool

report:  ## 테스트베드 결과 요약(head-to-head)
	@$(PY) tools/report.py

ghcr-login:  ## GHCR 로그인 (원격 모드 협업 전 1회, GH_USER=... GH_PAT=...)
	@echo "$${GH_PAT:?GH_PAT(패키지 read/write PAT) 필요}" | \
	  docker login ghcr.io -u "$${GH_USER:?GH_USER 필요}" --password-stdin

check:  ## 로컬 유닛 점검(도커 불필요)
	$(PY) tools/selfcheck.py

clean: down  ## 생성 파일 정리
	rm -f docker-compose.yml
