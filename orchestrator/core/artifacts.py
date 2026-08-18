"""라운드 아티팩트 수집.

리허설에서 받은 파일명 규칙을 그대로 쓴다.

  TEAM9-P1-R1-attacker.log.gz
  TEAM9-P1-R1-defender.log.gz
  TEAM9-P2-R10-l1-20260815070006.pcap.gz

파일명의 라운드 번호는 전역 번호이고, 참가자 페이지는 Phase별 번호로 표시한다.
리허설에서 두 체계가 달라 대조에 손이 갔던 부분이라 대시보드에 둘 다 적는다.
"""

from __future__ import annotations

import gzip
import os
import shutil
import time

from . import docker_ctl

CAPTURE_DIR = os.environ.get("CAPTURE_DIR", "/captures")
ARTIFACT_DIR = os.environ.get("ARTIFACT_DIR", "/artifacts")


def _gzip_bytes(dst: str, data: bytes) -> int:
    with gzip.open(dst, "wb", compresslevel=6) as out:
        out.write(data)
    return os.path.getsize(dst)


def _gzip_file(src: str, dst: str) -> int:
    with open(src, "rb") as fin, gzip.open(dst, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1 << 20)
    return os.path.getsize(dst)


def collect(cfg, store, global_round: int, round_started: float) -> list[dict]:
    """라운드 종료 직후 pcap·로그를 수집해 다운로드 가능한 형태로 만든다."""
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    tag = cfg.round_tag(global_round)
    produced: list[dict] = []

    for team in cfg.teams:
        prefix = f"TEAM{team.id}-{tag}"

        # 컨테이너 로그
        for role in ("attacker", "defender"):
            service = f"{team.slug}-{role}"
            text = docker_ctl.logs_since(service, round_started)
            name = f"{prefix}-{role}.log.gz"
            path = os.path.join(ARTIFACT_DIR, name)
            try:
                size = _gzip_bytes(path, text.encode("utf-8", "replace"))
            except OSError:
                continue
            produced.append({"team": team.id, "round": global_round, "tag": tag,
                             "kind": role, "name": name, "size": size,
                             "created": time.time()})

        # 레이어별 pcap
        try:
            entries = sorted(os.listdir(CAPTURE_DIR))
        except OSError:
            entries = []
        want = f"team{team.id}-{tag}-"
        for entry in entries:
            if not entry.startswith(want) or not entry.endswith(".pcap"):
                continue
            src = os.path.join(CAPTURE_DIR, entry)
            if not os.path.isfile(src) or os.path.getsize(src) <= 24:
                continue
            name = f"TEAM{team.id}-{entry[len(f'team{team.id}-'):-5]}.pcap.gz"
            path = os.path.join(ARTIFACT_DIR, name)
            try:
                size = _gzip_file(src, path)
                os.remove(src)
            except OSError:
                continue
            layer = "l?"
            for part in entry.split("-"):
                if part.startswith("l") and part[1:].isdigit():
                    layer = part
            produced.append({"team": team.id, "round": global_round, "tag": tag,
                             "kind": f"pcap-{layer}", "name": name, "size": size,
                             "created": time.time()})

    store.add_artifacts(produced)
    store.log(f"{tag} 아티팩트 {len(produced)}건 생성")
    return produced


def path_for(name: str) -> str | None:
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    path = os.path.join(ARTIFACT_DIR, name)
    return path if os.path.isfile(path) else None
