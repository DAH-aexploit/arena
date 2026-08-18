"""라운드 자동 진행 엔진.

리허설과 같은 방식이다. 참가자에게 시작·정지 버튼은 없고, Phase 와 Round 가
정해진 시간표대로 흐른다. 이미지는 라운드 사이 pull 시점에만 반영된다.

  break → (pull 시점) 이미지 pull → 라운드 준비 → 라운드 진행 → 종료·집계 → break
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import artifacts, docker_ctl, state
from .registry import ImageSync


def _post(url: str, payload: dict, timeout: int = 10) -> dict | None:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _get(url: str, timeout: int = 5) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


class Scheduler(threading.Thread):
    def __init__(self, cfg, store: state.Store):
        super().__init__(daemon=True, name="scheduler")
        self.cfg = cfg
        self.store = store
        self.images = ImageSync(cfg, store)
        self._stop = threading.Event()
        self._skip = threading.Event()

    # ── 외부 제어 ────────────────────────────────────────────────────────
    def stop(self) -> None:
        self._stop.set()

    def skip_round(self) -> None:
        """운영자용 — 남은 대기/라운드를 즉시 넘긴다."""
        self._skip.set()

    def reload_agent(self, team_id: int, role: str) -> dict:
        """테스트 편의 — 라운드 경계를 기다리지 않고 지금 pull·재생성한다.

        대회 절차(라운드 사이 pull)와 달리 즉시 반영한다. 플랫폼 불일치는
        여전히 존중되어 미반영 경고가 뜬다.
        """
        team = self.cfg.team(team_id)
        if team is None:
            return {"ok": False, "error": f"unknown team {team_id}"}
        roles = ["defender", "attacker"] if role in ("both", "*", "") else [role]
        result = {}
        for r in roles:
            if r not in ("defender", "attacker"):
                result[r] = {"ok": False, "error": "bad role"}
                continue
            slot = self.images.pull(team, r)
            svc = f"{team.slug}-{r}"
            out = docker_ctl.recreate([svc])
            result[r] = {"applied": slot.get("applied"), "warning": slot.get("warning"),
                         "recreate": out[-200:] if out else ""}
        self.store.log(f"team{team_id} {'/'.join(roles)} 즉시 reload")
        return {"ok": True, "team": team_id, "roles": result}

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            if self._skip.is_set():
                self._skip.clear()
                return
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(1.0, left))

    # ── 인프라 조작 ──────────────────────────────────────────────────────
    def _gateway_url(self, team, path: str) -> str:
        return f"http://{team.slug}-gateway:9090{path}"

    def _inject_flags(self, layers: list[int]) -> int:
        issued = self.store.rotate_flags(self.cfg.teams, layers)
        count = 0
        for (team_id, layer), flag in issued.items():
            team = self.cfg.team(team_id)
            if team is None:
                continue
            if layer == 1:
                service, path = f"{team.slug}-helper-1", "/flags/LAYER2_CHALL1_FLAG"
            else:
                service = f"{team.slug}-layer-{layer}"
                path = f"/flags/LAYER{layer + 1}_CHALL1_FLAG"
            out = docker_ctl.exec_in(
                service, ["sh", "-c", f"mkdir -p /flags && printf %s '{flag}' > {path}"])
            if out is not None:
                count += 1
            if layer == 3:  # sqlite in-memory 로 읽는 챌린지는 재기동이 필요
                docker_ctl.exec_in(service, ["sh", "-c", "kill -HUP 1 2>/dev/null || true"])
        return count

    def _start_gateways(self, tag: str, layers: list[int]) -> None:
        for team in self.cfg.teams:
            _post(self._gateway_url(team, "/control/round"),
                  {"tag": tag, "active_layers": layers})

    def _stop_gateways(self) -> None:
        for team in self.cfg.teams:
            _post(self._gateway_url(team, "/control/stop"), {})

    def _defender_services(self) -> list[str]:
        return [f"{t.slug}-defender" for t in self.cfg.teams]

    def _attacker_services(self) -> list[str]:
        return [f"{t.slug}-attacker" for t in self.cfg.teams]

    def _agent_services(self) -> list[str]:
        return self._defender_services() + self._attacker_services()

    def _await_defenders(self, timeout: float) -> dict:
        """모든 팀 방어 에이전트가 broker 에 연결될 때까지 대기.

        라운드 시작 시 방어 재기동 중 fail-open 창으로 공격이 새는 것을 막아,
        탈취가 시작 레이스가 아닌 실제 방어 우회를 반영하도록 한다.
        """
        deadline = time.time() + timeout
        pending = {t.id for t in self.cfg.teams}
        while pending and time.time() < deadline and not self._stop.is_set():
            for team in list(self.cfg.teams):
                if team.id not in pending:
                    continue
                snap = _get(self._gateway_url(team, "/stats"))
                if snap and snap.get("agent_connected"):
                    pending.discard(team.id)
            if pending:
                time.sleep(0.5)
        return {"connected": [t.id for t in self.cfg.teams if t.id not in pending],
                "pending": sorted(pending)}

    def _poll_round(self, until: float) -> None:
        """라운드 중 지표 수집."""
        while not self._stop.is_set() and time.time() < until:
            if self._skip.is_set():
                self._skip.clear()
                return
            for team in self.cfg.teams:
                snap = _get(self._gateway_url(team, "/stats"))
                if snap:
                    self.store.record_gateway(team.id, snap)
                self.store.record_restarts(
                    team.id, docker_ctl.restart_count(f"{team.slug}-defender"))
            time.sleep(5)

    # ── 라운드 ───────────────────────────────────────────────────────────
    def _run_round(self, global_round: int) -> None:
        cfg, store = self.cfg, self.store
        tag = cfg.round_tag(global_round)
        layers = cfg.active_layers(global_round)
        found = cfg.locate(global_round)
        phase_label = f"{found[0].name} Round {found[1]}" if found else f"Round {global_round}"

        store.phase_state = state.PREP
        store.log(f"{phase_label} 준비 — 활성 레이어 {layers}")

        # 1) 방어 먼저 기동하고 broker 연결을 확인한다(시작 fail-open 창 제거).
        self._start_gateways(tag, layers)
        docker_ctl.recreate(self._defender_services())
        warmup = float(os.environ.get("DEFENDER_WARMUP_SECONDS", "20"))
        ready = self._await_defenders(warmup)
        if ready["pending"]:
            store.log(f"{phase_label} 경고 — 방어 미연결 team{ready['pending']} "
                      "(해당 팀은 fail-open 상태로 시작)")

        # 2) 방어가 준비된 뒤 flag 주입·공격 시작 → 탈취는 실제 우회만 반영
        injected = self._inject_flags(layers)
        docker_ctl.recreate(self._attacker_services())

        store.begin_round(global_round, cfg.round_seconds)
        store.log(f"{phase_label} 시작 (flag {injected}건 주입, 방어연결 "
                  f"{len(ready['connected'])}/{len(cfg.teams)}, tag {tag})")

        self._poll_round(store.round_ends)

        # 종료 순서가 중요: 공격을 먼저 멈추고 제출창을 닫은 뒤 방어를 내린다.
        # (방어를 먼저 내리면 그 fail-open 창에서 아직 살아있는 공격이 통과한다)
        docker_ctl.stop(self._attacker_services())
        summary = store.end_round()
        docker_ctl.stop(self._defender_services())
        self._stop_gateways()
        artifacts.collect(cfg, store, global_round, store.round_started)
        stolen = {tid: row["stolen"] for tid, row in summary["teams"].items()}
        store.log(f"{phase_label} 종료 — flags stolen {stolen}")
        store.save()

    def _break_until(self, seconds: int, next_round: int) -> None:
        cfg, store = self.cfg, self.store
        store.phase_state = state.BREAK
        started = time.time()
        store.next_round_at = started + seconds
        lead = min(cfg.pull_lead_seconds, max(5, seconds - 5))
        store.next_pull_at = store.next_round_at - lead

        # pull 시점까지 대기하면서 레지스트리 push 상태만 갱신
        while not self._stop.is_set() and time.time() < store.next_pull_at:
            if self._skip.is_set():
                break
            for team in cfg.participants:
                for role in ("defender", "attacker"):
                    self.images.observe(team, role)
            time.sleep(min(5.0, max(0.5, store.next_pull_at - time.time())))

        store.log(f"이미지 pull 시점 — Round {next_round} 반영 대상 확인")
        for team in cfg.participants:
            for role in ("defender", "attacker"):
                self.images.pull(team, role)
        store.next_pull_at = 0.0
        store.save()

        remain = store.next_round_at - time.time()
        if remain > 0:
            self._sleep(remain)

    # ── 메인 루프 ────────────────────────────────────────────────────────
    def run(self) -> None:
        cfg, store = self.cfg, self.store
        if not cfg.autostart:
            store.log("AUTOSTART=0 — 운영자 시작 대기")
            while not self._stop.is_set() and not self._skip.is_set():
                time.sleep(1)
            self._skip.clear()

        loop = os.environ.get("LOOP", "0") == "1"
        mode = "연속 루프" if loop else f"전체 {cfg.total_rounds} 라운드"
        store.log(f"{cfg.name} 시작 — {mode}")
        self._break_until(min(60, cfg.break_seconds), 1)

        start_from = max(1, store.global_round + 1)
        if start_from > cfg.total_rounds:  # 재시작 시 완주 상태면 처음부터
            start_from = 1

        while not self._stop.is_set():
            for global_round in range(start_from, cfg.total_rounds + 1):
                if self._stop.is_set():
                    break
                self._run_round(global_round)
                last = global_round >= cfg.total_rounds
                if not last or loop:
                    nxt = 1 if last else global_round + 1
                    self._break_until(cfg.break_seconds, nxt)
            if not loop:
                break
            start_from = 1
            store.log("전체 라운드 완료 — 연속 루프로 처음부터 재시작")

        store.finish()
        store.log("대회 종료")
        store.save()
