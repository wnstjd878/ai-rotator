"""
== 운영 맥락 ==
용도: 계정별 남은 사용량을 화면에 적을 글자로 바꾼다. claude_pool·codex_pool 의 --status 가 쓴다.
실행 시점: 사람이 `cs`/`gs` 를 치거나 `cc`/`codex` 를 켤 때(표시 전용).
입력: 기록해둔 사용량 한 건(weekly_pct/weekly_reset/session_pct/session_reset/at/host)
출력: "주간 남은 100%, 세션 남은 100% (08/03 01:59 초기화된 뒤 읽은 값 없음)" 같은 한 줄
외부 의존: shared_state.host_label(기계 이름 바꾸기)만. 없어도 원래 이름으로 떨어진다.

핵심 규칙(2026-08-03 사용자 지적으로 바뀐 부분):
    기록에 적힌 초기화 시각이 이미 지났으면 그 창은 다시 채워졌다는 뜻이다.
    예전에는 그 값을 통째로 버려서 화면에 "사용량 못 읽음" 만 남았다(D 계정이 실제로는
    100% 쓸 수 있는데 못 읽음으로 보였다). 이제는 "남은 100%" 로 보여주고,
    실측이 아니라 초기화 시각에서 끌어낸 값이라는 것을 같이 적는다.

의도적 미구현: 이 값으로 계정을 고르거나 막지 않는다(표시 전용). 순서·차단 판단은
    pool_state 의 weekly_reset 과 실제 거절만 쓴다 — 낡은 값으로 계정을 막았다가
    2026-07-29 에 멀쩡한 4계정이 전부 막힌 사고가 있었다.
마지막 점검: 2026-08-03
"""

from __future__ import annotations

import time

KINDS = (("weekly_pct", "weekly_reset", "주간"),
         ("session_pct", "session_reset", "세션"))


def _num(value):
    """숫자로 못 바꾸면 None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def usage_parts(entry: dict, now: float):
    """[(이름, 남은 %, 초기화에서 끌어낸 값인지), ...] 와 지나간 초기화 시각 중 가장 늦은 것.

    초기화 시각이 지난 창은 남은 100% 로 본다. 그 뒤에 누가 썼으면 그 기계가
    더 새 값을 올리므로(공유 칠판) 여기까지 오지 않는다.
    """
    parts, reset_at = [], None
    for pct_key, reset_key, name in KINDS:
        pct = _num(entry.get(pct_key))
        if pct is None:
            continue
        reset = _num(entry.get(reset_key))
        if reset is not None and reset <= now:
            parts.append((name, 100.0, True))
            reset_at = reset if reset_at is None else max(reset_at, reset)
        else:
            parts.append((name, max(0.0, 100.0 - pct), False))
    return parts, reset_at


def format_parts(parts) -> str:
    """한 줄 글로. 실측과 초기화 추정이 섞여 있을 때만 어느 쪽이 추정인지 표시한다."""
    if not parts:
        return ""
    mixed = any(d for _, _, d in parts) and any(not d for _, _, d in parts)
    return ", ".join(
        f"{name} 남은 {left:.0f}%" + ("(초기화됨)" if derived and mixed else "")
        for name, left, derived in parts
    )


def usage_line(entry: dict, now: float) -> str:
    """기록해둔 사용량 한 줄. 보여줄 게 없으면 빈 글."""
    parts, _ = usage_parts(entry, now)
    return format_parts(parts)


def ago_text(sec: float) -> str:
    """지난 시간을 '방금 / 12분 전 / 3시간 전 / 2일 전' 으로."""
    if sec < 60:
        return "방금"
    if sec < 3600:
        return f"{int(sec // 60)}분 전"
    if sec < 86400:
        return f"{int(sec // 3600)}시간 전"
    return f"{int(sec // 86400)}일 전"


def host_label(host) -> str:
    """기계 이름을 사람이 알아보는 이름으로(본체/Mac/winserver). 못 바꾸면 원래 이름."""
    try:
        import shared_state

        return shared_state.host_label(host)
    except Exception:
        return str(host or "다른 기기")


def _at(entry) -> float:
    return _num((entry or {}).get("at")) or 0.0


def last_seen(state: dict, shared: dict | None, label: str, now: float) -> str:
    """지금 서버에서 못 읽을 때 화면에 적을 글.

    이 기계의 기록과 다른 기계가 올린 값 중 더 최신 것을 쓴다. 언제 어디서 읽은
    값인지 같이 적는다(낡은 값을 지금 값으로 오해하면 안 된다). 초기화 시각이 지나
    100% 로 되살린 값이면 그 사실을 대신 적는다.
    """
    cands = []
    local = (state.get("usage_seen") or {}).get(label)
    if local:
        cands.append(("이 기기", local))
    remote = (shared or {}).get(label)
    if remote:
        cands.append((host_label(remote.get("host")), remote))
    if not cands:
        return "사용량 못 읽음"

    who, best = max(cands, key=lambda pair: _at(pair[1]))
    parts, reset_at = usage_parts(best, now)
    line = format_parts(parts)
    if not line:
        return "사용량 못 읽음"

    if parts and all(d for _, _, d in parts) and reset_at:
        when = time.strftime("%m/%d %H:%M", time.localtime(reset_at))
        return f"{line} ({when} 초기화된 뒤 읽은 값 없음)"

    seen_at = _at(best)
    return f"{line} ({who} {ago_text(now - seen_at) if seen_at else '이전'} 기준)"
