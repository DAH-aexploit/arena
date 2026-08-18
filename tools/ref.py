#!/usr/bin/env python3
"""팀·역할의 외부 이미지 참조(push 대상)를 출력. push 스크립트 보조."""

from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "orchestrator"))
from core import config  # noqa: E402


def main() -> None:
    team_id = int(sys.argv[1])
    role = sys.argv[2]
    cfg = config.load(os.path.join(ROOT, "config", "competition.yml"))
    team = cfg.team(team_id)
    if team is None:
        sys.exit(f"unknown team {team_id}")
    print(cfg.external_image_ref(team, role))


if __name__ == "__main__":
    main()
