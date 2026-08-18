# CLAUDE.md — arena 작업 지침

DAH 공방 에이전트 **head-to-head 테스트베드**. 실제 대회와 동일한 broker 계약
위에서 내 방어 에이전트와 팀원 공격 에이전트를 붙여 검증한다. 개요·사용법은
`README.md`, 협업은 `docs/COLLAB.md`, git 명령은 `docs/GIT.md`.

## 커밋·푸시 규칙 (중요)

- **커밋·푸시는 사용자가 직접 한다.** Claude 는 `git commit`/`git push`/`git remote`
  를 실행하지 말고, 복붙용 명령만 제시한다.
- **커밋 메시지는 Conventional Commits, 한 줄, 영문.**
  형식: `<type>: <summary>` — 예:
  - `feat: add GHCR remote registry mode`
  - `fix: close fail-open window at round boundary`
  - `docs: document head-to-head testbed workflow`
  - `refactor: centralize image ref resolution`
  - `chore: bump round timing defaults`
  - type: `feat` `fix` `docs` `refactor` `chore` `test` `perf`.
- 커밋 전 `git status --short` 로 시크릿·생성물이 없는지 확인한다.
- **브랜치 정책**: 최초 import 는 `main` 직접 push(기준선 생성). 이후 모든 변경은
  `feat/…`·`fix/…` 브랜치에서 작업하고 `main` 에 병합한다. `main` 직접 커밋 금지.

## 절대 커밋하지 않는 것 (`.gitignore` 로 제외됨)

- `.env` — 로컬 설정·키
- `docker-compose.yml` — **생성물**. `tools/gen_compose.py` 가 만들며 LLM 키가 박힌다.
  손으로 고치지 말고 `config/competition.yml` 수정 후 `make compose` 로 재생성.
- `data/`, `*.pcap`, `captures/`, `artifacts/` — 런타임 산출물

## 코드 변경 시

- 의존성 0 원칙: 표준 라이브러리만. compose·config 파서도 자체 구현(`core/miniyaml.py`).
- config 스키마를 바꾸면 `core/config.py` 파서와 `config/competition.yml` 을 함께 수정.
- 도커 없이 검증: `make check` (`tools/selfcheck.py` — config·채점·패킷합성·broker 로직).
- 파이썬 편집 후 `python3 -m py_compile` 로 문법 확인(런타임 import 오류 예방).

## 구조 한눈에

- `gateway/` — userspace 방어 broker(NFQUEUE 없이 PACKET/VERDICT/HEARTBEAT, 300ms).
- `orchestrator/` — 참가자 페이지·라운드 자동 진행·채점·registry pull. `core/` 에 로직.
- `challenges/` — L1 SSRF · L2 세션위조 · L3 SQLi (리허설 스켈레톤, 본선 무관).
- `agents/` — `passthrough-defender`(방어 골격), `bot-attacker`(봇), `ATTACKER_INTERFACE.md`.
- 실제 방어·공격은 각자 레포에서 개발해 이미지로 붙인다(로컬/ GHCR 모드).

## 실행

```bash
make up        # 기동  ·  make report  # head-to-head 요약
make push-defender TEAM=9 CTX=../defense-agent RELOAD=1
make push-attacker TEAM=9 CTX=../attack-agent/agents/team1/attacker RELOAD=1
```
