"""docker CLI 래퍼.

오케스트레이터는 대회 운영 노드 역할이므로 docker.sock 을 통해 컨테이너를
재생성하고 flag 를 주입하며 이미지를 pull 한다.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess

COMPOSE_FILE = os.environ.get("COMPOSE_FILE", "/proj/docker-compose.yml")
COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT_NAME", "dah-arena")


class DockerError(RuntimeError):
    pass


def run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["docker", *args], capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise DockerError(f"timeout: docker {shlex.join(args)}") from e
    except OSError as e:
        raise DockerError(f"docker 실행 불가: {e}") from e


def compose(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return run(["compose", "-f", COMPOSE_FILE, "-p", COMPOSE_PROJECT, *args], timeout)


def service_pattern(service: str) -> str:
    """compose 컨테이너 이름 `{project}-{service}-{index}` 에 정확히 맞는 정규식.

    부분 일치를 쓰면 team1 필터가 team10 컨테이너까지 잡아 라운드 중 엉뚱한
    컨테이너를 조작하게 된다.
    """
    return rf"^{re.escape(COMPOSE_PROJECT)}[-_]{re.escape(service)}[-_][0-9]+$"


def container_ids(service: str) -> list[str]:
    r = run(["ps", "-aq", "-f", f"name={service_pattern(service)}"])
    return r.stdout.split() if r.returncode == 0 else []


def running_ids(service: str) -> list[str]:
    r = run(["ps", "-q", "-f", f"name={service_pattern(service)}"])
    return r.stdout.split() if r.returncode == 0 else []


def exec_in(name_filter: str, argv: list[str], timeout: int = 20) -> str | None:
    for cid in running_ids(name_filter):
        r = run(["exec", cid, *argv], timeout)
        if r.returncode == 0:
            return r.stdout
        return None
    return None


def logs_since(name_filter: str, since_epoch: float) -> str:
    for cid in container_ids(name_filter):
        r = run(["logs", "--since", str(int(since_epoch)), cid], timeout=60)
        if r.returncode == 0:
            return (r.stdout or "") + (r.stderr or "")
        return ""
    return ""


def restart_count(name_filter: str) -> int:
    for cid in container_ids(name_filter):
        r = run(["inspect", "-f", "{{.RestartCount}}", cid])
        if r.returncode == 0:
            try:
                return int(r.stdout.strip())
            except ValueError:
                return 0
    return 0


def container_image_id(name_filter: str) -> str | None:
    for cid in running_ids(name_filter):
        r = run(["inspect", "-f", "{{.Image}}", cid])
        if r.returncode == 0:
            return r.stdout.strip()
    return None


def image_platform(ref: str) -> tuple[str | None, str | None]:
    """로컬 이미지의 (os/arch, RepoDigest)."""
    r = run(["image", "inspect", ref, "-f", "{{.Os}}/{{.Architecture}}|{{index .RepoDigests 0}}"])
    if r.returncode != 0:
        r = run(["image", "inspect", ref, "-f", "{{.Os}}/{{.Architecture}}"])
        if r.returncode != 0:
            return None, None
        return r.stdout.strip(), None
    platform, _, digest = r.stdout.strip().partition("|")
    if "@" in digest:
        digest = digest.split("@", 1)[1]
    return platform or None, digest or None


def image_exists(ref: str) -> bool:
    return run(["image", "inspect", ref, "-f", "ok"]).returncode == 0


def pull(ref: str, platform: str | None = None, timeout: int = 180) -> tuple[bool, str]:
    args = ["pull"]
    if platform:
        args += ["--platform", platform]
    args.append(ref)
    r = run(args, timeout)
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0, out.strip()


def imagetools_describe(ref: str, timeout: int = 60) -> dict | None:
    """원격 레지스트리(GHCR 등) 이미지의 플랫폼·digest 조회.

    데몬 자격증명을 쓰는 `docker buildx imagetools inspect --raw` 로 manifest 를
    읽어, 로컬 registry HTTP 없이 원격 이미지를 판정한다.
    """
    r = run(["buildx", "imagetools", "inspect", "--raw", ref], timeout)
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None
    platforms: list[str] = []
    media = data.get("mediaType", "")
    if "index" in media or "manifest.list" in media or "manifests" in data:
        for m in data.get("manifests", []) or []:
            p = m.get("platform") or {}
            if p.get("os") and p.get("architecture") and p.get("architecture") != "unknown":
                platforms.append(f"{p['os']}/{p['architecture']}")
    # digest: imagetools inspect (비 raw)의 Digest, 여기서는 별도 조회
    d = run(["buildx", "imagetools", "inspect", ref, "--format", "{{.Manifest.Digest}}"], timeout)
    digest = d.stdout.strip() if d.returncode == 0 else None
    return {"platforms": platforms, "digest": digest}


def manifest_platforms(ref: str, timeout: int = 60) -> list[str]:
    """레지스트리의 manifest 에 포함된 플랫폼 목록(가능한 경우)."""
    r = run(["manifest", "inspect", "--insecure", ref], timeout)
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return []
    out = []
    for m in data.get("manifests", []) or []:
        p = m.get("platform") or {}
        if p.get("os") and p.get("architecture"):
            out.append(f"{p['os']}/{p['architecture']}")
    return out


def tag(src: str, dst: str) -> bool:
    return run(["tag", src, dst]).returncode == 0


def recreate(services: list[str], timeout: int = 300) -> str:
    r = compose(["up", "-d", "--no-build", "--no-deps", "--force-recreate", *services], timeout)
    return ((r.stdout or "") + (r.stderr or "")).strip()


def stop(services: list[str], timeout: int = 120) -> None:
    compose(["stop", *services], timeout)


def start(services: list[str], timeout: int = 180) -> None:
    compose(["up", "-d", "--no-build", "--no-deps", *services], timeout)
