"""팀 이미지 레지스트리 연동.

대회의 ACR(`ligacr.azurecr.io`) 자리를 로컬 registry 가 대신한다. 참가자는
`team9/defender:latest` 로 push 하고, 운영 인프라는 라운드 사이 pull 시점에만
이를 반영한다. push 즉시 적용되지 않는 구조를 그대로 재현한다.

플랫폼 검사도 재현 대상이다. 2026-08-15 리허설에서 Apple Silicon 기본값으로
빌드된 arm64 이미지가 amd64 노드에서 4라운드 연속 반영되지 않았고, push 와
pull 은 모두 성공해 원인 파악이 늦어졌다. 여기서는 그 상황을 조용히 넘기지
않고 대시보드 경고로 노출한다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import docker_ctl

MANIFEST_ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
])


class RegistryClient:
    def __init__(self, host: str, node_platform: str, enforce: bool):
        self.host = host
        self.node_platform = node_platform
        self.enforce = enforce
        self.base = f"http://{host}/v2"

    # ── HTTP ────────────────────────────────────────────────────────────
    def _get(self, path: str, accept: str | None = None, timeout: int = 10):
        req = urllib.request.Request(self.base + path)
        if accept:
            req.add_header("Accept", accept)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.headers, resp.read()

    def manifest(self, repo: str, ref: str = "latest"):
        try:
            headers, body = self._get(f"/{repo}/manifests/{ref}", MANIFEST_ACCEPT)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        try:
            data = json.loads(body)
        except ValueError:
            return None
        return {"digest": headers.get("Docker-Content-Digest"), "body": data}

    def blob(self, repo: str, digest: str):
        try:
            _headers, body = self._get(f"/{repo}/blobs/{digest}")
            return json.loads(body)
        except (urllib.error.URLError, OSError, ValueError):
            return None

    # ── 이미지 정보 ─────────────────────────────────────────────────────
    def describe(self, repo: str) -> dict | None:
        """레지스트리에 올라온 최신 이미지의 digest·플랫폼·생성시각."""
        top = self.manifest(repo)
        if top is None:
            return None
        body = top["body"]
        media = body.get("mediaType", "")
        platforms: list[str] = []

        if "index" in media or "manifest.list" in media or "manifests" in body:
            chosen = None
            for entry in body.get("manifests", []) or []:
                p = entry.get("platform") or {}
                if p.get("architecture") == "unknown":
                    continue
                label = f"{p.get('os')}/{p.get('architecture')}"
                platforms.append(label)
                if label == self.node_platform:
                    chosen = entry
            if chosen is None:
                return {"digest": top["digest"], "platforms": platforms,
                        "platform": platforms[0] if platforms else None,
                        "created": None, "matches_node": False}
            sub = self.manifest(repo, chosen["digest"])
            if sub is None:
                return {"digest": top["digest"], "platforms": platforms,
                        "platform": self.node_platform, "created": None,
                        "matches_node": True}
            body = sub["body"]

        config_digest = (body.get("config") or {}).get("digest")
        created, platform = None, None
        if config_digest:
            cfg = self.blob(repo, config_digest)
            if cfg:
                created = cfg.get("created")
                if cfg.get("os") and cfg.get("architecture"):
                    platform = f"{cfg['os']}/{cfg['architecture']}"
        if platform and platform not in platforms:
            platforms.append(platform)
        return {
            "digest": top["digest"],
            "platform": platform,
            "platforms": platforms,
            "created": created,
            "matches_node": (platform == self.node_platform) if platform else False,
        }


class ImageSync:
    """참가팀 이미지의 push 감지 → pull → 적용 판정."""

    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        # 로컬 모드만 HTTP 레지스트리 API 를 쓴다. 원격(GHCR)은 docker CLI 로 조회.
        self.client = None if cfg.remote else RegistryClient(
            cfg.registry_api, cfg.node_platform, cfg.enforce_platform)

    def _describe(self, team, role: str) -> dict | None:
        if self.cfg.remote:
            info = docker_ctl.imagetools_describe(self.cfg.image_ref(team, role))
            if info is None:
                return None
            platforms = info.get("platforms") or []
            node = self.cfg.node_platform
            platform = node if node in platforms else (platforms[0] if platforms else None)
            return {"digest": info.get("digest"), "platform": platform,
                    "platforms": platforms, "created": None,
                    "matches_node": node in platforms if platforms else False}
        return self.client.describe(f"{team.slug}/{role}")

    def observe(self, team, role: str) -> dict:
        """레지스트리 상태만 갱신(참가자 페이지의 '마지막 Push' 표시용)."""
        info = self._describe(team, role)
        slot = self.store.images.setdefault(team.id, {}).setdefault(
            role, {"pushed_at": None, "pulled_at": None, "digest": None,
                   "running_digest": None, "platform": None, "applied": False,
                   "warning": None})
        if info is None:
            slot["warning"] = "레지스트리에 이미지 없음 — 기본 이미지로 동작"
            return slot
        slot["pushed_at"] = info.get("created")
        slot["digest"] = info.get("digest")
        slot["platform"] = info.get("platform")
        slot["platforms"] = info.get("platforms")
        return slot

    def pull(self, team, role: str) -> dict:
        """대회 노드의 자동 pull. 플랫폼이 맞지 않으면 기존 컨테이너를 유지한다."""
        ref = self.cfg.image_ref(team, role)
        slot = self.observe(team, role)
        info = self._describe(team, role)
        if info is None:
            slot["applied"] = False
            return slot

        if self.cfg.enforce_platform and not info.get("matches_node"):
            have = info.get("platform") or ", ".join(info.get("platforms") or []) or "unknown"
            slot["applied"] = False
            slot["warning"] = (
                f"이미지 플랫폼 {have} · 실행 노드 {self.cfg.node_platform} — "
                f"미반영. 직전 적용 이미지로 계속 실행합니다.")
            self.store.log(
                f"team{team.id} {role}: 플랫폼 불일치({have}) — pull 건너뜀")
            return slot

        ok, out = docker_ctl.pull(ref, self.cfg.node_platform if self.cfg.enforce_platform else None)
        if not ok:
            slot["applied"] = False
            slot["warning"] = f"pull 실패: {out.splitlines()[-1] if out else '알 수 없음'}"
            self.store.log(f"team{team.id} {role}: pull 실패")
            return slot

        platform, _digest = docker_ctl.image_platform(ref)
        slot["pulled_at"] = __import__("time").time()
        slot["running_digest"] = info.get("digest")
        slot["platform"] = platform or slot.get("platform")
        slot["applied"] = True
        slot["warning"] = None
        self.store.log(f"team{team.id} {role}: pull 완료 {(info.get('digest') or '')[:19]}")
        return slot
