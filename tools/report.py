#!/usr/bin/env python3
"""테스트베드 결과 요약 — head-to-head.

참가자 페이지 API 를 읽어 두 가지를 나눠 보여준다.

  방어(team9) : 라운드별 stolen / drop / SLA — 공격을 막았는가
  공격(team9) : 상대별 captured — 무방비/방어 미러를 뚫었는가

역할 라벨(config.role_label)로 각 상대가 무엇을 검증하는 표적인지 표시한다.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "orchestrator"))
from core import config  # noqa: E402

PORT = os.environ.get("DASHBOARD_PORT", "4100")
BASE = f"http://localhost:{PORT}"


def api(path: str, token: str | None = None) -> dict:
    req = urllib.request.Request(BASE + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or b"{}")


def bar(ratio: float, width: int = 20) -> str:
    n = int(round(ratio * width))
    return "█" * n + "░" * (width - n)


def main() -> None:
    cfg = config.load(os.path.join(ROOT, "config", "competition.yml"))
    me = next((t for t in cfg.teams if t.participant), cfg.teams[0])
    token = me.secret("dashboard", cfg.secret_salt)
    label = {t.id: (t.role_label or t.name) for t in cfg.teams}

    try:
        d = api("/api/state", token)
    except OSError as e:
        print(f"오케스트레이터에 연결 불가({BASE}): {e}")
        sys.exit(1)

    s = d["status"]
    print(f"\n═══ DAH Arena 테스트베드 — {me.slug} ({me.name}) ═══")
    print(f"  진행: {s['state']} · {s['phase']} R{s['round']} · 완료 {s['rounds_done']}/{s['total_rounds']}")
    print(f"  방어 = {me.defender_role} · 공격 = {me.attacker_role}\n")

    # ── 방어 ──────────────────────────────────────────────────────────────
    print("─ 방어 (team%d 본진) 라운드별 ───────────────────────" % me.id)
    print("  %-16s %-10s %6s %8s  %s" % ("라운드", "stolen", "drop", "SLA", ""))
    rows = d.get("rounds", [])
    if not rows:
        print("  (종료된 라운드 없음 — 첫 라운드 진행 중)")
    for r in rows[-10:]:
        sla = r.get("sla_ratio", 1.0)
        stolen = r.get("stolen_layers", [])
        mark = "실패" if stolen else "방어"
        print("  %-16s %-10s %6s %7.0f%% %s %s" % (
            r["tag"], (",".join("L%d" % x for x in stolen) or "없음"),
            r.get("drop", 0), sla * 100, bar(sla, 12), mark))

    # ── 공격 ──────────────────────────────────────────────────────────────
    print("\n─ 공격 (team%d 본진) 상대별 성과 ──────────────────" % me.id)
    caps = d.get("captures", [])
    by_victim: dict[int, dict] = {}
    for c in caps:
        v = by_victim.setdefault(c["victim"], {"total": 0, "layers": set(), "rounds": set()})
        v["total"] += 1
        v["layers"].add(c["layer"])
        v["rounds"].add(c["round"])
    opponents = [t for t in cfg.teams if t.id != me.id]
    if not caps:
        print("  (아직 탈취 없음)")
    for opp in opponents:
        v = by_victim.get(opp.id)
        tag = label.get(opp.id, opp.name)
        if v:
            print("  team%-2d %-24s captured=%d  L=%s  (%d개 라운드)" % (
                opp.id, tag, v["total"],
                ",".join("L%d" % x for x in sorted(v["layers"])), len(v["rounds"])))
        else:
            print("  team%-2d %-24s captured=0  (뚫지 못함)" % (opp.id, tag))

    # ── 핵심 지표 ────────────────────────────────────────────────────────
    mirror = next((t for t in opponents if t.defender_role == me.defender_role), None)
    open_t = next((t for t in opponents if t.defender_role == "passthrough"), None)
    print("\n─ 핵심 해석 ───────────────────────────────────────")
    if open_t:
        v = by_victim.get(open_t.id)
        print("  · 공격 순수 능력(무방비 team%d): %s" % (
            open_t.id, "성공" if v else "미탈취 — 공격 에이전트 점검 필요"))
    if mirror:
        v = by_victim.get(mirror.id)
        print("  · 공격 vs 방어(미러 team%d): %s" % (
            mirror.id, "방어 뚫림 ⚠" if v else "방어가 막음 ✓"))
    print()


if __name__ == "__main__":
    main()
