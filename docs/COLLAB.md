# 팀 협업 가이드 — arena 공유 테스트베드

`arena` 는 **공유 통합·채점판**이다. 방어 에이전트와 공격 에이전트는 각자 자기
저장소에서 개발하고, arena 는 그 이미지를 pull 해 대회와 동일한 broker 계약 위에서
head-to-head 로 붙인다.

## 저장소 3개 (실제 대회 모델과 동일)

| 저장소 | 소유 | 산출물 이미지 |
|---|---|---|
| `DAH-aexploit/defense-agent` | 방어 담당 | `team9-defender` |
| `DAH-aexploit/rehearsal-attacker` | 공격 담당 | `team9-attacker` |
| **`DAH-aexploit/arena`** (이 저장소) | 공용 | 인프라·챌린지·채점·참가자 페이지 |

에이전트는 **env 계약만** 지키면 arena 에 그대로 붙는다(하드코딩 없음).

- 공격: `TARGETS, PORTS, SUBMIT_URL, SUBMIT_TOKEN, LLM_BASE_URL, LLM_API_KEY`
  → [`agents/ATTACKER_INTERFACE.md`](../agents/ATTACKER_INTERFACE.md)
- 방어: broker 소켓 `AGENT_SOCKET`(PACKET/VERDICT/HEARTBEAT, 300ms)
  → `agents/passthrough-defender/defender.py` 가 최소 골격

`rehearsal-attacker/attacker/` 는 이 계약을 이미 그대로 구현하므로 수정 없이 붙는다.

## 협업 방식: GHCR 이미지 공유 (기본, 다른 PC 간)

각자 자기 에이전트를 GHCR 에 push 하고, arena 가 pull 한다. **소스를 공유하지
않고** 서로의 최신 이미지로 붙을 수 있다(대회와 동일).

```bash
# 0) 최초 1회: GHCR 로그인 (org 패키지 read/write PAT)
make ghcr-login GH_USER=<github-id> GH_PAT=<PAT>

# 1) .env 에서 원격 모드 켜기
REGISTRY_MODE=remote
REMOTE_REGISTRY=ghcr.io/dah-aexploit

# 2) 각자 자기 에이전트 push
#    방어 담당:
make push-defender TEAM=9 CTX=../defense-agent
#    공격 담당:
make push-attacker TEAM=9 CTX=../rehearsal-attacker/attacker

# 3) arena 운영자(누구든 1명)가 스택 기동 — 라운드마다 최신 이미지를 pull
make up
```

- push 는 즉시 반영이 아니다. arena 는 **라운드 사이 pull 시점**에 최신 이미지를
  가져온다(참가자 페이지 `Next pull in`). 대회와 동일.
- 이미지는 **실행 노드 플랫폼**으로 빌드해야 한다(`NODE_PLATFORM`). 불일치 시
  참가자 페이지에 미반영 경고가 뜬다.
- 팀원은 각자 `make report` / 참가자 페이지로 **같은 스코어보드**를 본다.
  (arena 를 한 사람이 호스팅하고 대시보드 포트를 공유하거나, 각자 자기 PC 에서
  arena 를 띄우고 같은 GHCR 이미지를 pull 한다.)

### 이미지 이름 규칙(GHCR)

```
ghcr.io/dah-aexploit/team9-defender:latest
ghcr.io/dah-aexploit/team9-attacker:latest
```

패키지는 처음 push 후 org 설정에서 팀에 read 권한을 부여한다(비공개 유지 가능).

## 협업 방식: 로컬 경로 (혼자·같은 PC 개발)

세 저장소를 나란히 체크아웃하고 `.env` 를 `REGISTRY_MODE=local`(기본)로 둔다.

```
work/
  arena/            ← 이 저장소 (여기서 make 실행)
  defense-agent/
  rehearsal-attacker/
```

```bash
# config/competition.yml 의 agent_sources 또는 환경변수로 경로 지정
AGENT_SRC_MINE=../defense-agent \
AGENT_SRC_MATE=../rehearsal-attacker/attacker \
make up
```

## 협업 방식: git submodule (커밋 고정 통합)

arena 에 두 에이전트를 submodule 로 붙여 특정 커밋으로 재현 가능한 통합 실행.

```bash
git submodule add git@github.com:DAH-aexploit/defense-agent      vendor/defense-agent
git submodule add git@github.com:DAH-aexploit/rehearsal-attacker vendor/rehearsal-attacker
# config: agent_sources.mine=vendor/defense-agent
#         agent_sources.mate=vendor/rehearsal-attacker/attacker
```

## 역할 배치(누가 무엇을 검증하나)

`config/competition.yml` 의 팀 역할이 매치업을 만든다(기본값):

| 팀 | 방어 | 공격 | 검증 |
|---|---|---|---|
| team9 | 내 방어(mine) | 팀원 공격(mate) | 내 페이지에서 양쪽 동시 |
| team1 | passthrough | bot | 팀원 공격의 순수 능력 |
| team2 | 내 방어 미러(mine) | bot | 팀원 공격이 내 방어를 뚫는지 |

역할·소스는 config 에서 바꾼다. 예를 들어 "팀원 공격 vs 내 방어"만 집중하려면
team2 의 attacker 를 `mate` 로 바꾸면 된다.

## 결과 공유

- `make report` — head-to-head 요약(내 방어 / 팀원 공격 분리)
- 참가자 페이지 — 실시간 스코어보드·verdict·SLA·이미지 반영 상태
- 아티팩트 — 라운드별 pcap·에이전트 로그(파일명 `TEAM9-P2-R8-l1-*.pcap.gz`).
  `rehearsal-attacker/tools/fetch_captures.py` 로 하베스트 가능(파일명 규칙 동일).

## 시크릿 원칙

- 실제 대회 계정·ACR 토큰·LLM 키·GHCR PAT 는 저장소에 넣지 않는다.
- `.env`, `docker-compose.yml`(생성물·LLM 키 박힘), `data/` 는 `.gitignore` 대상.
- arena 팀 토큰은 `ARENA_SALT` 에서 파생된다. 공유 환경에서는 각 팀이 같은
  `ARENA_SALT` 를 `.env` 에 두어야 토큰이 일치한다(기본값은 로컬 전용).
