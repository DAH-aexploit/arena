#!/usr/bin/env python3
"""DAH Arena 오케스트레이터 — 참가자 페이지 + 라운드 자동 진행.

리허설 참가자 페이지에서 확인할 수 있었던 항목을 그대로 제공한다.
Phase·Round 진행 상황, Next pull in, 팀 진입점, 이미지 push·pull 시각,
SLA, verdict, Flags captured / Flags stolen, 라운드 아티팩트, LLM 비용.
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import artifacts, config, docker_ctl, state  # noqa: E402
from core.scheduler import Scheduler  # noqa: E402

PORT = int(os.environ.get("PORT", "4100"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "static")
CRED_PATH = os.environ.get("CREDENTIALS_PATH", "/data/credentials.json")

CFG = config.load()
STORE = state.Store(CFG)
SCHED = Scheduler(CFG, STORE)


# ── 자격 증명 ─────────────────────────────────────────────────────────────
def credentials() -> dict:
    out = {}
    for team in CFG.teams:
        out[team.id] = {
            "team": team.slug,
            "name": team.name,
            "account": team.account,
            "dashboard_token": team.secret("dashboard", CFG.secret_salt),
            "submit_token": team.secret("submit", CFG.secret_salt),
            "registry_user": team.slug,
            "registry_password": team.secret("registry", CFG.secret_salt),
        }
    return out


CREDS = credentials()
TOKEN_TO_TEAM = {v["dashboard_token"]: k for k, v in CREDS.items()}
SUBMIT_TO_TEAM = {v["submit_token"]: k for k, v in CREDS.items()}


def write_credentials() -> None:
    try:
        os.makedirs(os.path.dirname(CRED_PATH), exist_ok=True)
        with open(CRED_PATH, "w", encoding="utf-8") as fh:
            json.dump(CREDS, fh, ensure_ascii=False, indent=2)
        os.chmod(CRED_PATH, 0o600)
    except OSError as e:
        print(f"[arena] 자격증명 저장 실패: {e}", flush=True)


# ── 뷰 데이터 ─────────────────────────────────────────────────────────────
def team_view(team_id: int) -> dict:
    team = CFG.team(team_id)
    if team is None:
        return {}
    snap = STORE.snapshot()
    totals = STORE.totals()
    with STORE.lock:
        stats = dict(STORE.round_stats.get(team_id, {}))
        images = json.loads(json.dumps(STORE.images.get(team_id, {})))
        my_captures = [c for c in reversed(STORE.captures) if c["by"] == team_id][:60]
        my_artifacts = [a for a in reversed(STORE.artifacts) if a["team"] == team_id][:120]
        rounds = [
            {
                "round": r["round"],
                "tag": r["tag"],
                "label": _round_label(r["round"]),
                **r["teams"].get(team_id, r["teams"].get(str(team_id), {})),
            }
            for r in STORE.rounds
        ]
        events = [e for e in list(STORE.events)[:40]]
        llm_used = STORE.llm_cost.get(team_id, 0.0)

    sla_ratio = (stats.get("sla_ok", 0) / stats["sla_total"]) if stats.get("sla_total") else None
    creds = CREDS[team_id]
    return {
        "competition": {
            "id": CFG.competition_id, "name": CFG.name, "subtitle": CFG.subtitle,
            "node_platform": CFG.node_platform,
        },
        "team": {
            "id": team.id, "slug": team.slug, "name": team.name,
            "account": team.account, "entry_host": team.entry_host,
            "entry_ip": team.entry_ip, "participant": team.participant,
        },
        "status": snap,
        "entrypoints": [
            {"layer": l.n, "title": l.title, "host": team.entry_host,
             "ip": team.entry_ip, "port": l.entry_port,
             "active": l.n in snap["active_layers"]}
            for l in CFG.layers
        ],
        "images": images,
        "round_stats": {
            "sla_ok": stats.get("sla_ok", 0),
            "sla_total": stats.get("sla_total", 0),
            "sla_ratio": sla_ratio,
            "accept": stats.get("accept", 0),
            "drop": stats.get("drop", 0),
            "timeout": stats.get("timeout", 0),
            "packets": stats.get("packets", 0),
            "per_port": stats.get("per_port", {}),
            "stolen_layers": sorted(set(stats.get("stolen_layers", []))),
            "captured": stats.get("captured", 0),
            "agent_connected": stats.get("agent_connected", False),
            "restarts": stats.get("restarts", 0),
        },
        "totals": totals.get(team_id, {}),
        "captures": my_captures,
        "rounds": rounds,
        "artifacts": my_artifacts,
        "events": events,
        "scoreboard": scoreboard(),
        "llm": {"used": round(llm_used, 4), "budget": CFG.llm_budget},
        "credentials": {
            "submit_token": creds["submit_token"],
            "registry": CFG.registry_external,
            "registry_user": creds["registry_user"],
            "registry_password": creds["registry_password"],
            "defender_image": CFG.external_image_ref(team, "defender"),
            "attacker_image": CFG.external_image_ref(team, "attacker"),
        },
    }


def _round_label(global_round: int) -> str:
    found = CFG.locate(global_round)
    if not found:
        return f"Round {global_round}"
    phase, local = found
    return f"{phase.name} Round {local}"


def scoreboard() -> list[dict]:
    totals = STORE.totals()
    rows = []
    for team in CFG.teams:
        row = totals.get(team.id, {})
        rows.append({
            "id": team.id, "slug": team.slug, "name": team.name,
            "score": row.get("score", 0.0),
            "captured": row.get("captured", 0),
            "stolen": row.get("stolen", 0),
            "sla_ratio": row.get("sla_ratio", 1.0),
        })
    rows.sort(key=lambda r: (-r["score"], r["stolen"], -r["captured"]))
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows


def sla_plan() -> dict:
    snap = STORE.snapshot()
    return {
        "state": snap["state"],
        "round": snap["global_round"],
        "remaining": snap["remaining"],
        "active_layers": snap["active_layers"],
        "checks_per_round": CFG.sla_checks,
        "timeout": CFG.sla_timeout,
        "targets": [
            {"team": t.id, "host": f"{t.slug}-gateway",
             "ports": [l.entry_port for l in CFG.layers if l.n in snap["active_layers"]]}
            for t in CFG.teams
        ],
    }


# ── HTTP ─────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "dah-arena"

    def log_message(self, *a):
        pass

    # helpers
    def _send(self, code: int, body, ctype="application/json", extra=None):
        raw = body if isinstance(body, bytes) else str(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, payload, extra=None):
        self._send(code, json.dumps(payload, ensure_ascii=False), "application/json", extra)

    def _redirect(self, location: str, extra=None):
        self._send(302, b"", "text/plain", {"Location": location, **(extra or {})})

    def _static(self, name: str):
        path = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(path):
            return self._send(404, "not found", "text/plain")
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        with open(path, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def _team_from_request(self) -> int | None:
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        token = (params.get("token") or [None])[0]
        if not token:
            raw = self.headers.get("Cookie")
            if raw:
                cookie = SimpleCookie()
                cookie.load(raw)
                if "arena_token" in cookie:
                    token = cookie["arena_token"].value
        if not token:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:]
        return TOKEN_TO_TEAM.get(token or "")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            return {}

    # routes
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if path == "/":
            return self._redirect(f"/competitions/{CFG.competition_id}")
        if path.startswith("/static/"):
            return self._static(os.path.basename(path))

        if path.startswith("/competitions/"):
            comp_id = path.split("/", 2)[2].split("/")[0]
            if comp_id != CFG.competition_id:
                return self._send(404, "unknown competition", "text/plain")
            if self._team_from_request() is None:
                return self._static("login.html")
            return self._static("dashboard.html")

        if path == "/api/state":
            team_id = self._team_from_request()
            if team_id is None:
                return self._json(401, {"error": "unauthorized"})
            return self._json(200, team_view(team_id))

        if path == "/api/scoreboard":
            return self._json(200, {"status": STORE.snapshot(), "rows": scoreboard()})

        if path == "/api/sla/plan":
            return self._json(200, sla_plan())

        if path.startswith("/artifacts/"):
            team_id = self._team_from_request()
            if team_id is None:
                return self._json(401, {"error": "unauthorized"})
            name = urllib.parse.unquote(path[len("/artifacts/"):])
            if not name.startswith(f"TEAM{team_id}-"):
                return self._json(403, {"error": "다른 팀의 아티팩트"})
            full = artifacts.path_for(name)
            if full is None:
                return self._json(404, {"error": "not found"})
            with open(full, "rb") as fh:
                return self._send(200, fh.read(), "application/gzip",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})

        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/login":
            data = self._body()
            token = str(data.get("token", "")).strip()
            team_id = TOKEN_TO_TEAM.get(token)
            if team_id is None:
                return self._json(401, {"error": "토큰이 올바르지 않습니다"})
            return self._json(200, {"ok": True, "team": team_id}, {
                "Set-Cookie": f"arena_token={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400"})

        if path == "/logout":
            return self._json(200, {"ok": True},
                              {"Set-Cookie": "arena_token=; Path=/; Max-Age=0"})

        if path == "/submit":
            data = self._body()
            flag = str(data.get("flag", ""))
            team_id = SUBMIT_TO_TEAM.get(str(data.get("token", "")))
            result = STORE.submit(flag, team_id)
            code = result.pop("_code", 200)
            extra = {"Retry-After": "60"} if result["status"] == "rate_limited" else None
            return self._json(code, result, extra)

        if path == "/api/sla/report":
            data = self._body()
            try:
                STORE.record_sla(int(data["team"]), int(data.get("ok", 0)),
                                 int(data.get("total", 0)))
            except (KeyError, TypeError, ValueError):
                return self._json(400, {"error": "bad report"})
            return self._json(200, {"ok": True})

        if path == "/api/llm/usage":
            data = self._body()
            try:
                STORE.add_llm_cost(int(data["team"]), float(data.get("usd", 0)))
            except (KeyError, TypeError, ValueError):
                return self._json(400, {"error": "bad usage"})
            return self._json(200, {"ok": True})

        if path == "/control/skip":
            if os.environ.get("ALLOW_CONTROL", "1") != "1":
                return self._json(403, {"error": "disabled"})
            SCHED.skip_round()
            return self._json(200, {"ok": True})

        if path == "/control/pull":
            if os.environ.get("ALLOW_CONTROL", "1") != "1":
                return self._json(403, {"error": "disabled"})
            data = self._body()
            try:
                team_id = int(data["team"])
            except (KeyError, TypeError, ValueError):
                return self._json(400, {"error": "team 필요"})
            role = str(data.get("role", "both"))
            return self._json(200, SCHED.reload_agent(team_id, role))

        return self._send(404, "not found", "text/plain")


def _print_credentials() -> None:
    print("\n" + "=" * 74, flush=True)
    print(f"  {CFG.name}", flush=True)
    print(f"  참가자 페이지  http://localhost:{PORT}/competitions/{CFG.competition_id}", flush=True)
    print("=" * 74, flush=True)
    for team in CFG.teams:
        c = CREDS[team.id]
        mark = "참가팀" if team.participant else "봇"
        print(f"  [{mark}] {c['team']} ({c['name']}) / 계정 {c['account']}", flush=True)
        print(f"        dashboard token : {c['dashboard_token']}", flush=True)
        print(f"        submit token    : {c['submit_token']}", flush=True)
    print("=" * 74 + "\n", flush=True)


def main() -> None:
    write_credentials()
    STORE.restore()
    _print_credentials()
    threading.Thread(target=lambda: (time.sleep(3), SCHED.start()), daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
