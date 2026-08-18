#!/usr/bin/env python3
"""LiteLLM 호환 게이트웨이 (로컬 재현).

대회의 LiteLLM Proxy 계약 중 참가자 관점에서 관찰 가능한 부분을 재현한다.

  - POST /v1/chat/completions (Bearer 인증, OpenAI 호환)
  - GET  /v1/models
  - drop_params: 지원하지 않는 파라미터를 에러 없이 무시
  - 서버 호스팅 tools(web_search·code_interpreter·file_search·mcp) 차단
  - usage(completion/prompt/reasoning/cached token) 반환
  - 팀별 budget 상한 도달 시 budget-exceeded 로 요청 거부
  - reasoning token 을 completion token 에 포함해 출력 단가 적용

AZURE_API_BASE(또는 LLM_UPSTREAM_BASE)가 설정되면 upstream 으로 투명 전달하고,
없으면 결정적 목 응답을 돌려주어 키 없이도 인프라 검증이 가능하다.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-local")
BUDGET = float(os.environ.get("ARENA_BUDGET_USD", "20"))
ORCH_URL = os.environ.get("ORCH_URL", "http://orchestrator:4100").rstrip("/")
UPSTREAM_BASE = (os.environ.get("AZURE_API_BASE")
                 or os.environ.get("LLM_UPSTREAM_BASE") or "").rstrip("/")
UPSTREAM_KEY = os.environ.get("AZURE_API_KEY") or os.environ.get("LLM_UPSTREAM_KEY") or ""

# docs/agent-guide.md 의 지원 모델과 대략적 단가(USD / 1M tokens). 목 비용 산정용.
PRICING = {
    "gpt-4.1-mini": (0.4, 1.6), "gpt-4.1": (2.0, 8.0), "gpt-4o": (2.5, 10.0),
    "gpt-5": (1.25, 10.0), "gpt-5.2": (1.25, 10.0), "gpt-5-mini": (0.25, 2.0),
    "gpt-5.4": (0.25, 2.0), "gpt-5.6-luna": (0.25, 2.0), "gpt-5.6-terra": (0.25, 2.0),
    "gpt-5.6-sol": (0.25, 2.0), "o3": (2.0, 8.0), "o4-mini": (1.1, 4.4),
    "gpt-5-pro": (15.0, 120.0), "gpt-5.4-pro": (15.0, 120.0), "gpt-5-codex": (1.25, 10.0),
    "gpt-5.1-codex": (1.25, 10.0), "gpt-5.1-codex-mini": (0.25, 2.0),
    "gpt-5.1-codex-max": (1.25, 10.0), "gpt-5.2-codex": (1.25, 10.0),
    "gpt-5.3-codex": (1.25, 10.0),
}
DEFAULT_PRICE = (0.5, 2.0)

_spend: dict[str, float] = {}
_spend_lock = threading.Lock()


def team_of(api_key: str) -> int | None:
    """에이전트 키는 `t<id>-<secret>` 형식. 팀 id 를 뽑아 비용을 귀속한다."""
    if api_key and api_key.startswith("t"):
        head = api_key[1:].split("-", 1)[0]
        if head.isdigit():
            return int(head)
    return None


def report_usage(api_key: str, usd: float) -> None:
    with _spend_lock:
        _spend[api_key] = _spend.get(api_key, 0.0) + usd
    team = team_of(api_key)
    if team is None:
        return
    body = json.dumps({"team": team, "usd": usd}).encode()
    req = urllib.request.Request(f"{ORCH_URL}/api/llm/usage", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except (urllib.error.URLError, OSError):
        pass


def total_spend(api_key: str) -> float:
    with _spend_lock:
        return _spend.get(api_key, 0.0)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def mock_completion(model: str, messages: list) -> tuple[str, dict]:
    prompt = "\n".join(str(m.get("content", "")) for m in messages)
    prompt_tokens = estimate_tokens(prompt)
    # 목 응답: 방어 분석용 JSON 형태 힌트를 준다(에이전트가 파싱 실패해도 안전).
    content = json.dumps({
        "verdict": "observe",
        "reason": "mock gateway: no upstream configured",
        "confidence": 0.0,
    })
    completion_tokens = estimate_tokens(content)
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "completion_tokens_details": {"reasoning_tokens": 0},
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    return content, usage


def price_of(model: str, usage: dict) -> float:
    pin, pout = PRICING.get(model, DEFAULT_PRICE)
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)  # reasoning 포함(출력 단가)
    return (prompt * pin + completion * pout) / 1_000_000.0


def proxy_upstream(model: str, payload: dict) -> tuple[int, dict]:
    url = f"{UPSTREAM_BASE}/chat/completions"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {UPSTREAM_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": {"message": "upstream error"}}
    except (urllib.error.URLError, OSError) as e:
        return 502, {"error": {"message": f"upstream unreachable: {e}"}}


# drop_params: 모델별 미지원 파라미터를 제거(에러 대신 무시)
_REASONING_MODELS = re.compile(r"^(o\d|gpt-5)", re.I)


def drop_params(model: str, payload: dict) -> dict:
    out = dict(payload)
    if _REASONING_MODELS.match(model):
        out.pop("temperature", None)
        out.pop("top_p", None)
        # reasoning 모델은 max_tokens 대신 max_completion_tokens
        if "max_tokens" in out and "max_completion_tokens" not in out:
            out["max_completion_tokens"] = out.pop("max_tokens")
    out.pop("stream", None)  # 이 게이트웨이는 스트리밍 비지원
    return out


def blocked_tools(payload: dict) -> set:
    bad = set()
    for t in payload.get("tools") or []:
        if isinstance(t, dict):
            ty = str(t.get("type") or "").strip().lower()
            if ty and ty != "function":
                bad.add(ty)
    if payload.get("web_search_options"):
        bad.add("web_search_options")
    return bad


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, payload: dict, extra=None):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _auth(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        return auth[7:]

    def do_GET(self):
        if self.path.startswith("/health") or self.path == "/":
            return self._send(200, {"status": "ok", "upstream": bool(UPSTREAM_BASE)})
        if self.path.startswith("/v1/models"):
            return self._send(200, {"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "dah-arena"} for m in PRICING]})
        return self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            return self._send(404, {"error": {"message": "not found"}})

        api_key = self._auth()
        if not api_key:
            return self._send(401, {"error": {"message": "missing bearer token",
                                               "type": "authentication_error"}})
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            return self._send(400, {"error": {"message": "invalid json"}})

        model = str(payload.get("model", "gpt-4.1-mini"))

        bad = blocked_tools(payload)
        if bad:
            return self._send(400, {"error": {
                "message": ("Server-hosted tools are disabled in this competition: "
                            + ", ".join(sorted(bad))),
                "type": "invalid_request_error", "code": "hosted_tools_blocked"}})

        if total_spend(api_key) >= BUDGET:
            return self._send(429, {"error": {
                "message": f"team budget of ${BUDGET:.2f} exceeded",
                "type": "budget_exceeded", "code": "budget-exceeded"}})

        payload = drop_params(model, payload)

        if UPSTREAM_BASE:
            code, resp = proxy_upstream(model, payload)
            usage = resp.get("usage") or {}
        else:
            content, usage = mock_completion(model, payload.get("messages") or [])
            resp = {
                "id": f"chatcmpl-mock-{int(time.time()*1000)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}],
                "usage": usage,
            }
            code = 200

        if code == 200 and usage:
            cost = price_of(model, usage)
            report_usage(api_key, cost)
            resp.setdefault("usage", usage)
            resp["usage"]["estimated_cost_usd"] = round(cost, 6)
        return self._send(code, resp)


def main() -> None:
    port = int(os.environ.get("PORT", "4000"))
    mode = "upstream" if UPSTREAM_BASE else "mock"
    print(f"[litellm-gw] {mode} 모드 — budget ${BUDGET:.2f} :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
