"""대회 상태 · 채점.

지표 정의는 리허설에서 실제로 확인된 해석을 따른다.

  Flags captured      공격 에이전트가 다른 팀에서 획득해 제출에 성공한 기록
  Flags stolen        다른 팀이 이 팀 flag 를 가져간 방어 실패 지표.
                      라운드·레이어 단위 이진값이라 DROP 수천 건과 연동되지 않는다.
                      한 라운드에 한 건만 통과해도 그 레이어는 실패로 집계된다.
  SLA                 라운드당 전체 서비스 합계 100회 점검의 성공률(점수 곱셈 계수)
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections import deque

STATE_PATH = os.environ.get("STATE_PATH", "/data/state.json")

IDLE, PREP, ROUND, BREAK, FINISHED = "idle", "prep", "round", "break", "finished"

ATTACK_POINTS = 100
DEFENSE_POINTS = 100


def now() -> float:
    return time.time()


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.phase_state = IDLE
        self.global_round = 0
        self.round_started = 0.0
        self.round_ends = 0.0
        self.next_pull_at = 0.0
        self.next_round_at = 0.0
        self.started_at = now()

        # flag → (team_id, layer)
        self.flag_owner: dict[str, tuple[int, int]] = {}
        # (round, by_team, victim_team, layer) 중복 제출 차단
        self._capture_keys: set = set()

        self.captures: list[dict] = []           # 전 팀의 flag 획득 기록
        self.rounds: list[dict] = []             # 라운드별 요약
        self.round_stats: dict = {}              # 진행 중 라운드의 팀별 집계
        self.images: dict = {}                   # 팀별 이미지 반영 상태
        self.llm_cost: dict = {}                 # 팀별 누적 LLM 비용
        self.artifacts: list[dict] = []
        self.events = deque(maxlen=400)
        self.submit_window: dict = {}

        for team in cfg.teams:
            self.images[team.id] = {
                "defender": self._blank_image(),
                "attacker": self._blank_image(),
            }
            self.llm_cost[team.id] = 0.0
        self._reset_round_stats()

    # ── 내부 ─────────────────────────────────────────────────────────────
    @staticmethod
    def _blank_image() -> dict:
        return {"pushed_at": None, "pulled_at": None, "digest": None,
                "running_digest": None, "platform": None, "applied": False,
                "warning": None}

    def _reset_round_stats(self) -> None:
        self.round_stats = {
            t.id: {
                "sla_ok": 0, "sla_total": 0,
                "accept": 0, "drop": 0, "timeout": 0, "packets": 0,
                "stolen_layers": [], "captured": 0,
                "agent_connected": False, "restarts": 0,
            } for t in self.cfg.teams
        }

    def log(self, message: str) -> None:
        entry = {"ts": now(), "message": message}
        with self.lock:
            self.events.appendleft(entry)
        print(f"[arena] {time.strftime('%H:%M:%S')} {message}", flush=True)

    # ── 라운드 생명주기 ──────────────────────────────────────────────────
    def begin_round(self, global_round: int, duration: int) -> None:
        with self.lock:
            self.global_round = global_round
            self.phase_state = ROUND
            self.round_started = now()
            self.round_ends = self.round_started + duration
            self._capture_keys = {k for k in self._capture_keys if k[0] == global_round}
            self._reset_round_stats()

    def end_round(self) -> dict:
        with self.lock:
            summary = {
                "round": self.global_round,
                "tag": self.cfg.round_tag(self.global_round),
                "ended_at": now(),
                "teams": {},
            }
            for team in self.cfg.teams:
                s = self.round_stats[team.id]
                active = self.cfg.active_layers(self.global_round)
                stolen = sorted(set(s["stolen_layers"]))
                sla_ratio = (s["sla_ok"] / s["sla_total"]) if s["sla_total"] else 1.0
                defended = [l for l in active if l not in stolen]
                score = (s["captured"] * ATTACK_POINTS
                         + len(defended) * DEFENSE_POINTS) * sla_ratio
                summary["teams"][team.id] = {
                    "captured": s["captured"],
                    "stolen": len(stolen),
                    "stolen_layers": stolen,
                    "defended_layers": defended,
                    "sla_ok": s["sla_ok"], "sla_total": s["sla_total"],
                    "sla_ratio": round(sla_ratio, 4),
                    "accept": s["accept"], "drop": s["drop"], "timeout": s["timeout"],
                    "score": round(score, 1),
                }
            self.rounds.append(summary)
            self.phase_state = BREAK
            return summary

    def finish(self) -> None:
        with self.lock:
            self.phase_state = FINISHED

    # ── flag ────────────────────────────────────────────────────────────
    def rotate_flags(self, teams, layers) -> dict:
        """라운드 시작 시 팀×레이어 flag 재발급."""
        issued = {}
        with self.lock:
            self.flag_owner.clear()
            for team in teams:
                for layer in layers:
                    flag = f"FLAG{{{secrets.token_hex(16)}}}"
                    self.flag_owner[flag] = (team.id, layer)
                    issued[(team.id, layer)] = flag
        return issued

    def submit(self, flag: str, team_id: int | None, rate_limit: int = 30,
               window: int = 60) -> dict:
        ts = now()
        with self.lock:
            if team_id is None:
                return {"status": "rejected", "correct": False, "captured": None,
                        "round": self.global_round, "_code": 200}
            dq = self.submit_window.setdefault(team_id, deque())
            while dq and ts - dq[0] > window:
                dq.popleft()
            if len(dq) >= rate_limit:
                return {"status": "rate_limited", "correct": False, "captured": None,
                        "round": self.global_round, "_code": 429}
            dq.append(ts)

            if self.phase_state != ROUND:
                return {"status": "closed", "correct": False, "captured": None,
                        "round": self.global_round, "_code": 200}
            owner = self.flag_owner.get(flag)
            if owner is None:
                return {"status": "rejected", "correct": False, "captured": None,
                        "round": self.global_round, "_code": 200}
            victim, layer = owner
            if victim == team_id:
                return {"status": "own_team", "correct": False, "captured": None,
                        "round": self.global_round, "_code": 200}
            key = (self.global_round, team_id, victim, layer)
            captured = {"by": team_id, "victim": victim, "layer": layer,
                        "round": self.global_round}
            if key in self._capture_keys:
                return {"status": "duplicate", "correct": False, "captured": captured,
                        "round": self.global_round, "_code": 200}
            self._capture_keys.add(key)
            self.captures.append({**captured, "ts": ts})
            self.round_stats[team_id]["captured"] += 1
            victim_stats = self.round_stats[victim]
            if layer not in victim_stats["stolen_layers"]:
                victim_stats["stolen_layers"].append(layer)
        self.log(f"CAPTURE team{team_id} → team{victim} L{layer}")
        return {"status": "accepted", "correct": True, "captured": captured,
                "round": self.global_round, "_code": 200}

    # ── 지표 반영 ───────────────────────────────────────────────────────
    def record_gateway(self, team_id: int, snap: dict) -> None:
        with self.lock:
            s = self.round_stats.get(team_id)
            if s is None:
                return
            s["accept"] = snap.get("accept", 0)
            s["drop"] = snap.get("drop", 0)
            s["timeout"] = snap.get("timeout", 0)
            s["packets"] = snap.get("packets", 0)
            s["agent_connected"] = bool(snap.get("agent_connected"))
            s["per_port"] = snap.get("per_port", {})

    def record_sla(self, team_id: int, ok: int, total: int) -> None:
        with self.lock:
            s = self.round_stats.get(team_id)
            if s is None:
                return
            s["sla_ok"] += ok
            s["sla_total"] += total

    def record_restarts(self, team_id: int, count: int) -> None:
        with self.lock:
            s = self.round_stats.get(team_id)
            if s is not None:
                s["restarts"] = count

    def add_llm_cost(self, team_id: int, usd: float) -> None:
        with self.lock:
            self.llm_cost[team_id] = self.llm_cost.get(team_id, 0.0) + usd

    def add_artifacts(self, entries: list[dict]) -> None:
        with self.lock:
            self.artifacts.extend(entries)

    # ── 조회 ────────────────────────────────────────────────────────────
    def totals(self) -> dict:
        with self.lock:
            out = {t.id: {"score": 0.0, "captured": 0, "stolen": 0,
                          "sla_ok": 0, "sla_total": 0} for t in self.cfg.teams}
            for rnd in self.rounds:
                for tid, row in rnd["teams"].items():
                    tid = int(tid)
                    if tid not in out:
                        continue
                    out[tid]["score"] += row["score"]
                    out[tid]["captured"] += row["captured"]
                    out[tid]["stolen"] += row["stolen"]
                    out[tid]["sla_ok"] += row["sla_ok"]
                    out[tid]["sla_total"] += row["sla_total"]
            for row in out.values():
                row["score"] = round(row["score"], 1)
                row["sla_ratio"] = round(row["sla_ok"] / row["sla_total"], 4) \
                    if row["sla_total"] else 1.0
            return out

    def remaining(self) -> int:
        with self.lock:
            if self.phase_state == ROUND:
                return max(0, int(self.round_ends - now()))
            if self.phase_state in (BREAK, PREP):
                return max(0, int(self.next_round_at - now()))
            return 0

    def snapshot(self) -> dict:
        with self.lock:
            found = self.cfg.locate(self.global_round) if self.global_round else None
            phase_name = found[0].name if found else "—"
            phase_index = found[0].index if found else 0
            local_round = found[1] if found else 0
            return {
                "state": self.phase_state,
                "phase": phase_name,
                "phase_index": phase_index,
                "round": local_round,
                "global_round": self.global_round,
                "total_rounds": self.cfg.total_rounds,
                "active_layers": self.cfg.active_layers(self.global_round),
                "remaining": self.remaining(),
                "round_seconds": self.cfg.round_seconds,
                "next_pull_in": max(0, int(self.next_pull_at - now())) if self.next_pull_at else None,
                "next_round_in": max(0, int(self.next_round_at - now())) if self.next_round_at else None,
                "rounds_done": len(self.rounds),
            }

    # ── 지속화 ──────────────────────────────────────────────────────────
    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            with self.lock:
                data = {
                    "global_round": self.global_round,
                    "state": self.phase_state,
                    "rounds": self.rounds,
                    "captures": self.captures[-2000:],
                    "images": self.images,
                    "llm_cost": self.llm_cost,
                    "artifacts": self.artifacts[-2000:],
                }
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, STATE_PATH)
        except OSError as e:
            print(f"[arena] state 저장 실패: {e}", flush=True)

    def restore(self) -> bool:
        if not os.path.isfile(STATE_PATH):
            return False
        try:
            with open(STATE_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        with self.lock:
            self.rounds = data.get("rounds", [])
            self.captures = data.get("captures", [])
            self.artifacts = data.get("artifacts", [])
            self.llm_cost = {int(k): v for k, v in (data.get("llm_cost") or {}).items()}
            for tid, roles in (data.get("images") or {}).items():
                self.images[int(tid)] = roles
            self.global_round = int(data.get("global_round") or 0)
        return True
