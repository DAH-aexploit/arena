#!/usr/bin/env python3
"""compose 서비스 이름 나열 (Makefile 보조)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "orchestrator"))
from core import config  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def main() -> None:
    cfg = config.load(os.path.join(ROOT, "config", "competition.yml"))
    want = sys.argv[1] if len(sys.argv) > 1 else "--all"
    names: list[str] = []

    # 게이트웨이·챌린지 이미지는 팀 간 공유 태그다. 병렬 빌드 충돌을 피하려고
    # 빌드 대상은 첫 팀 서비스만 나열해 고유 이미지를 한 번씩만 빌드한다.
    if want == "--build":
        first = cfg.teams[0]
        for layer in cfg.layers:
            names.append(f"{first.slug}-layer-{layer.n}")
        names.append(f"{first.slug}-helper-1")
        names.append(f"{first.slug}-gateway")
        print(" ".join(names))
        return

    if want in ("--challenges", "--all"):
        for team in cfg.teams:
            for layer in cfg.layers:
                names.append(f"{team.slug}-layer-{layer.n}")
            names.append(f"{team.slug}-helper-1")
    if want in ("--gateways", "--all"):
        for team in cfg.teams:
            names.append(f"{team.slug}-gateway")
    print(" ".join(names))


if __name__ == "__main__":
    main()
