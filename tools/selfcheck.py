#!/usr/bin/env python3
"""도커 없이 핵심 로직을 점검한다.

  - config 로딩과 Phase/Round 매핑
  - 채점(state) 흐름
  - gateway 패킷 합성과 pcap 헤더
  - broker 프레임 왕복(로컬 소켓)
"""

from __future__ import annotations

import os
import socket
import struct
import sys
import tempfile
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "orchestrator"))
sys.path.insert(0, os.path.join(ROOT, "gateway"))

from core import config, state  # noqa: E402

PASS, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
_fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _fail
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def test_config() -> "config.Config":
    print("config")
    cfg = config.load(os.path.join(ROOT, "config", "competition.yml"))
    check("총 라운드 = 16", cfg.total_rounds == 16, str(cfg.total_rounds))
    # Phase 2 Round 1 = 전역 Round 7
    loc = cfg.locate(7)
    check("전역 R7 = Phase 2 Round 1", loc and loc[0].index == 2 and loc[1] == 1,
          str(loc))
    # Phase 3 Round 1 = 전역 Round 13
    loc = cfg.locate(13)
    check("전역 R13 = Phase 3 Round 1", loc and loc[0].index == 3 and loc[1] == 1,
          str(loc))
    check("R1 활성 레이어 [1]", cfg.active_layers(1) == [1], str(cfg.active_layers(1)))
    check("R7 활성 레이어 [1,2]", cfg.active_layers(7) == [1, 2], str(cfg.active_layers(7)))
    check("R13 활성 레이어 [1,2,3]", cfg.active_layers(13) == [1, 2, 3])
    check("R10 태그 = P2-R10", cfg.round_tag(10) == "P2-R10", cfg.round_tag(10))
    check("참가팀 1팀(team9)", [t.id for t in cfg.participants] == [9])
    # 토큰 결정성
    t9 = cfg.team(9)
    check("토큰 결정적", t9.secret("submit", cfg.secret_salt) ==
          t9.secret("submit", cfg.secret_salt))
    return cfg


def test_scoring(cfg) -> None:
    print("scoring")
    store = state.Store(cfg)
    # 전역 R7(Phase2, L1·L2) 시뮬레이션
    store.begin_round(7, 1200)
    layers = cfg.active_layers(7)
    issued = store.rotate_flags(cfg.teams, layers)
    check("flag 발급 = 팀×레이어", len(issued) == len(cfg.teams) * len(layers),
          str(len(issued)))

    # team9 가 team1 의 L1 flag 를 탈취
    victim_flag = issued[(1, 1)]
    t9_token = None  # submit 은 team_id 로 직접 호출
    r = store.submit(victim_flag, 9)
    check("정상 제출 accepted", r["status"] == "accepted", r["status"])
    r2 = store.submit(victim_flag, 9)
    check("중복 제출 duplicate", r2["status"] == "duplicate", r2["status"])
    r3 = store.submit(issued[(9, 1)], 9)
    check("자팀 flag own_team", r3["status"] == "own_team", r3["status"])

    # SLA·verdict 기록
    store.record_sla(9, 98, 100)
    store.record_gateway(9, {"accept": 500, "drop": 40, "timeout": 0,
                             "packets": 540, "agent_connected": True})
    summary = store.end_round()
    t9 = summary["teams"][9]
    check("team9 captured=1", t9["captured"] == 1, str(t9["captured"]))
    check("team1 stolen L1", summary["teams"][1]["stolen"] == 1)
    check("team9 SLA 0.98", abs(t9["sla_ratio"] - 0.98) < 1e-6, str(t9["sla_ratio"]))
    check("team9 방어 유지 L2", 2 in t9["defended_layers"], str(t9["defended_layers"]))

    totals = store.totals()
    check("누적 점수 계산됨", totals[9]["score"] > 0, str(totals[9]))


def test_gateway_packets() -> None:
    print("gateway packet 합성")
    import gateway
    pkt = gateway.build_ipv4_tcp("10.9.0.9", "10.9.0.4", 40000, 8082,
                                 1000, 2000, 0x18, b"GET / HTTP/1.0\r\n\r\n", 1)
    check("IPv4 version/ihl", pkt[0] == 0x45)
    check("protocol=TCP", pkt[9] == 6)
    # IP 체크섬 검증
    check("IP 체크섬 유효", gateway._checksum(pkt[:20]) == 0)
    # TCP 체크섬 검증(의사헤더 포함)
    src = socket.inet_aton("10.9.0.9")
    dst = socket.inet_aton("10.9.0.4")
    tcp = pkt[20:]
    pseudo = struct.pack(">4s4sBBH", src, dst, 0, 6, len(tcp))
    check("TCP 체크섬 유효", gateway._checksum(pseudo + tcp) == 0)

    # pcap writer 헤더
    with tempfile.TemporaryDirectory() as d:
        gateway.CAPTURE_DIR = d
        gateway.CAPTURE = True
        w = gateway.PcapWriter(1)
        w.open("P1-R1")
        w.write(pkt)
        w.close()
        with open(w.path, "rb") as fh:
            magic = fh.read(4)
        check("pcap magic (le)", magic == b"\xd4\xc3\xb2\xa1", magic.hex())


def _seqpacket_supported() -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        s.close()
        return True
    except OSError:
        return False


def test_broker_roundtrip() -> None:
    print("broker 프레임 왕복")
    if not _seqpacket_supported():
        print(f"  \033[33m⏭\033[0m AF_UNIX/SOCK_SEQPACKET 미지원 호스트(macOS) — "
              "컨테이너 안에서만 검증 가능하므로 건너뜀")
        return
    import gateway
    with tempfile.TemporaryDirectory() as d:
        sock_path = os.path.join(d, "agent.sock")
        metrics = gateway.Metrics()
        link = gateway.AgentLink(sock_path, metrics)
        threading.Thread(target=link.serve_forever, daemon=True).start()

        # 미연결 상태: fail-open ACCEPT
        v, _e, by = link.verdict_for(b"\x45" + b"\x00" * 39)
        check("미연결 fail-open ACCEPT", v == gateway.ACCEPT and not by)

        # 가짜 방어 에이전트: 8082 로 향하는 패킷만 DROP
        def fake_agent():
            for _ in range(50):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                    s.connect(sock_path)
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                return
            s.send(bytes([gateway.MSG_HEARTBEAT]))
            while True:
                data = s.recv(70000)
                if not data or data[0] != gateway.MSG_PACKET:
                    break
                pkt_id = struct.unpack_from(">Q", data, 1)[0]
                pkt_len = struct.unpack_from(">H", data, 9)[0]
                raw = data[11:11 + pkt_len]
                dport = struct.unpack_from(">H", raw, 22)[0] if len(raw) >= 24 else 0
                verdict = gateway.DROP if dport == 8082 else gateway.ACCEPT
                s.send(struct.pack(">BQB", gateway.MSG_VERDICT, pkt_id, verdict))

        threading.Thread(target=fake_agent, daemon=True).start()
        time.sleep(0.5)
        check("에이전트 연결됨", link.connected)

        drop_pkt = gateway.build_ipv4_tcp("10.9.0.9", "10.9.0.4", 40000, 8082,
                                          1, 1, 0x18, b"attack", 1)
        v, _e, by = link.verdict_for(drop_pkt)
        check("8082 → DROP", v == gateway.DROP and by, f"v={v} by={by}")

        accept_pkt = gateway.build_ipv4_tcp("10.9.0.9", "10.9.0.4", 40000, 8083,
                                            1, 1, 0x18, b"ok", 1)
        v, _e, by = link.verdict_for(accept_pkt)
        check("8083 → ACCEPT", v == gateway.ACCEPT and by, f"v={v} by={by}")


def main() -> None:
    cfg = test_config()
    test_scoring(cfg)
    test_gateway_packets()
    test_broker_roundtrip()
    print()
    if _fail:
        print(f"\033[31m{_fail}개 실패\033[0m")
        sys.exit(1)
    print("\033[32m전체 통과\033[0m")


if __name__ == "__main__":
    main()
