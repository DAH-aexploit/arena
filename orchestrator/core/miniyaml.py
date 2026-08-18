"""설정 파일용 최소 YAML 서브셋 파서.

호스트(compose 생성)와 컨테이너(오케스트레이터) 양쪽에서 외부 의존성 없이
config/competition.yml 을 읽기 위한 것이다. 지원 범위는 이 파일이 쓰는 문법
(들여쓰기 매핑 · `- ` 리스트 · 인라인 리스트 · 주석 · 기본 스칼라)뿐이다.
"""

from __future__ import annotations


def _scalar(text: str):
    text = text.strip()
    if not text:
        return ""
    if text[0] in "\"'" and text[-1] == text[0] and len(text) >= 2:
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(p) for p in inner.split(",")] if inner else []
    low = text.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", "none"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip()


def _lines(text: str):
    for raw in text.splitlines():
        body = _strip_comment(raw)
        if not body.strip():
            continue
        yield len(body) - len(body.lstrip(" ")), body.strip()


def loads(text: str):
    items = list(_lines(text))
    value, _ = _parse_block(items, 0, 0)
    return value


def load(path: str):
    with open(path, encoding="utf-8") as fh:
        return loads(fh.read())


def _parse_block(items, idx: int, indent: int):
    if idx >= len(items):
        return None, idx
    if items[idx][1].startswith("- "):
        return _parse_list(items, idx, indent)
    return _parse_map(items, idx, indent)


def _parse_map(items, idx: int, indent: int):
    result = {}
    while idx < len(items):
        ind, body = items[idx]
        if ind < indent or body.startswith("- ") and ind == indent:
            break
        if ind > indent:  # 상위에서 소비하지 못한 줄 — 방어적으로 스킵
            idx += 1
            continue
        key, _, rest = body.partition(":")
        key, rest = key.strip(), rest.strip()
        idx += 1
        if rest:
            result[key] = _scalar(rest)
            continue
        if idx < len(items) and items[idx][0] > indent:
            child, idx = _parse_block(items, idx, items[idx][0])
            result[key] = child
        elif idx < len(items) and items[idx][0] == indent and items[idx][1].startswith("- "):
            child, idx = _parse_list(items, idx, indent)
            result[key] = child
        else:
            result[key] = None
    return result, idx


def _parse_list(items, idx: int, indent: int):
    result = []
    while idx < len(items):
        ind, body = items[idx]
        if ind != indent or not body.startswith("- "):
            break
        entry = body[2:].strip()
        idx += 1
        if ":" in entry and not entry.startswith("["):
            # 리스트 원소가 매핑인 경우: 첫 줄을 같은 들여쓰기의 매핑으로 취급
            sub = [(indent + 2, entry)]
            while idx < len(items) and items[idx][0] > indent:
                sub.append(items[idx])
                idx += 1
            value, _ = _parse_map(sub, 0, indent + 2)
            result.append(value)
        else:
            result.append(_scalar(entry))
    return result, idx
