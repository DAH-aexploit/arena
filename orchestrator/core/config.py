"""대회 설정 로딩.

config/competition.yml 을 읽고 환경변수로 덮어쓴다. 전역 라운드 번호와
Phase별 라운드 번호를 함께 다루는데, 리허설에서 참가자 페이지(Phase별 번호)와
아티팩트 파일명(전역 번호)이 서로 달라 대조에 손이 갔던 점을 반영한 것이다.
  Phase 2 Round 1 = 전역 Round 7 / Phase 3 Round 1 = 전역 Round 13
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from . import miniyaml

DEFAULT_PATH = os.environ.get("COMPETITION_CONFIG", "/config/competition.yml")


def _env_int(name: str, fallback: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return fallback


@dataclass(frozen=True)
class Layer:
    n: int
    entry_port: int
    service: str
    title: str

    @property
    def key(self) -> str:
        return f"l{self.n}"


@dataclass(frozen=True)
class Team:
    id: int
    name: str
    account: str
    participant: bool
    subnet_octet: int
    defender_role: str = "passthrough"   # defense | passthrough | ...
    attacker_role: str = "bot"           # attack | bot | ...
    role_label: str = ""                 # 대시보드 표시용(예: "무방비 스파링")

    @property
    def slug(self) -> str:
        return f"team{self.id}"

    @property
    def entry_host(self) -> str:
        return f"team{self.id}.lig.internal"

    @property
    def entry_ip(self) -> str:
        return f"10.{self.subnet_octet}.0.4"

    @property
    def lab_subnet(self) -> str:
        return f"172.31.{self.subnet_octet}.0/24"

    def secret(self, kind: str, salt: str) -> str:
        raw = f"{salt}:{kind}:{self.id}:{self.account}".encode()
        return hashlib.sha256(raw).hexdigest()[:32]


@dataclass(frozen=True)
class Phase:
    index: int
    name: str
    rounds: int
    layers: tuple[int, ...]


@dataclass
class Config:
    competition_id: str
    name: str
    subtitle: str
    round_seconds: int
    break_seconds: int
    pull_lead_seconds: int
    autostart: bool
    phases: list[Phase]
    layers: list[Layer]
    teams: list[Team]
    registry_host: str
    registry_api: str
    registry_external: str
    registry_mode: str
    remote_registry: str
    node_platform: str
    enforce_platform: bool
    llm_budget: float
    llm_base_url: str
    sla_checks: int
    sla_timeout: int
    agent_sources: dict = field(default_factory=dict)
    secret_salt: str = field(default="dah-arena-local")

    # ── 에이전트 소스 해석 ───────────────────────────────────────────────
    def agent_source(self, role: str) -> str:
        """역할 이름 → 빌드 컨텍스트 디렉토리. 환경변수가 우선한다.

        예: role='defense' → env AGENT_SRC_DEFENSE 또는 agent_sources['defense'].
        """
        env = os.environ.get(f"AGENT_SRC_{role.upper()}")
        if env:
            return env
        return str(self.agent_sources.get(role, ""))

    def defender_source(self, team: Team) -> str:
        return self.agent_source(team.defender_role)

    def attacker_source(self, team: Team) -> str:
        return self.agent_source(team.attacker_role)

    # ── 조회 헬퍼 ────────────────────────────────────────────────────────
    def team(self, team_id: int) -> Team | None:
        return next((t for t in self.teams if t.id == team_id), None)

    def layer(self, n: int) -> Layer | None:
        return next((l for l in self.layers if l.n == n), None)

    @property
    def participants(self) -> list[Team]:
        return [t for t in self.teams if t.participant]

    @property
    def total_rounds(self) -> int:
        return sum(p.rounds for p in self.phases)

    def locate(self, global_round: int) -> tuple[Phase, int] | None:
        """전역 라운드 번호 → (Phase, Phase 내 라운드 번호)."""
        cursor = 0
        for phase in self.phases:
            if global_round <= cursor + phase.rounds:
                return phase, global_round - cursor
            cursor += phase.rounds
        return None

    def active_layers(self, global_round: int) -> list[int]:
        found = self.locate(global_round)
        return list(found[0].layers) if found else []

    def round_tag(self, global_round: int) -> str:
        """아티팩트 파일명 태그. 리허설과 같은 `P2-R8` 형태."""
        found = self.locate(global_round)
        if not found:
            return f"R{global_round}"
        phase, _local = found
        return f"P{phase.index}-R{global_round}"

    @property
    def remote(self) -> bool:
        """원격(GHCR 등) 레지스트리 모드 여부. 로컬 registry 컨테이너를 쓰지 않는다."""
        return self.registry_mode == "remote"

    def _remote_ref(self, team: Team, role: str) -> str:
        # GHCR 등은 owner 아래 단일 세그먼트 이미지명. team9/defender 대신 team9-defender.
        return f"{self.remote_registry}/{team.slug}-{role}:latest"

    def image_ref(self, team: Team, role: str) -> str:
        """compose 이미지 참조 · 오케스트레이터 pull 대상(호스트 데몬이 해석)."""
        if self.remote:
            return self._remote_ref(team, role)
        return f"{self.registry_host}/{team.slug}/{role}:latest"

    def external_image_ref(self, team: Team, role: str) -> str:
        """참가자가 push 할 때 쓰는 주소(로컬=localhost:5001, 원격=동일 ref)."""
        if self.remote:
            return self._remote_ref(team, role)
        return f"{self.registry_external}/{team.slug}/{role}:latest"


def load(path: str = DEFAULT_PATH) -> Config:
    raw = miniyaml.load(path)
    comp = raw.get("competition") or {}
    sched = raw.get("schedule") or {}
    reg = raw.get("registry") or {}
    llm = raw.get("llm") or {}
    sla = raw.get("sla") or {}

    phases = [
        Phase(index=i + 1,
              name=str(p.get("name") or f"Phase {i + 1}"),
              rounds=int(p.get("rounds") or 1),
              layers=tuple(int(x) for x in (p.get("layers") or [])))
        for i, p in enumerate(sched.get("phases") or [])
    ]
    layers = [
        Layer(n=int(l["n"]), entry_port=int(l["entry_port"]),
              service=str(l["service"]), title=str(l.get("title") or l["service"]))
        for l in (raw.get("layers") or [])
    ]
    def _role_defaults(t: dict) -> tuple[str, str]:
        # 명시 role 이 없으면 participant 여부로 기본값 도출(하위 호환)
        part = bool(t.get("participant"))
        dfd = str(t.get("defender") or ("defense" if part else "passthrough"))
        atk = str(t.get("attacker") or ("attack" if part else "bot"))
        return dfd, atk

    teams = []
    for t in (raw.get("teams") or []):
        dfd, atk = _role_defaults(t)
        teams.append(Team(
            id=int(t["id"]), name=str(t["name"]), account=str(t.get("account") or ""),
            participant=bool(t.get("participant")),
            subnet_octet=int(t.get("subnet_octet") or t["id"]),
            defender_role=dfd, attacker_role=atk,
            role_label=str(t.get("role_label") or "")))

    agent_sources = dict(raw.get("agent_sources") or {})
    agent_sources.setdefault("passthrough", "agents/passthrough-defender")
    agent_sources.setdefault("bot", "agents/bot-attacker")
    agent_sources.setdefault("defense", "../defense-agent")
    agent_sources.setdefault("attack", "../attack-agent/agents/team1/attacker")

    return Config(
        competition_id=str(comp.get("id") or "local"),
        name=str(comp.get("name") or "DAH Arena"),
        subtitle=str(comp.get("subtitle") or ""),
        round_seconds=_env_int("ROUND_SECONDS", int(sched.get("round_seconds") or 1200)),
        break_seconds=_env_int("BREAK_SECONDS", int(sched.get("break_seconds") or 1200)),
        pull_lead_seconds=_env_int("PULL_LEAD_SECONDS",
                                   int(sched.get("pull_lead_seconds") or 300)),
        autostart=os.environ.get("AUTOSTART", "1" if sched.get("autostart") else "0") == "1",
        phases=phases,
        layers=layers,
        teams=teams,
        registry_host=os.environ.get("REGISTRY_HOST", str(reg.get("host") or "localhost:5001")),
        registry_api=os.environ.get("REGISTRY_API", str(reg.get("api") or "registry:5000")),
        registry_external=os.environ.get(
            "REGISTRY_EXTERNAL", str(reg.get("external_host") or "localhost:5001")),
        registry_mode=os.environ.get("REGISTRY_MODE", str(reg.get("mode") or "local")).lower(),
        remote_registry=os.environ.get(
            "REMOTE_REGISTRY", str(reg.get("remote") or "ghcr.io/dah-aexploit")),
        node_platform=os.environ.get("NODE_PLATFORM", str(reg.get("node_platform") or "linux/amd64")),
        enforce_platform=os.environ.get(
            "ENFORCE_PLATFORM", "1" if reg.get("enforce_platform") else "0") == "1",
        llm_budget=float(llm.get("budget_usd") or 0),
        llm_base_url=str(llm.get("base_url") or ""),
        sla_checks=_env_int("SLA_CHECKS", int(sla.get("checks_per_round") or 100)),
        sla_timeout=int(sla.get("timeout_seconds") or 5),
        agent_sources=agent_sources,
        secret_salt=os.environ.get("ARENA_SALT", "dah-arena-local"),
    )
