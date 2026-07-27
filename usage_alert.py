"""
== 운영 맥락 ==
용도: 지금 쓰는 계정의 사용률을 주기적으로 보고, 세션(5시간)/주간 한도가
      50% / 70% / 90% 를 넘을 때마다 텔레그램으로 남은량을 알린다. (선제 경고)
실행: python usage_alert.py   (winserver 작업 스케줄러 ai-rotator-usage-alert, 10분마다)
      python usage_alert.py --preview  → 지금 값으로 알림 문구만 출력(전송·기록 안 함)
동작: 현재 계정(state.current, 없으면 순차상 다음)의 usage 를 /api/oauth/usage 로 읽어
      주간·세션 각각 넘은 단계(50/70/90)를 그 리셋 구간에 한 번씩만 알린다(스팸 방지).
      리셋되면(리셋 시각이 바뀌면) 다시 무장돼 다음 구간에 또 알린다.
      한 번에 여러 단계를 넘었으면(예: 첫 확인에 92%) 가장 높은 단계 하나만 알린다.
문구: 두괄식. 첫 줄이 "어느 계정이 얼마 남았고 다음에 어디로 넘어가는가".
      전환은 순환기가 자동으로 하므로 사람에게 행동을 시키지 않는다(알림만).
      계정 목록은 라벨 순서 = 실제 전환 순서(A→B→C→D)로 두고, 다음 대상만 표시한다.
      잔여 0%(소진) 계정은 "사용 불가"로 갈라 아래에 둔다.
상태: alert_state.json 에 "이미 알린 (계정, 종류, 리셋구간, 단계)" 를 기록.
자격: 사용률만 읽고, 텔레그램 자격은 notify 가 파일에서 읽음.
의도적 미구현: 굵게·고정폭 표시 없음. notify.send 가 평문(parse_mode 없음)으로 보내므로
      마크다운을 넣으면 기호가 그대로 보인다. 붙이려면 notify.send 부터 고쳐야 한다.
마지막 점검: 2026-07-27
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import pool_state as st  # noqa: E402
import usage_check  # noqa: E402
import notify  # noqa: E402

ALERT_STATE = BASE / "alert_state.json"
THRESHOLDS = [50, 70, 90]  # 넘을 때마다 알리는 단계
SWITCH_SOON = 10           # 남은량이 이 % 이하면 첫 줄에 다음 계정을 붙인다
HISTORY_KEEP = 80
_WEEKDAY = "월화수목금토일"
_KIND_NAME = {"wk": "주간", "se": "세션"}


def _left_phrase(sec: float) -> str:
    """남은 시간을 '5일 뒤' / '3시간 뒤' / '20분 뒤' 로. 지났으면 '곧'."""
    if sec <= 0:
        return "곧"
    if sec < 3600:
        return f"{int(sec // 60)}분 뒤"
    if sec < 86400:
        return f"{int(sec // 3600)}시간 뒤"
    return f"{int(sec // 86400)}일 뒤"


def _fmt_reset_full(epoch) -> str:
    """'8/1 토 21:00 초기화, 5일 뒤' 형식. 없으면 빈 문자열."""
    if not epoch:
        return ""
    lt = time.localtime(float(epoch))
    when = f"{lt.tm_mon}/{lt.tm_mday} {_WEEKDAY[lt.tm_wday]} {time.strftime('%H:%M', lt)}"
    return f"{when} 초기화, {_left_phrase(float(epoch) - time.time())}"


def _load_alerted() -> dict:
    if not ALERT_STATE.exists():
        return {}
    try:
        return json.loads(ALERT_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_alerted(d: dict) -> None:
    if len(d) > HISTORY_KEEP:
        d = dict(list(d.items())[-HISTORY_KEEP:])
    try:
        ALERT_STATE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _check(kind: str, pct, reset, label: str, alerted: dict):
    """넘은 단계 중 아직 안 알린 게 있으면 (종류, 남은%, 리셋). 넘은 단계는 모두 마킹."""
    if pct is None:
        return None
    rr = round(float(reset or 0))
    crossed = [t for t in sorted(THRESHOLDS, reverse=True) if pct > t]
    if not crossed:
        return None
    fresh = [t for t in crossed if alerted.get(f"{kind}_{label}_{rr}_{t}") != 1]
    for t in crossed:  # 넘은 단계 전부 마킹(다음 실행에서 중복 안 함)
        alerted[f"{kind}_{label}_{rr}_{t}"] = 1
    if not fresh:
        return None
    return (kind, max(0.0, 100 - float(pct)), reset)


def _others_weekly(label: str) -> list:
    """다른 계정들의 주간 남은량. 라벨 순서(=전환 순서) 그대로 [(라벨, 남은%)]."""
    out = []
    for l in st.account_labels():
        if l == label:
            continue
        cred = BASE / "creds" / f"{l}.json"
        if not cred.exists():
            continue
        u = usage_check.get_usage(cred)
        if not u or u.get("weekly_pct") is None:
            continue
        out.append((l, max(0.0, 100 - float(u["weekly_pct"]))))
    return out


def _next_label(label: str, others: list):
    """이 계정이 막히면 순환기가 다음에 고를 계정. 주간이 남은 것 중 순서상 다음."""
    alive = [l for l, rem in others if rem > 0]
    if not alive:
        return None
    labels = st.account_labels()
    if label not in labels:
        return alive[0]
    i = labels.index(label)
    order = labels[i + 1:] + labels[:i]  # 순차 소진: 다음 인덱스부터 한 바퀴
    for l in order:
        if l in alive:
            return l
    return alive[0]


def build_message(label: str, hits: list, others: list) -> str:
    """알림 본문. 첫 줄에 결론, 그 아래 상세, 그 아래 계정 목록."""
    nxt = _next_label(label, others)
    worst = min(hits, key=lambda h: h[1])
    head = f"[클로드] 계정 {label} {_KIND_NAME[worst[0]]} {worst[1]:.0f}% 남음"
    if worst[1] <= SWITCH_SOON:
        head += f", 곧 계정 {nxt}로 자동 전환" if nxt else ", 자동 전환할 계정 없음"

    lines = [head, ""]
    for kind, rem, reset in hits:
        when = _fmt_reset_full(reset)
        lines.append(f"{_KIND_NAME[kind]} {rem:.0f}% 남음" + (f" ({when})" if when else ""))

    ok = [(l, r) for l, r in others if r > 0]
    dead = [(l, r) for l, r in others if r <= 0]
    if ok:
        lines += ["", "전환 가능"]
        lines += [f"계정 {l} {r:.0f}%" + (" (다음)" if l == nxt else "") for l, r in ok]
    if dead:
        lines += ["", "사용 불가"]
        lines += [f"계정 {l} {r:.0f}%" for l, r in dead]
    return "\n".join(lines)


def main() -> int:
    preview = "--preview" in sys.argv
    state = st.prune_blocked(st.load_state())
    label = state.get("current") or st.pick_next(state)
    if not label:
        return 0
    cred = BASE / "creds" / f"{label}.json"
    if not cred.exists():
        return 0
    u = usage_check.get_usage(cred)
    if not u:
        return 0

    if preview:  # 단계를 안 넘었어도 지금 값으로 문구를 만들어 보여준다
        hits = [(k, max(0.0, 100 - float(u[p])), u[r])
                for k, p, r in (("wk", "weekly_pct", "weekly_reset"),
                                ("se", "session_pct", "session_reset"))
                if u.get(p) is not None]
        if not hits:
            print("사용량 못 읽음")
            return 0
        print(build_message(label, hits, _others_weekly(label)))
        return 0

    alerted = _load_alerted()
    hits = []
    for kind, pct_key, reset_key in (("wk", "weekly_pct", "weekly_reset"),
                                     ("se", "session_pct", "session_reset")):
        hit = _check(kind, u.get(pct_key), u.get(reset_key), label, alerted)
        if hit:
            hits.append(hit)

    if hits:
        notify.send(build_message(label, hits, _others_weekly(label)))
        _save_alerted(alerted)
        print(f"알림 보냄: {label} " + ", ".join(f"{_KIND_NAME[k]} {r:.0f}% 남음" for k, r, _ in hits))
    else:
        _save_alerted(alerted)  # 마킹 갱신은 저장
        print(f"새 단계 없음: {label} (주간 {u.get('weekly_pct')}%, 세션 {u.get('session_pct')}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
