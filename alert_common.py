"""
== 운영 맥락 ==
용도: 사용률 단계 알림 두 벌(usage_alert.py 클로드, codex_alert.py 지피티)이 같이 쓰는 부분.
      두 파일에 똑같이 들어 있던 것을 2026-11-21 한 벌로 옮겼다.
      next_label    이 계정이 막히면 순환기가 다음에 고를 계정(순서 규칙은 pool_state 한 곳)
      message_body  알림 본문에서 첫 줄 아래(종류별 남은량, 전환 가능·사용 불가 계정 목록)
      save_alerted  "이미 알린 단계" 기록 저장(최근 것만 남김)
실행 시점: 위 두 스크립트가 부를 때만(10분마다 도는 알림 작업). 이 파일을 직접 돌리지 않는다.
입력·출력: 넘겨받은 값만 쓴다. 자격·토큰 파일을 읽지 않고, 사용량 조회·알림 전송·계정 전환을 하지 않는다.
      pool_state 는 부르는 쪽이 넘긴다(지피티 쪽은 st.use("codex") 로 바꾼 뒤의 것이어야 하므로 여기서 불러오지 않는다).
의도적 미구현: 첫 줄(머신 이름·Fable 칸 안내 등)과 단계 판정(_check)은 두 벌의 규칙이 달라 각 파일에 둔다.
마지막 점검: 2026-11-21
"""

from __future__ import annotations

import json


def next_label(st, others: list, state: dict | None = None):
    alive = [l for l, rem in others if rem > 0]
    if not alive:
        return None
    state = st.prune_blocked(st.load_state()) if state is None else state
    order = st.priority_order(state, alive)
    return order[0] if order else None


def message_body(head: str, hits: list, others: list, nxt, kind_name: dict, pct, fmt_reset) -> str:
    lines = [head, ""]
    for kind, rem, reset in hits:
        when = fmt_reset(reset)
        lines.append(f"{kind_name[kind]} {pct(rem)} 남음" + (f" ({when})" if when else ""))

    ok = [(l, r) for l, r in others if r > 0]
    dead = [(l, r) for l, r in others if r <= 0]
    if ok:
        lines += ["", "전환 가능"]
        lines += [f"계정 {l} {pct(r)}" + (" (다음)" if l == nxt else "") for l, r in ok]
    if dead:
        lines += ["", "사용 불가"]
        lines += [f"계정 {l} {pct(r)}" for l, r in dead]
    return "\n".join(lines)


def save_alerted(path, d: dict, keep: int) -> None:
    if len(d) > keep:
        d = dict(list(d.items())[-keep:])
    try:
        path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
