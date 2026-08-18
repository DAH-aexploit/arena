#!/usr/bin/env python3
"""패스스루 방어 에이전트 — 기준선(ACCEPT-all).

봇팀과, 아직 자기 이미지를 올리지 않은 참가팀의 기본 방어 에이전트다.
broker 계약(PACKET/VERDICT/HEARTBEAT, 300ms, 1s heartbeat)을 그대로 지키되
모든 패킷을 ACCEPT 한다. 리허설 초기의 "실질적 ACCEPT-all" 상태와 동일하며,
봇팀은 매 라운드 flag 를 내주는 상대 역할을 한다.

이 파일은 참가팀이 교체할 방어 에이전트의 최소 골격이기도 하다. verdict 를
DROP 으로 바꾸는 판정 로직만 얹으면 대회와 동일한 인터페이스로 동작한다.
"""

from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import time

AGENT_SOCKET = os.environ.get("AGENT_SOCKET", "/run/agent.sock")
MSG_PACKET, MSG_VERDICT, MSG_HEARTBEAT = 0x01, 0x02, 0x05
ACCEPT, DROP = 0x00, 0x01
HEADER = 1 + 8 + 2
MAX_MSG = HEADER + 65535


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} agent {msg}", flush=True)


def make_verdict(pkt_id: int, raw_ip: bytes) -> int:
    """기준선: 전부 ACCEPT. (참가팀은 이 함수를 교체한다.)"""
    return ACCEPT


def heartbeat_loop(sock: socket.socket, lock: threading.Lock, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            with lock:
                sock.send(bytes([MSG_HEARTBEAT]))
        except OSError:
            return
        time.sleep(1.0)


def dial(path: str, attempts: int = 60) -> socket.socket:
    for i in range(attempts):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            s.connect(path)
            return s
        except OSError:
            if i == 0:
                log(f"waiting for broker socket {path}")
            time.sleep(1.0)
    raise SystemExit(f"broker socket {path} 연결 실패")


def main() -> None:
    sock = dial(AGENT_SOCKET)
    log("engine ready: passthrough (ACCEPT-all), heartbeat=1s, budget=300ms")
    lock = threading.Lock()
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(sock, lock, stop), daemon=True).start()

    accepted = 0
    while True:
        try:
            data = sock.recv(MAX_MSG)
        except OSError:
            break
        if not data:
            break
        if data[0] != MSG_PACKET or len(data) < HEADER:
            continue
        pkt_id = struct.unpack_from(">Q", data, 1)[0]
        pkt_len = struct.unpack_from(">H", data, 9)[0]
        raw_ip = data[HEADER:HEADER + pkt_len]
        verdict = make_verdict(pkt_id, raw_ip)
        try:
            with lock:
                sock.send(struct.pack(">BQB", MSG_VERDICT, pkt_id, verdict))
        except OSError:
            break
        accepted += 1
        if accepted % 500 == 0:
            log(f"processed {accepted} packets (ACCEPT-all)")

    stop.set()
    log("broker disconnected — exiting")


if __name__ == "__main__":
    main()
