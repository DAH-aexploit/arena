# DAH Arena — 공방 에이전트 테스트베드 (리허설 환경 재현)

`rade.dev.ctf-zone.com` 참가자 페이지와 DAH 2026 리허설(2026-08-15) 인프라를
로컬에서 재현한 공방전 환경이다. 참가자 페이지, 라운드 자동 진행, 방어 broker,
이미지 push→pull→반영, SLA, 아티팩트까지 대회와 같은 인터페이스로 동작한다.

**용도: 내 방어 에이전트와 팀원의 공격 에이전트를 대회와 같은 계약으로
head-to-head 테스트한다.** 챌린지는 리허설 스켈레톤과 동일한 테스트용이며 본선과
무관하다.

## 테스트베드 구성 (head-to-head)

역할 기반으로 3팀이 배치된다(`config/competition.yml`).

| 팀 | 방어 | 공격 | 이 팀이 검증하는 것 |
|---|---|---|---|
| **team9 (나)** | 내 방어(`../defense-agent`) | 팀원 공격(`../attack-agent`) | 내 페이지에서 방어·공격 성적을 동시에 본다 |
| team1 (무방비 스파링) | passthrough(ACCEPT-all) | 봇 | 팀원 공격의 **순수 익스플로잇 능력** |
| team2 (내 방어 미러) | 내 방어 복제 | 봇 | 팀원 공격이 **내 방어를 뚫는지** |

공격은 자기 팀의 모든 상대를 향하므로, 한 판에서 아래가 동시에 측정된다.

- **내 방어 성능**: team1·team2 의 봇 공격이 team9 를 때린다 → team9 의 stolen/drop/SLA
- **팀원 공격 능력**: team9 의 팀원 공격이 team1(무방비)·team2(내 방어)를 때린다
  → team1 captured(순수 능력) vs team2 captured(내 방어 우회 여부)

`make report` 가 이 둘을 나눠서 보여준다.

> **팀 협업**: 방어·공격 에이전트는 각자 저장소에서 개발하고, arena 는 그 이미지를
> pull 해 붙인다. GHCR 이미지 공유·로컬 경로·submodule 방식은
> [`docs/COLLAB.md`](docs/COLLAB.md) 참고.

## 무엇이 리허설과 같은가

| 항목 | 재현 방식 |
|---|---|
| 참가자 페이지 | Phase·Round 진행, `Next pull in`, 진입점, SLA, verdict, Flags captured/stolen, 아티팩트, LLM 비용 |
| 라운드 자동 진행 | 시작·정지 버튼 없음. Phase가 오를수록 활성 레이어 증가. 리허설: Round 20분 / Break 20분 |
| 이미지 반영 | push 즉시 반영 아님. 라운드 사이 **pull 시점**에만 반영 |
| 방어 broker 계약 | `docs`의 PACKET/VERDICT/HEARTBEAT, 300ms verdict, 1s heartbeat, fail-open |
| inbound-only | 방어 에이전트는 외부→서비스 요청 패킷만 관찰. 응답은 관찰 범위 밖 |
| 진입점 rate limit | source IP 기준 신규 TCP 연결 10/s, burst 20 (팀 전체 공유) |
| 아티팩트 파일명 | `TEAM9-P1-R1-l1-<ts>.pcap.gz`, `TEAM9-P1-R1-defender.log.gz` |
| Phase별/전역 라운드 번호 | 페이지는 Phase별(P2-R1), 파일명은 전역(R7). 대시보드에 둘 다 표기 |
| LLM 게이트웨이 | LiteLLM 호환, drop_params, hosted-tools 차단, budget-exceeded, usage 토큰 |
| 플랫폼 불일치 사고 | arm64/amd64 불일치 시 pull 미반영을 **경고로 노출** (리허설 4라운드 미반영 재현) |

## 요구 사항

- Docker + Docker Compose v2
- Python 3 (compose 생성·셀프체크용, 외부 패키지 불필요)
- Azure/OpenAI 키는 **선택**. 없으면 LLM 게이트웨이가 목(mock)으로 동작

## 빠른 시작

```bash
cd dah-arena
cp .env.example .env          # 짧은 라운드·연속 루프가 기본
make up                        # compose 생성 → 역할별 이미지 시드 → 기동
make creds                     # 팀 토큰 확인
open http://localhost:4100/competitions/a0000000-0000-4000-8000-000000000001
```

기동 후 자동으로 라운드가 진행되고(기본 `LOOP=1` 로 16라운드 후 처음부터 반복),
team9 는 `../defense-agent`, team2 는 그 복제, team1 은 passthrough 로 시작한다.
팀원 공격(`../attack-agent`)이 없으면 team9 공격은 봇으로 대체된다.

## 내 에이전트 붙이기

```bash
# 내 방어 (기본 CTX=../defense-agent)
make push-defender TEAM=9                       # 다음 라운드 pull 시점 반영
make push-defender TEAM=9 RELOAD=1              # 즉시 반영

# 팀원 공격 (팀원 코드 디렉토리를 CTX 로)
make push-attacker CTX=/path/to/mate-attacker TEAM=9 RELOAD=1

# 팀원 공격 코드 위치를 고정하려면 config/competition.yml 의 agent_sources.mate
# 경로를 바꾸거나 AGENT_SRC_MATE 환경변수로 지정한 뒤 make seed
```

공격 에이전트가 지켜야 할 환경변수·제출 규약은
[`agents/ATTACKER_INTERFACE.md`](agents/ATTACKER_INTERFACE.md) 참고.
빌드 플랫폼(`NODE_PLATFORM`, 기본 호스트 아키텍처)이 실행 노드와 다르면
push·pull 이 성공해도 반영되지 않고 참가자 페이지에 경고가 뜬다.

## 명령어

| 명령 | 설명 |
|---|---|
| `make up` | compose 생성 + 역할별 이미지 시드 + 전체 기동 |
| `make down` | 스택 중지 및 볼륨 삭제 |
| `make creds` | 팀 토큰·계정 출력 |
| `make report` | **테스트베드 결과 요약(head-to-head)** |
| `make push-defender TEAM=9 [CTX=.. RELOAD=1]` | 내 방어 재빌드·push (+즉시 반영) |
| `make push-attacker CTX=.. TEAM=9 [RELOAD=1]` | 팀원 공격 재빌드·push (+즉시 반영) |
| `make reload TEAM=9 [ROLE=defender]` | 이미 push된 이미지를 즉시 pull·재생성 |
| `make skip` | 현재 대기/라운드를 즉시 넘김(빠른 반복) |
| `make logs` / `make status` | 오케스트레이터 로그 / 상태(JSON) |
| `make compose` | `config/competition.yml` → `docker-compose.yml` 재생성 |
| `make check` | 도커 없이 핵심 로직 셀프체크 |

### 반복(iteration) 두 가지 모드

- **즉시 반영**(`RELOAD=1` 또는 `make reload`): 코드 수정 → push → 지금 pull·재생성.
  라운드 경계를 기다리지 않는다. 빠른 디버깅용.
- **대회와 동일**(`RELOAD` 없음): push 후 다음 라운드 pull 시점에만 반영. 대회
  배포 절차(라운드 사이 pull) 연습용. `LOOP=1` 이라 라운드는 계속 돈다.

## 방어 에이전트 배포 흐름 (대회와 동일)

1. 방어 에이전트를 작성한다. broker 계약은 `agents/passthrough-defender/defender.py`가
   최소 골격이다(`make_verdict`만 교체하면 된다).
2. 이미지를 **실행 노드 플랫폼으로** 빌드해 레지스트리에 push 한다.

   ```bash
   make push-defender CTX=../defense-agent TEAM=9
   # 내부적으로:
   #   docker buildx build --platform $NODE_PLATFORM --provenance=false --sbom=false \
   #     -t localhost:5001/team9/defender:latest --push .
   #   docker pull / inspect 로 아키텍처 검증
   ```

3. 참가자 페이지의 **이미지 배포 상태**에서 다음 `Next pull in` 이후 반영을 확인한다.
   플랫폼이 실행 노드와 다르면 push·pull이 성공해도 **미반영 경고**가 뜬다
   (리허설의 arm64 4라운드 미반영 사고 재현).

## 구성

```
orchestrator/   참가자 페이지 + 라운드 자동 진행 + 채점 + 레지스트리 pull
  core/         config·state·scheduler·registry·docker_ctl·artifacts·miniyaml
  web/static/   대시보드(login·dashboard·app.js·style.css)
gateway/        팀 진입점 + 방어 broker(userspace). NFQUEUE 없이 broker 계약 재현
challenges/     리허설 스켈레톤 3종(L1 SSRF · L2 세션위조 · L3 SQLi) + helper-box
agents/
  bot-attacker/         결정적 봇 공격(3종 익스플로잇, LLM 불필요)
  passthrough-defender/ 봇·초기 방어 기준선(ACCEPT-all) 겸 방어 골격
sla/            SLA 검사기(정상 트래픽, 라운드당 100회, 점수 곱셈 계수)
litellm-gw/     LiteLLM 호환 게이트웨이(mock/upstream), budget·usage·hosted-tools
config/         competition.yml (Phase·팀·레이어·역할·레지스트리·SLA 정의)
tools/          gen_compose · seed · push_agent · report · selfcheck
```

역할 소스: `mine`=`../defense-agent`, `mate`=`../attack-agent`,
`passthrough`/`bot`=내장. `AGENT_SRC_<ROLE>` 환경변수로 경로를 덮어쓴다.

## broker 없이 macOS에서 되는 이유

방어 broker는 대회 인프라의 `NFQUEUE + broker 바이너리`를 커널 기능 없이
재현한 **userspace gateway**다. 진입점 TCP 연결을 받아 요청 방향 세그먼트를
raw IPv4/TCP 패킷으로 합성한 뒤, `AF_UNIX/SOCK_SEQPACKET`으로 방어 에이전트에
`docs`와 동일한 프레임으로 전달하고 verdict(ACCEPT/DROP)를 적용한다. 따라서
`nfnetlink_queue`가 없는 호스트에서도 대회와 같은 계약으로 방어 에이전트를
구동·검증할 수 있다(단 SEQPACKET은 Linux 컨테이너 전용이라 gateway는 컨테이너에서
실행한다). 리눅스 호스트에서 실제 NFQUEUE broker로 바꾸려면 `deploy/router`의
broker 바이너리를 gateway 자리에 넣으면 된다.

## LLM (실제 키 연결)

에이전트가 실제 LLM 을 쓰면 `.env` 에 upstream 을 채운다. 비우면 `litellm-gw`가
목(mock)으로 동작해 키 없이도 인프라가 돈다.

```bash
# Azure Foundry (endpoint 뒤에 /openai/v1)
AZURE_API_BASE=https://<project>.openai.azure.com/openai/v1
AZURE_API_KEY=<key>
# 또는 OpenAI 호환 upstream
LLM_UPSTREAM_BASE=... 
LLM_UPSTREAM_KEY=...
```

- 에이전트는 `LLM_BASE_URL=http://litellm-gw:4000` / `LLM_API_KEY=t<팀id>-<secret>`
  를 주입받아 `POST /v1/chat/completions`(Bearer)로 호출한다.
- 게이트웨이가 `drop_params`, hosted-tools 차단, budget-exceeded, usage 토큰을
  대회와 같은 계약으로 처리하고, 팀별 누적 비용을 참가자 페이지에 반영한다.
- budget 은 `.env` 의 `LLM_BUDGET`(기본 20 USD). 상한 도달 시 요청은
  `budget-exceeded`(429)로 거부된다.

## 라운드 경계와 fail-open

방어 에이전트가 broker 에 연결되기 전(재기동 중)이나 연결이 끊긴 동안 broker 는
fail-open(전 패킷 ACCEPT)이다. 이는 대회와 동일한 안전장치지만, 테스트베드에서
방어 **품질**을 재려면 이 창에 공격이 새면 안 된다. 그래서 오케스트레이터는
라운드 시작 시 **방어 연결을 확인한 뒤** flag 를 주입·공격을 시작하고, 종료 시
**공격을 먼저 멈추고 제출창을 닫은 뒤** 방어를 내린다. 덕분에 탈취는 시작·종료
레이스가 아닌 실제 방어 우회만 반영한다. (경기 도중 방어가 죽으면 fail-open 이
적용되어 그 자체가 감점 신호가 된다.)

## 설정 바꾸기

`config/competition.yml`에서 Phase 구성·팀·레이어·역할(`defender`/`attacker`)·
`agent_sources` 경로·라운드 시간을 바꾼 뒤 `make compose`로 compose를 재생성한다.
`.env`가 라운드 시간·플랫폼·budget·`LOOP`을 덮어쓴다.

## 주의

- 챌린지·토큰은 리허설/본선과 무관한 로컬 테스트용이다.
- 실제 대회 계정 비밀번호·ACR 토큰·LLM 키는 이 저장소에 두지 않는다.
- 로컬 레지스트리(`localhost:5001`)는 도커가 insecure로 자동 처리한다.
```
