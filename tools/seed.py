#!/usr/bin/env python3
"""레지스트리 시드 — 역할 기반으로 팀별 에이전트 이미지를 build·push.

config 의 각 팀 defender/attacker 역할을 agent_sources 로 실제 코드에 매핑해
NODE_PLATFORM 으로 빌드하고 로컬 레지스트리에 push 한다. 소스 디렉토리가 없으면
안전한 기본(방어=passthrough, 공격=bot)으로 폴백하고 경고한다.

  python3 tools/seed.py                 # 전 팀 defender·attacker 시드
  python3 tools/seed.py --team 9 --role defender   # 일부만
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "orchestrator"))
from core import config  # noqa: E402

FALLBACK = {"defender": "agents/passthrough-defender", "attacker": "agents/bot-attacker"}


def platform() -> str:
    if os.environ.get("NODE_PLATFORM"):
        return os.environ["NODE_PLATFORM"]
    out = subprocess.run(["docker", "version", "--format", "{{.Server.Arch}}"],
                         capture_output=True, text=True)
    return f"linux/{out.stdout.strip() or 'amd64'}"


def resolve(cfg, team, role: str) -> tuple[str, str, str]:
    """(role_name, 절대경로, 실제사용경로). 없으면 폴백."""
    role_name = team.defender_role if role == "defender" else team.attacker_role
    src = cfg.defender_source(team) if role == "defender" else cfg.attacker_source(team)
    abspath = src if os.path.isabs(src) else os.path.join(ROOT, src)
    if os.path.isfile(os.path.join(abspath, "Dockerfile")):
        return role_name, abspath, abspath
    fallback = os.path.join(ROOT, FALLBACK[role])
    print(f"  ⚠ team{team.id} {role}='{role_name}' 소스 없음({src}) → "
          f"기본({FALLBACK[role]}) 사용", flush=True)
    return role_name, abspath, fallback


def build_push(context: str, ref: str, plat: str) -> bool:
    print(f"  · build {ref}  ({os.path.relpath(context, ROOT)}, {plat})", flush=True)
    r = subprocess.run([
        "docker", "buildx", "build", "--platform", plat,
        "--provenance=false", "--sbom=false", "-t", ref, "--push", context],
        cwd=ROOT)
    return r.returncode == 0


def wait_registry(external: str, tries: int = 30) -> None:
    for _ in range(tries):
        try:
            urllib.request.urlopen(f"http://{external}/v2/", timeout=2)
            return
        except OSError:
            import time
            time.sleep(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", type=int, default=None)
    ap.add_argument("--role", choices=["defender", "attacker"], default=None)
    ap.add_argument("--no-registry-wait", action="store_true")
    args = ap.parse_args()

    cfg = config.load(os.path.join(ROOT, "config", "competition.yml"))
    plat = platform()
    where = cfg.remote_registry if cfg.remote else cfg.registry_external
    print(f"▶ 시드 레지스트리 {where} ({'원격/GHCR' if cfg.remote else '로컬'}, "
          f"platform={plat})")

    if cfg.remote:
        # 원격 모드: docker login 필요. push 실패 시 로그인 안내.
        print("  (원격 모드: 'docker login ghcr.io' 로 org 패키지 write 권한 필요)")
    else:
        subprocess.run(["docker", "compose", "up", "-d", "registry"], cwd=ROOT,
                       capture_output=True)
        if not args.no_registry_wait:
            wait_registry(cfg.registry_external)

    teams = [t for t in cfg.teams if args.team is None or t.id == args.team]
    roles = [args.role] if args.role else ["defender", "attacker"]
    failed = 0
    for team in teams:
        for role in roles:
            role_name, _abs, context = resolve(cfg, team, role)
            ref = cfg.external_image_ref(team, role)
            if not build_push(context, ref, plat):
                failed += 1
                print(f"  ✗ team{team.id} {role} push 실패", flush=True)
    if failed:
        print(f"✗ {failed}건 실패")
        sys.exit(1)
    print("✓ 시드 완료")


if __name__ == "__main__":
    main()
