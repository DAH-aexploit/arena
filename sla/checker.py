#!/usr/bin/env python3
"""SLA 검사기 — 정상 트래픽 생성기.

리허설 조건을 따른다.

  - 라운드당 전체 서비스 합계 100회 점검
  - 방어 broker 를 통과하는 정상 요청(공격 에이전트와 같은 진입점 경유)
  - 성공 판정 기준은 참가팀에 비공개지만, 여기서는 각 레이어의 정상 경로가
    2xx/기대 응답을 돌려주는지로 판정한다
  - 결과는 오케스트레이터로 보고되어 점수 곱셈 계수(SLA 비율)가 된다

방어 에이전트가 정상 트래픽까지 DROP 하면 이 검사가 실패하도록 설계되어,
과도한 광역 차단을 억제하는 압력으로 작동한다.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request

ORCH = os.environ.get("ORCH_URL", "http://orchestrator:4100").rstrip("/")
INTERVAL = float(os.environ.get("SLA_INTERVAL", "2"))
TIMEOUT = float(os.environ.get("SLA_TIMEOUT", "5"))

# 각 레이어의 정상 경로. 공격 경로(/fetch loopback, admin 세션, sqli)는 제외.
PROBES = {
    # 내부망이라 외부 URL fetch 는 불가하므로, L1 정상 트래픽은 배너·health 로
    # 한정한다(공격 경로인 /fetch loopback·registry 는 제외).
    1: [
        ("GET", "/", None, lambda b: "Fetcher" in b or "URL" in b),
        ("GET", "/health", None, lambda b: "ok" in b),
    ],
    2: [
        ("GET", "/", None, lambda b: "Portal" in b or "Admin" in b),
        ("GET", "/health", None, lambda b: "ok" in b),
        ("GET", "/login", None, lambda b: True),
    ],
    3: [
        ("GET", "/", None, lambda b: "Shop" in b),
        ("GET", "/health", None, lambda b: "ok" in b),
        ("GET", "/product?id=1", None, lambda b: "Widget" in b or "|" in b),
    ],
}

USER_AGENT = os.environ.get("SLA_USER_AGENT", "lig-sla/1.0")


def get_plan() -> dict | None:
    try:
        with urllib.request.urlopen(f"{ORCH}/api/sla/plan", timeout=TIMEOUT) as r:
            return json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def report(team: int, ok: int, total: int) -> None:
    body = json.dumps({"team": team, "ok": ok, "total": total}).encode()
    req = urllib.request.Request(f"{ORCH}/api/sla/report", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT).read()
    except (urllib.error.URLError, OSError):
        pass


def probe(host: str, port: int, layer: int) -> bool:
    method, path, _body, check = random.choice(PROBES.get(layer, PROBES[1]))
    url = f"http://{host}:{port}{path}"
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            text = r.read(65536).decode("latin-1")
            return 200 <= r.status < 400 and check(text)
    except urllib.error.HTTPError as e:
        return 200 <= e.code < 400
    except (urllib.error.URLError, OSError):
        return False


def main() -> None:
    print(f"[sla] checker 시작 — orchestrator {ORCH}", flush=True)
    layer_port = {1: 8082, 2: 8083, 3: 8084}
    current_round = None
    pace = INTERVAL
    accum: dict[int, list[int]] = {}  # team -> [ok, total] 미보고 누적

    while True:
        plan = get_plan()
        if not plan or plan.get("state") != "round":
            current_round = None
            time.sleep(INTERVAL)
            continue

        rnd = plan.get("round")
        layers = plan.get("active_layers") or [1]
        targets = plan.get("targets") or []
        checks = int(plan.get("checks_per_round", 100))
        remaining = max(1, int(plan.get("remaining", 1)))

        if rnd != current_round:
            current_round = rnd
            accum = {t["team"]: [0, 0] for t in targets}
            # 라운드당 총 checks 회를 남은 시간에 고르게 분산한다. 한 소스에서
            # 진입점 rate limit(10/s)을 넘지 않도록 페이스를 확보한다.
            pace = max(0.3, remaining / max(1, checks))
            print(f"[sla] Round {rnd} — 서비스 합계 {checks}회 점검 "
                  f"(활성 레이어 {layers}, pace {pace:.2f}s)", flush=True)

        # 한 틱에 팀 하나×레이어 하나만 점검(전체를 라운드 내내 얇게 분산)
        for target in targets:
            team = target["team"]
            if not (target.get("ports") or []):
                continue
            layer = random.choice(layers)
            ok = probe(target["host"], layer_port.get(layer, 8082), layer)
            slot = accum.setdefault(team, [0, 0])
            slot[0] += 1 if ok else 0
            slot[1] += 1
            # 소량씩 즉시 보고(대시보드 실시간 반영)
            report(team, 1 if ok else 0, 1)

        time.sleep(pace)


if __name__ == "__main__":
    main()
