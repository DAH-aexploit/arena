#!/usr/bin/env python3
"""DAH Arena Gateway — 팀 진입점 + 방어 broker(userspace 구현).

리허설 인프라의 team router(iptables DNAT + NFQUEUE + broker 바이너리)를 커널
기능 없이 재현한다. nfnetlink_queue 가 없는 호스트(macOS의 Docker Desktop 등)
에서도 방어 에이전트를 대회와 동일한 계약으로 구동하는 것이 목적이다.

  공격자 / SLA 검사기
    → gateway 진입 포트(8082...)
    → 방어 broker → 방어 에이전트 (ACCEPT/DROP)
    → 챌린지 컨테이너
  챌린지의 응답은 에이전트를 거치지 않는다 (inbound-only).

broker 프로토콜은 docs/agent-guide.md 와 동일하다.

  | 0x01 PACKET    | broker→agent | type(1) pkt_id(8) pkt_len(2) raw_ip(pkt_len) |
  | 0x02 VERDICT   | agent→broker | type(1) pkt_id(8) verdict(1)  0=ACCEPT 1=DROP |
  | 0x05 HEARTBEAT | agent→broker | type(1)                                       |

  verdict 300ms 미수신 → DROP / heartbeat 3초 침묵 → 연결 해제 /
  에이전트 미접속 → 전 패킷 fail-open ACCEPT
"""

from __future__ import annotations

import json
import os
import random
import socket
import struct
import sys
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MSG_PACKET, MSG_VERDICT, MSG_HEARTBEAT = 0x01, 0x02, 0x05
ACCEPT, DROP = 0x00, 0x01

TEAM = os.environ.get("TEAM", "1")
ENTRY_IP = os.environ.get("ENTRY_IP", "10.0.0.4")
AGENT_SOCKET_PATH = os.environ.get("AGENT_SOCKET_PATH", "/tmp/broker/agent.sock")
VERDICT_TIMEOUT = int(os.environ.get("VERDICT_TIMEOUT_MS", "300")) / 1000.0
HEARTBEAT_TIMEOUT = float(os.environ.get("HEARTBEAT_TIMEOUT", "3"))
FORWARD_CONTROL_PACKETS = os.environ.get("FORWARD_CONTROL_PACKETS", "1") == "1"
SYN_LIMIT = float(os.environ.get("SYN_LIMIT", "10"))
SYN_BURST = float(os.environ.get("SYN_BURST", "20"))
CAPTURE_DIR = os.environ.get("CAPTURE_DIR", "/captures")
CAPTURE = os.environ.get("CAPTURE", "1") == "1"
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9090"))
MAX_PAYLOAD = int(os.environ.get("MAX_SEGMENT", "1400"))
DROP_ACTION = os.environ.get("DROP_ACTION", "discard")  # discard | reset

FAKE_SRC_MAC = bytes.fromhex("02421f000001")
FAKE_DST_MAC = bytes.fromhex("0242ac1f0001")


def log(msg: str) -> None:
    print(f"[gateway team{TEAM}] {msg}", flush=True)


# ── 패킷 합성 ──────────────────────────────────────────────────────────────
def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) | data[i + 1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def build_ipv4_tcp(src_ip: str, dst_ip: str, sport: int, dport: int,
                   seq: int, ack: int, flags: int, payload: bytes,
                   ip_id: int, window: int = 64240) -> bytes:
    """방어 에이전트에 전달할 raw IPv4 패킷을 만든다(옵션 없는 20+20 헤더)."""
    tcp_len = 20 + len(payload)
    total_len = 20 + tcp_len
    src = socket.inet_aton(src_ip)
    dst = socket.inet_aton(dst_ip)

    ip_hdr = struct.pack(">BBHHHBBH4s4s", 0x45, 0, total_len, ip_id & 0xFFFF,
                         0x4000, 62, socket.IPPROTO_TCP, 0, src, dst)
    ip_hdr = ip_hdr[:10] + struct.pack(">H", _checksum(ip_hdr)) + ip_hdr[12:]

    tcp_hdr = struct.pack(">HHIIBBHHH", sport, dport, seq & 0xFFFFFFFF,
                          ack & 0xFFFFFFFF, 0x50, flags, window, 0, 0)
    pseudo = struct.pack(">4s4sBBH", src, dst, 0, socket.IPPROTO_TCP, tcp_len)
    csum = _checksum(pseudo + tcp_hdr + payload)
    tcp_hdr = tcp_hdr[:16] + struct.pack(">H", csum) + tcp_hdr[18:]

    return ip_hdr + tcp_hdr + payload


# ── pcap 기록 ─────────────────────────────────────────────────────────────
class PcapWriter:
    """리허설 아티팩트와 같은 형식(libpcap 2.4 / linktype 1 Ethernet)으로 기록."""

    def __init__(self, layer: int):
        self.layer = layer
        self._lock = threading.Lock()
        self._fh = None
        self.path = None

    def open(self, tag: str) -> None:
        if not CAPTURE:
            return
        with self._lock:
            self._close_locked()
            os.makedirs(CAPTURE_DIR, exist_ok=True)
            stamp = time.strftime("%Y%m%d%H%M%S")
            name = f"team{TEAM}-{tag}-l{self.layer}-{stamp}.pcap"
            self.path = os.path.join(CAPTURE_DIR, name)
            try:
                fh = open(self.path, "wb")
                fh.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, 1))
                fh.flush()
                self._fh = fh
            except OSError as e:
                log(f"pcap 열기 실패 l{self.layer}: {e}")
                self._fh, self.path = None, None

    def write(self, ip_packet: bytes) -> None:
        if self._fh is None:
            return
        frame = FAKE_DST_MAC + FAKE_SRC_MAC + b"\x08\x00" + ip_packet
        now = time.time()
        hdr = struct.pack("<IIII", int(now), int((now % 1) * 1_000_000),
                          len(frame), len(frame))
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.write(hdr + frame)
            except OSError:
                pass

    def flush(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                except OSError:
                    pass

    def _close_locked(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()


# ── 방어 에이전트 링크 ─────────────────────────────────────────────────────
class AgentLink:
    """AF_UNIX/SOCK_SEQPACKET broker 소켓 서버. 에이전트 1개만 연결한다."""

    def __init__(self, path: str, metrics: "Metrics"):
        self.path = path
        self.metrics = metrics
        self._conn = None
        self._conn_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, list] = {}
        self._pending_lock = threading.Lock()
        self._next_id = 1
        self._last_heartbeat = 0.0
        self.connected_since = 0.0

    # 서버
    def serve_forever(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except OSError:
            pass
        if os.path.exists(self.path):
            os.unlink(self.path)
        try:
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        except OSError as e:
            # SOCK_SEQPACKET 은 Linux 컨테이너 전용. macOS 호스트에서 직접 실행하면
            # 여기서 멈춘다(정상 — gateway 는 컨테이너에서 구동한다).
            log(f"SOCK_SEQPACKET 미지원 — 컨테이너에서 실행하세요: {e}")
            return
        srv.bind(self.path)
        os.chmod(self.path, 0o666)
        srv.listen(4)
        log(f"broker socket listen {self.path}")
        threading.Thread(target=self._heartbeat_watchdog, daemon=True).start()
        while True:
            try:
                conn, _ = srv.accept()
            except OSError as e:
                log(f"accept 오류: {e}")
                time.sleep(0.5)
                continue
            self._attach(conn)

    def _attach(self, conn: socket.socket) -> None:
        with self._conn_lock:
            old = self._conn
            self._conn = conn
            self._last_heartbeat = time.time()
            self.connected_since = time.time()
            self.metrics.agent_connects += 1
        if old is not None:
            try:
                old.close()
            except OSError:
                pass
        log("방어 에이전트 연결")
        threading.Thread(target=self._reader, args=(conn,), daemon=True).start()

    def _reader(self, conn: socket.socket) -> None:
        while True:
            try:
                data = conn.recv(64)
            except OSError:
                break
            if not data:
                break
            kind = data[0]
            if kind == MSG_HEARTBEAT:
                self._last_heartbeat = time.time()
            elif kind == MSG_VERDICT and len(data) >= 10:
                pkt_id, verdict = struct.unpack_from(">QB", data, 1)
                self._resolve(pkt_id, verdict)
            else:
                self.metrics.protocol_errors += 1
        self._detach(conn, "연결 종료")

    def _detach(self, conn: socket.socket, why: str) -> None:
        with self._conn_lock:
            if self._conn is conn:
                self._conn = None
                self.connected_since = 0.0
        try:
            conn.close()
        except OSError:
            pass
        with self._pending_lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for w in waiters:
            w[0] = None
            w[1].set()
        log(f"방어 에이전트 해제 ({why}) — 이후 패킷 fail-open ACCEPT")

    def _heartbeat_watchdog(self) -> None:
        while True:
            time.sleep(0.5)
            with self._conn_lock:
                conn = self._conn
                last = self._last_heartbeat
            if conn is None:
                continue
            if time.time() - last > HEARTBEAT_TIMEOUT:
                self.metrics.heartbeat_timeouts += 1
                self._detach(conn, f"heartbeat {HEARTBEAT_TIMEOUT}s 침묵")

    def _resolve(self, pkt_id: int, verdict: int) -> None:
        with self._pending_lock:
            waiter = self._pending.pop(pkt_id, None)
        if waiter is None:
            self.metrics.late_verdicts += 1
            return
        waiter[0] = verdict
        waiter[1].set()

    @property
    def connected(self) -> bool:
        with self._conn_lock:
            return self._conn is not None

    def verdict_for(self, raw_ip: bytes) -> tuple[int, float, bool]:
        """(verdict, 소요초, 에이전트판정여부). 미연결이면 fail-open ACCEPT."""
        with self._conn_lock:
            conn = self._conn
        if conn is None:
            return ACCEPT, 0.0, False
        if len(raw_ip) > 65535:
            raw_ip = raw_ip[:65535]

        with self._pending_lock:
            pkt_id = self._next_id
            self._next_id = (self._next_id + 1) & 0xFFFFFFFFFFFFFFFF or 1
            waiter = [None, threading.Event()]
            self._pending[pkt_id] = waiter

        frame = struct.pack(">BQH", MSG_PACKET, pkt_id, len(raw_ip)) + raw_ip
        started = time.perf_counter()
        try:
            with self._write_lock:
                conn.send(frame)
        except OSError:
            with self._pending_lock:
                self._pending.pop(pkt_id, None)
            self._detach(conn, "송신 실패")
            return ACCEPT, 0.0, False

        got = waiter[1].wait(VERDICT_TIMEOUT)
        elapsed = time.perf_counter() - started
        if not got:
            with self._pending_lock:
                self._pending.pop(pkt_id, None)
            self.metrics.verdict_timeouts += 1
            return DROP, elapsed, True
        if waiter[0] is None:  # 대기 중 연결 해제 → fail-open
            return ACCEPT, elapsed, False
        return (DROP if waiter[0] == DROP else ACCEPT), elapsed, True


# ── 지표 ──────────────────────────────────────────────────────────────────
class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()
        self.agent_connects = 0
        self.protocol_errors = 0
        self.late_verdicts = 0
        self.heartbeat_timeouts = 0

    def reset(self) -> None:
        self.accept = 0
        self.drop = 0
        self.verdict_timeouts = 0
        self.packets = 0
        self.conn_total = 0
        self.conn_ratelimited = 0
        self.conn_rejected_layer = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self.verdict_seconds = 0.0
        self.per_port = defaultdict(lambda: {"accept": 0, "drop": 0, "timeout": 0})

    def observe(self, dport: int, verdict: int, elapsed: float, timed_out: bool) -> None:
        with self.lock:
            self.packets += 1
            self.verdict_seconds += elapsed
            slot = self.per_port[dport]
            if timed_out:
                slot["timeout"] += 1
            if verdict == DROP:
                self.drop += 1
                slot["drop"] += 1
            else:
                self.accept += 1
                slot["accept"] += 1

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "team": TEAM,
                "accept": self.accept,
                "drop": self.drop,
                "timeout": self.verdict_timeouts,
                "packets": self.packets,
                "connections": self.conn_total,
                "ratelimited": self.conn_ratelimited,
                "rejected_inactive_layer": self.conn_rejected_layer,
                "bytes_in": self.bytes_in,
                "bytes_out": self.bytes_out,
                "avg_verdict_ms": round(
                    1000 * self.verdict_seconds / self.packets, 3) if self.packets else 0.0,
                "agent_connects": self.agent_connects,
                "heartbeat_timeouts": self.heartbeat_timeouts,
                "late_verdicts": self.late_verdicts,
                "per_port": {str(k): dict(v) for k, v in self.per_port.items()},
            }


# ── 신규 연결 rate limit (source IP 기준 token bucket) ─────────────────────
class ConnRateLimiter:
    def __init__(self, rate: float, burst: float):
        self.rate, self.burst = rate, burst
        self._buckets: dict[str, list] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        if self.rate <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(ip, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < 1.0:
                self._buckets[ip] = (tokens, now)
                return False
            self._buckets[ip] = (tokens - 1.0, now)
            if len(self._buckets) > 4096:
                cutoff = now - 60
                for k in [k for k, v in self._buckets.items() if v[1] < cutoff]:
                    self._buckets.pop(k, None)
            return True


# ── 레이어 프록시 ─────────────────────────────────────────────────────────
class LayerProxy(threading.Thread):
    def __init__(self, layer: int, entry_port: int, upstream: tuple[str, int],
                 agent: AgentLink, metrics: Metrics, writer: PcapWriter,
                 limiter: ConnRateLimiter, state: "GatewayState"):
        super().__init__(daemon=True, name=f"l{layer}")
        self.layer = layer
        self.entry_port = entry_port
        self.upstream = upstream
        self.agent = agent
        self.metrics = metrics
        self.writer = writer
        self.limiter = limiter
        self.state = state

    def run(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.entry_port))
        srv.listen(256)
        log(f"L{self.layer} 진입 :{self.entry_port} → {self.upstream[0]}:{self.upstream[1]}")
        while True:
            try:
                conn, addr = srv.accept()
            except OSError:
                time.sleep(0.2)
                continue
            threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()

    def _handle(self, conn: socket.socket, addr) -> None:
        src_ip, src_port = addr[0], addr[1]
        with self.metrics.lock:
            self.metrics.conn_total += 1
        if self.layer not in self.state.active_layers:
            with self.metrics.lock:
                self.metrics.conn_rejected_layer += 1
            conn.close()
            return
        if not self.limiter.allow(src_ip):
            with self.metrics.lock:
                self.metrics.conn_ratelimited += 1
            conn.close()
            return

        flow = Flow(src_ip, src_port, ENTRY_IP, self.entry_port)
        conn.settimeout(30)

        # SYN 합성 — 실제 NFQUEUE FORWARD 체인처럼 payload 없는 패킷도 검사 대상
        if FORWARD_CONTROL_PACKETS:
            if not self._judge(flow.syn(), self.entry_port):
                conn.close()
                return

        try:
            up = socket.create_connection(self.upstream, timeout=5)
        except OSError:
            conn.close()
            return
        up.settimeout(30)

        stop = threading.Event()
        t = threading.Thread(target=self._pump_out, args=(up, conn, flow, stop), daemon=True)
        t.start()
        try:
            self._pump_in(conn, up, flow)
        finally:
            stop.set()
            for s in (conn, up):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    s.close()
                except OSError:
                    pass

    def _judge(self, raw_ip: bytes, dport: int) -> bool:
        self.writer.write(raw_ip)
        verdict, elapsed, by_agent = self.agent.verdict_for(raw_ip)
        timed_out = by_agent and verdict == DROP and elapsed >= VERDICT_TIMEOUT
        self.metrics.observe(dport, verdict, elapsed, timed_out)
        return verdict == ACCEPT

    def _pump_in(self, client: socket.socket, up: socket.socket, flow: "Flow") -> None:
        """클라이언트 → 챌린지. 방어 에이전트 판정 대상."""
        while True:
            try:
                chunk = client.recv(MAX_PAYLOAD)
            except OSError:
                break
            if not chunk:
                if FORWARD_CONTROL_PACKETS:
                    self._judge(flow.fin(), self.entry_port)
                break
            with self.metrics.lock:
                self.metrics.bytes_in += len(chunk)
            if not self._judge(flow.data(chunk), self.entry_port):
                if DROP_ACTION == "reset":
                    break
                continue  # DROP: 상류로 전달하지 않는다(커널 NFQUEUE DROP과 동일)
            try:
                up.sendall(chunk)
            except OSError:
                break

    def _pump_out(self, up: socket.socket, client: socket.socket,
                  flow: "Flow", stop: threading.Event) -> None:
        """챌린지 → 클라이언트. 에이전트에 전달하지 않고 pcap에만 기록."""
        while not stop.is_set():
            try:
                chunk = up.recv(MAX_PAYLOAD)
            except OSError:
                break
            if not chunk:
                break
            with self.metrics.lock:
                self.metrics.bytes_out += len(chunk)
            self.writer.write(flow.response(chunk))
            try:
                client.sendall(chunk)
            except OSError:
                break


class Flow:
    """합성 TCP 흐름의 seq/ack 상태."""

    def __init__(self, src_ip: str, src_port: int, dst_ip: str, dst_port: int):
        self.src_ip, self.src_port = src_ip, src_port
        self.dst_ip, self.dst_port = dst_ip, dst_port
        self.seq = random.randint(1, 0x7FFFFFFF)
        self.rseq = random.randint(1, 0x7FFFFFFF)
        self.ip_id = random.randint(1, 0xFFFF)
        self._lock = threading.Lock()

    def _next_id(self) -> int:
        with self._lock:
            self.ip_id = (self.ip_id + 1) & 0xFFFF
            return self.ip_id

    def syn(self) -> bytes:
        pkt = build_ipv4_tcp(self.src_ip, self.dst_ip, self.src_port, self.dst_port,
                             self.seq, 0, 0x02, b"", self._next_id())
        self.seq += 1
        return pkt

    def data(self, payload: bytes) -> bytes:
        pkt = build_ipv4_tcp(self.src_ip, self.dst_ip, self.src_port, self.dst_port,
                             self.seq, self.rseq, 0x18, payload, self._next_id())
        self.seq += len(payload)
        return pkt

    def fin(self) -> bytes:
        pkt = build_ipv4_tcp(self.src_ip, self.dst_ip, self.src_port, self.dst_port,
                             self.seq, self.rseq, 0x11, b"", self._next_id())
        self.seq += 1
        return pkt

    def response(self, payload: bytes) -> bytes:
        pkt = build_ipv4_tcp(self.dst_ip, self.src_ip, self.dst_port, self.src_port,
                             self.rseq, self.seq, 0x18, payload, self._next_id())
        self.rseq += len(payload)
        return pkt


# ── 상태 · 제어 API ───────────────────────────────────────────────────────
class GatewayState:
    def __init__(self, layers: list[int]):
        self.all_layers = layers
        self.active_layers = set(layers)
        self.tag = "idle"


def make_control_handler(state: GatewayState, metrics: Metrics,
                         agent: AgentLink, writers: dict) -> type:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: str, ctype="application/json"):
            raw = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path.startswith("/metrics"):
                snap = metrics.snapshot()
                lines = [
                    f"broker_verdicts_accept_total {snap['accept']}",
                    f"broker_verdicts_drop_total {snap['drop']}",
                    f"broker_verdict_timeout_total {snap['timeout']}",
                    f"broker_gc_dropped_total {snap['ratelimited']}",
                    f"broker_packets_total {snap['packets']}",
                    f"gateway_agent_connected {1 if agent.connected else 0}",
                ]
                return self._send(200, "\n".join(lines) + "\n", "text/plain")
            if self.path.startswith("/stats"):
                snap = metrics.snapshot()
                snap["agent_connected"] = agent.connected
                snap["active_layers"] = sorted(state.active_layers)
                snap["tag"] = state.tag
                snap["captures"] = {str(l): w.path for l, w in writers.items()}
                return self._send(200, json.dumps(snap, ensure_ascii=False))
            if self.path.startswith("/healthz"):
                return self._send(200, "ok", "text/plain")
            return self._send(404, "{}")

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            if self.path.startswith("/control/round"):
                tag = str(body.get("tag", "round"))
                active = body.get("active_layers") or state.all_layers
                state.active_layers = {int(x) for x in active}
                state.tag = tag
                with metrics.lock:
                    metrics.reset()
                for layer, w in writers.items():
                    if layer in state.active_layers:
                        w.open(tag)
                    else:
                        w.close()
                log(f"라운드 시작 tag={tag} active={sorted(state.active_layers)}")
                return self._send(200, json.dumps({"ok": True}))
            if self.path.startswith("/control/stop"):
                for w in writers.values():
                    w.close()
                state.active_layers = set()
                log("라운드 종료 — 캡처 종료")
                return self._send(200, json.dumps(
                    {"ok": True, "captures": {str(l): w.path for l, w in writers.items()}}))
            if self.path.startswith("/control/flush"):
                for w in writers.values():
                    w.flush()
                return self._send(200, json.dumps({"ok": True}))
            return self._send(404, "{}")

    return Handler


def main() -> None:
    layers = [int(x) for x in os.environ.get("LAYERS", "1,2,3").split(",") if x.strip()]
    metrics = Metrics()
    agent = AgentLink(AGENT_SOCKET_PATH, metrics)
    limiter = ConnRateLimiter(SYN_LIMIT, SYN_BURST)
    state = GatewayState(layers)
    writers: dict[int, PcapWriter] = {}

    threading.Thread(target=agent.serve_forever, daemon=True).start()

    for layer in layers:
        entry = int(os.environ.get(f"ENTRY_L{layer}", str(8081 + layer)))
        target = os.environ.get(f"CHAL_L{layer}", "")
        if not target:
            log(f"L{layer}: CHAL_L{layer} 미설정 → 스킵")
            continue
        host, _, port = target.partition(":")
        writer = PcapWriter(layer)
        writers[layer] = writer
        LayerProxy(layer, entry, (host, int(port or entry)),
                   agent, metrics, writer, limiter, state).start()

    handler = make_control_handler(state, metrics, agent, writers)
    srv = ThreadingHTTPServer(("0.0.0.0", METRICS_PORT), handler)
    log(f"제어·지표 :{METRICS_PORT} (verdict timeout {int(VERDICT_TIMEOUT*1000)}ms, "
        f"rate {SYN_LIMIT:g}/s burst {SYN_BURST:g})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        for w in writers.values():
            w.close()
        sys.exit(0)


if __name__ == "__main__":
    main()
