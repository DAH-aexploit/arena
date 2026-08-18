# DAH Arena (공방 에이전트 테스트베드)

대회와 **같은 계약**(방어 broker · flag 제출 · LLM 게이트웨이 · SLA · 이미지 pull)
위에서 **팀의 방어 에이전트와 공격 에이전트를 head-to-head로 검증**하는 공용
테스트베드다. 챌린지는 리허설 스켈레톤(본선 무관)이다.

## 핵심

방어·공격 에이전트를 **본진(team9)에 함께 배치**하고, 나머지 두 팀은 이 둘을
측정하기 위한 스파링 상대다. 한 판에서 방어와 공격이 **동시에** 드러난다.

| 팀 | 방어 | 공격 | 이 팀의 역할 |
|---|---|---|---|
| **team9 (본진)** | 방어 | 공격 | 방어·공격 성적이 여기서 나온다 |
| team1 (무방비 스파링) | passthrough | 봇 | **공격의 순수 능력** (뚫는 실력) |
| team2 (방어 미러) | 방어 복제 | 봇 | **공격이 방어를 뚫는지** |

- **방어**는 team1·team2의 봇 공격을 받아 성능이 드러난다(team9의 stolen/drop/SLA).
- **공격**은 team1(무방비)·team2(방어 미러)를 때려 순수 능력과 우회 여부가 갈린다.

## 시작

```bash
cp .env.example .env
make up                 # 빌드 → 이미지 시드 → 기동 (라운드 자동 진행)
make creds              # 팀 토큰
# 참가자 페이지: http://localhost:4100  (team9 토큰으로 로그인)
```

## 사용법 (담당자별)

수정 → push → `make report`로 확인, 이 리듬을 반복한다. `RELOAD=1`은 즉시 반영,
빼면 다음 라운드 pull 시점 반영(대회식). `make skip`으로 다음 라운드로 건너뛴다.

### 🛡️ 방어 담당 — `defense-agent`

```bash
make push-defender TEAM=9 CTX=../defense-agent RELOAD=1
```
- **본다**: `make report`의 *방어* — `stolen 없음` + `drop>0` + `SLA` 높음이면 성공.
  team2(미러)가 뚫리면 공격이 찾은 우회가 존재한다는 뜻.
- **판다**: `docker compose logs -f team9-defender` — 어떤 요청을 DROP/ACCEPT 하는지.

### 📐 룰 담당 — `signatures`

룰은 방어 이미지에 포함되어 함께 배포된다(방어 담당과 같은 push 흐름).
```bash
make push-defender TEAM=9 CTX=../defense-agent RELOAD=1   # 갱신된 signatures 포함
```
- **본다**: team9-defender 로그의 `rule_ids` 적중 — 룰이 실제로 잡는가.
- **주의**: 참가자 페이지 **SLA** — 정상 트래픽까지 막으면 SLA가 급락한다(과탐).

### ⚔️ 공격 담당 — `attack-agent`

```bash
make push-attacker TEAM=9 CTX=../attack-agent/agents/team1/attacker RELOAD=1
```
- **본다**: `make report`의 *공격* — team1(무방비) `captured`=순수 능력,
  team2(방어 미러) `captured`=우회 여부(0이면 방어에 막힘, >0이면 뚫음).
- **판다**: `docker compose logs -f team9-attacker` — 어떤 익스플로잇·우회가 통했는지.

## 동작 (요약)

```
공격/ SLA 검사기 → 팀 진입점(gateway) → 방어 broker(ACCEPT/DROP) → 챌린지(flag)
```

- `gateway`가 대회 broker를 재현: 들어오는 요청 패킷을 방어 에이전트에 넘겨 **300ms
  안에** ACCEPT/DROP 판정을 받는다(응답 패킷은 방어자가 보지 않는 inbound-only).
- 라운드는 Phase별로 자동 진행되고, 이미지는 **라운드 사이 pull 시점**에만 반영된다.
  라운드 종료 시 pcap·에이전트 로그가 아티팩트로 남는다.
- 점수 = (공격 + 방어) × SLA. `flags stolen`은 레이어 단위 이진값(한 번만 뚫려도 실패).

## 참고

- **협업·이미지 공유**: [`docs/COLLAB.md`](docs/COLLAB.md) — GHCR / 로컬 경로 / submodule.
  방어·공격은 각자 레포에서 개발하고 아레나가 이미지를 pull 한다.
- **git·커밋**: [`docs/GIT.md`](docs/GIT.md), [`CLAUDE.md`](CLAUDE.md) — Conventional
  Commits, main 직접 커밋 금지(브랜치+PR).
- **공격 계약**: [`agents/ATTACKER_INTERFACE.md`](agents/ATTACKER_INTERFACE.md) — 주입
  env·제출 규약. `attack-agent`는 이 계약을 이미 지켜 수정 없이 붙는다.
- **명령 전체**: `make help`. 주요: `up · down · report · creds · reload · skip · logs · check`.
- **설정**: `config/competition.yml`에서 Phase·팀·역할·라운드 시간을 바꾼 뒤 `make compose`.
- **LLM 실제 키**: `.env`의 `AZURE_API_BASE`/`AZURE_API_KEY`(비우면 mock 동작). 상한은
  `LLM_BUDGET`, 초과 시 `budget-exceeded`(429).
- **이미지 플랫폼**: `NODE_PLATFORM`이 실행 노드와 다르면 push·pull 성공해도 미반영
  경고가 뜬다(리허설의 arm64 미반영 사고 재현).
- **측정 정확도(fail-open)**: 방어 미연결·재기동 중엔 전 패킷 ACCEPT다. 아레나는 라운드
  시작 시 방어 연결을 확인한 뒤 공격을 시작하고, 종료 시 공격을 먼저 멈춰 시작·종료
  레이스를 제거한다(경기 중 방어가 죽으면 그대로 감점 신호).
- **리허설 충실도**: 참가자 페이지·라운드 자동 진행·진입점 rate limit(10/s, burst 20)·
  아티팩트 파일명·Phase별/전역 라운드 번호 표기까지 대회와 동일.
- **요구·검증**: Docker + Compose v2, Python 3(표준 라이브러리만). `make check`로 도커
  없이 핵심 로직 셀프체크.
- **시크릿**: `.env`·`docker-compose.yml`(생성물)·`data/`·`*.pcap`은 커밋하지 않는다.
