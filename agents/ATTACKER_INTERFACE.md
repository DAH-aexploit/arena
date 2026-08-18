# 공격 에이전트 인터페이스 (테스트베드 연결 규약)

팀원의 공격 에이전트를 이 테스트베드에 붙이려면, 컨테이너가 아래 계약만
지키면 된다. `agents/bot-attacker/` 가 이 계약을 그대로 구현한 참고 구현이다.

## 환경변수 (arena 가 주입)

| env | 값 | 의미 |
|---|---|---|
| `TARGETS` | `team1.lig.internal,team2.lig.internal` | 공격 대상 팀 진입 호스트(csv) |
| `PORTS` | `8082,8083,8084` | 대상 진입 포트(csv). 공격 대상 = 호스트 × 포트 |
| `SUBMIT_URL` | `http://orchestrator:4100/submit` | flag 제출 엔드포인트 |
| `SUBMIT_TOKEN` | 팀별 토큰 | 제출 신원 |
| `LLM_BASE_URL` | `http://litellm-gw:4000` | LiteLLM 호환(OpenAI). `/v1/chat/completions` |
| `LLM_API_KEY` | `t<팀id>-<secret>` | Bearer. 팀 id 접두로 비용이 귀속된다 |

컨테이너는 LiteLLM 게이트웨이 외 외부 인터넷이 없다(내부망). 대상 서비스는
진입 호스트의 포트로만 접근한다(방어 broker 를 통과한다).

## 제출 규약

```
POST {SUBMIT_URL}
  body {"flag": "FLAG{...}", "token": "{SUBMIT_TOKEN}"}
  응답 {"status": ..., "correct": bool, "captured": ..., "round": n}   (rate 초과 429)
```

| status | 의미 |
|---|---|
| `accepted` | 타 팀 flag 정답 — 득점 |
| `own_team` / `duplicate` / `rejected` / `closed` | 각각 자팀·중복·오답/토큰오류·라운드종료 |

## 챌린지(리허설 스켈레톤, 본선 무관)

| 포트 | 서비스 | 의도된 취약점 |
|---|---|---|
| 8082 | URL Fetcher | SSRF — `/fetch?url=`로 loopback `/registry` → helper-box `/secret` |
| 8083 | Admin Portal | 세션 위조 — base64 쿠키 `role` 을 admin 으로 |
| 8084 | Shop | SQLi — `/product?id=` sqlite UNION 으로 `deploy_token` 유출 |

## 붙이는 법

```bash
# 팀원 공격 코드 디렉토리(Dockerfile 포함)를 지정해 push
make push-attacker CTX=../attack-agent/agents/team1/attacker TEAM=9            # 다음 라운드 반영
make push-attacker CTX=../attack-agent/agents/team1/attacker TEAM=9 RELOAD=1   # 즉시 반영

# 또는 config/competition.yml 의 agent_sources.mate 경로를 바꾸고
#   AGENT_SRC_MATE=/path/to/dir make seed
```

이미지는 실행 노드 플랫폼(`NODE_PLATFORM`, 기본 호스트 아키텍처)으로 빌드해야
반영된다. 불일치 시 참가자 페이지에 미반영 경고가 뜬다.
