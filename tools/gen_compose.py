#!/usr/bin/env python3
"""config/competition.yml → docker-compose.yml 생성.

팀 수·레이어 수에 맞춰 인프라를 펼친다. 팀마다 gateway(방어 broker+진입점),
defender, attacker, 챌린지 컨테이너, 팀 전용 lab 네트워크를 만든다.

네트워크
  edge   외부 노출(orchestrator·registry·litellm)
  arena  진입점·에이전트·SLA·오케스트레이터 (internal, teamN.lig.internal alias)
  labN   팀 N 의 챌린지 전용 (internal)

broker 소켓은 gateway 와 defender 가 brokerN 볼륨을 공유해 전달한다.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "orchestrator"))
from core import config  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def q(v) -> str:
    return f'"{v}"'


def emit(cfg: config.Config) -> str:
    L = []
    add = L.append
    layers = cfg.layers
    reg = cfg.registry_host

    add("# 이 파일은 tools/gen_compose.py 가 생성합니다. 직접 수정하지 마세요.")
    add("# 재생성: make compose  (또는 python3 tools/gen_compose.py)")
    add("name: dah-arena")
    add("")

    # ── 공통 앵커 ────────────────────────────────────────────────────────
    add("x-def-lock: &def-lock")
    add("  cap_drop: [\"ALL\"]")
    add("  security_opt: [\"no-new-privileges:true\"]")
    add("  pids_limit: 512")
    add("  restart: unless-stopped")
    add("")
    add("x-att-lock: &att-lock")
    add("  security_opt: [\"no-new-privileges:true\"]")
    add("  pids_limit: 512")
    add("  restart: unless-stopped")
    add("")
    add("x-logging: &logging")
    add("  driver: json-file")
    add("  options: { max-size: \"10m\", max-file: \"5\" }")
    add("")
    add("services:")

    # ── 레지스트리 (로컬 모드에서만; 원격/GHCR 모드는 외부 레지스트리 사용) ──
    if not cfg.remote:
        add("  registry:")
        add("    image: registry:2")
        add("    restart: unless-stopped")
        add("    networks: { edge: {}, arena: {} }")
        add(f"    ports: [\"{cfg.registry_external.split(':')[-1]}:5000\"]")
        add("    environment:")
        add("      REGISTRY_STORAGE_DELETE_ENABLED: \"true\"")
        add("    volumes: [\"registry-data:/var/lib/registry\"]")
        add("")

    # ── LiteLLM 게이트웨이 ───────────────────────────────────────────────
    add("  litellm-gw:")
    add("    build: ./litellm-gw")
    add("    image: dah-arena/litellm-gw:latest")
    add("    restart: unless-stopped")
    add("    networks: { arena: {}, edge: {} }")
    add("    extra_hosts: [\"host.docker.internal:host-gateway\"]")
    add("    environment:")
    add("      LITELLM_MASTER_KEY: \"sk-local\"")
    add("      ARENA_BUDGET_USD: \"${LLM_BUDGET:-20}\"")
    add("      AZURE_API_BASE: \"${AZURE_API_BASE:-}\"")
    add("      AZURE_API_KEY: \"${AZURE_API_KEY:-}\"")
    add("      LLM_UPSTREAM_BASE: \"${LLM_UPSTREAM_BASE:-}\"")
    add("      LLM_UPSTREAM_KEY: \"${LLM_UPSTREAM_KEY:-}\"")
    add("      ORCH_URL: \"http://orchestrator:4100\"")
    add("    logging: *logging")
    add("")

    # ── 오케스트레이터 ───────────────────────────────────────────────────
    add("  orchestrator:")
    add("    build: ./orchestrator")
    add("    image: dah-arena/orchestrator:latest")
    add("    restart: unless-stopped")
    add("    networks: { edge: {}, arena: {} }")
    add("    ports: [\"${DASHBOARD_PORT:-4100}:4100\"]")
    add("    volumes:")
    add("      - \"./config:/config:ro\"")
    add("      - \".:/proj:ro\"")
    add("      - \"/var/run/docker.sock:/var/run/docker.sock\"")
    add("      - \"captures:/captures\"")
    add("      - \"artifacts:/artifacts\"")
    add("      - \"arena-data:/data\"")
    add("    environment:")
    add("      COMPOSE_PROJECT_NAME: \"dah-arena\"")
    add("      COMPOSE_FILE: \"/proj/docker-compose.yml\"")
    add("      ROUND_SECONDS: \"${ROUND_SECONDS:-1200}\"")
    add("      BREAK_SECONDS: \"${BREAK_SECONDS:-1200}\"")
    add("      PULL_LEAD_SECONDS: \"${PULL_LEAD_SECONDS:-300}\"")
    add("      AUTOSTART: \"${AUTOSTART:-1}\"")
    add("      LOOP: \"${LOOP:-0}\"")
    add("      ALLOW_CONTROL: \"${ALLOW_CONTROL:-1}\"")
    add("      ARENA_SALT: \"${ARENA_SALT:-dah-arena-local}\"")
    add(f"      NODE_PLATFORM: \"${{NODE_PLATFORM:-{cfg.node_platform}}}\"")
    add("      ENFORCE_PLATFORM: \"${ENFORCE_PLATFORM:-1}\"")
    add(f"      REGISTRY_MODE: \"${{REGISTRY_MODE:-{cfg.registry_mode}}}\"")
    add(f"      REMOTE_REGISTRY: \"${{REMOTE_REGISTRY:-{cfg.remote_registry}}}\"")
    if not cfg.remote:
        add("    depends_on: [registry]")
    add("    logging: *logging")
    add("")

    # ── SLA 검사기 ───────────────────────────────────────────────────────
    add("  sla-checker:")
    add("    build: ./sla")
    add("    image: dah-arena/sla-checker:latest")
    add("    restart: unless-stopped")
    add("    networks: { arena: {} }")
    add("    environment:")
    add("      ORCH_URL: \"http://orchestrator:4100\"")
    add(f"      SLA_TIMEOUT: {q(cfg.sla_timeout)}")
    add("    depends_on: [orchestrator]")
    add("    logging: *logging")
    add("")

    # ── 팀별 스택 ────────────────────────────────────────────────────────
    for team in cfg.teams:
        octet = team.subnet_octet
        lab = f"lab{octet}"
        broker_vol = f"broker{team.id}"
        chal_deps = []

        add(f"  # ── team{team.id} ({team.name}) {'참가' if team.participant else '봇'} ──")

        # 챌린지
        for layer in layers:
            svc = f"{team.slug}-layer-{layer.n}"
            chal_deps.append(svc)
            ip = f"172.31.{octet}.{10 + layer.n}"
            add(f"  {svc}:")
            add(f"    build: ./challenges/layer-{layer.n}")
            add(f"    image: dah-arena/layer-{layer.n}:latest")
            add("    restart: unless-stopped")
            add(f"    networks: {{ {lab}: {{ ipv4_address: {ip} }} }}")
            add("    logging: *logging")
        # helper-box (L1 내부 서비스)
        helper = f"{team.slug}-helper-1"
        chal_deps.append(helper)
        add(f"  {helper}:")
        add("    build:")
        add("      context: ./challenges/layer-1")
        add("      dockerfile: Dockerfile.helper")
        add("    image: dah-arena/helper-l1:latest")
        add("    restart: unless-stopped")
        add("    networks:")
        add(f"      {lab}: {{ ipv4_address: 172.31.{octet}.21, aliases: [\"helper-box\"] }}")
        add("    logging: *logging")

        # gateway (broker + 진입점)
        gw = f"{team.slug}-gateway"
        add(f"  {gw}:")
        add("    build: ./gateway")
        add("    image: dah-arena/gateway:latest")
        add("    restart: unless-stopped")
        add("    networks:")
        add(f"      arena: {{ aliases: [\"{team.entry_host}\"] }}")
        add(f"      {lab}: {{}}")
        add(f"    volumes: [\"{broker_vol}:/tmp/broker\", \"captures:/captures\"]")
        add("    environment:")
        add(f"      TEAM: {q(team.id)}")
        add(f"      ENTRY_IP: {q(team.entry_ip)}")
        add(f"      LAYERS: {q(','.join(str(l.n) for l in layers))}")
        for layer in layers:
            add(f"      ENTRY_L{layer.n}: {q(layer.entry_port)}")
            add(f"      CHAL_L{layer.n}: {q(f'{team.slug}-layer-{layer.n}:{layer.entry_port}')}")
        add("      AGENT_SOCKET_PATH: \"/tmp/broker/agent.sock\"")
        add("      VERDICT_TIMEOUT_MS: \"300\"")
        add("      SYN_LIMIT: \"10\"")
        add("      SYN_BURST: \"20\"")
        add(f"    depends_on: [{', '.join(chal_deps)}]")
        add("    logging: *logging")

        # defender
        dfd = f"{team.slug}-defender"
        add(f"  {dfd}:")
        add("    <<: *def-lock")
        add(f"    image: {cfg.image_ref(team, 'defender')}")
        add("    networks: { arena: {} }")
        add(f"    volumes: [\"{broker_vol}:/run\"]")
        add("    environment:")
        add("      AGENT_SOCKET: \"/run/agent.sock\"")
        add("      LLM_BASE_URL: \"http://litellm-gw:4000\"")
        add(f"      LLM_API_KEY: \"t{team.id}-{team.secret('llm', cfg.secret_salt)}\"")
        add(f"    depends_on: [{gw}]")
        add("    logging: *logging")

        # attacker
        atk = f"{team.slug}-attacker"
        opponents = [t for t in cfg.teams if t.id != team.id]
        targets = ",".join(t.entry_host for t in opponents)
        add(f"  {atk}:")
        add("    <<: *att-lock")
        add(f"    image: {cfg.image_ref(team, 'attacker')}")
        add("    networks: { arena: {} }")
        add("    environment:")
        add(f"      TARGETS: {q(targets)}")
        add(f"      PORTS: {q(','.join(str(l.entry_port) for l in layers))}")
        add("      SUBMIT_URL: \"http://orchestrator:4100/submit\"")
        add(f"      SUBMIT_TOKEN: \"{team.secret('submit', cfg.secret_salt)}\"")
        add("      LLM_BASE_URL: \"http://litellm-gw:4000\"")
        add(f"      LLM_API_KEY: \"t{team.id}-{team.secret('llm', cfg.secret_salt)}\"")
        add("    depends_on: [orchestrator]")
        add("    logging: *logging")
        add("")

    # ── 네트워크 ─────────────────────────────────────────────────────────
    add("networks:")
    add("  edge: { driver: bridge }")
    add("  arena:")
    add("    driver: bridge")
    add("    internal: true")
    add("    ipam: { config: [{ subnet: \"172.30.0.0/24\" }] }")
    for team in cfg.teams:
        add(f"  lab{team.subnet_octet}:")
        add("    driver: bridge")
        add("    internal: true")
        add(f"    ipam: {{ config: [{{ subnet: \"172.31.{team.subnet_octet}.0/24\" }}] }}")
    add("")

    # ── 볼륨 ─────────────────────────────────────────────────────────────
    add("volumes:")
    if not cfg.remote:
        add("  registry-data: {}")
    add("  captures: {}")
    add("  artifacts: {}")
    add("  arena-data: {}")
    for team in cfg.teams:
        add(f"  broker{team.id}: {{}}")
    add("")

    return "\n".join(L)


def main() -> None:
    cfg = config.load(os.path.join(ROOT, "config", "competition.yml"))
    out = emit(cfg)
    dst = os.path.join(ROOT, "docker-compose.yml")
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(out)
    print(f"생성 완료: {dst}")
    print(f"  팀 {len(cfg.teams)} · 레이어 {len(cfg.layers)} · "
          f"참가팀 {len(cfg.participants)}")


if __name__ == "__main__":
    main()
