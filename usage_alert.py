"""
== 운영 맥락 ==
용도: 지금 쓰는 계정의 사용률을 주기적으로 보고, 세션(5시간)/주간 한도가
      50% / 70% / 90% 를 넘을 때마다 텔레그램으로 남은량을 알린다. (선제 경고)
실행: python usage_alert.py   (작업 스케줄러/launchd 로 10분마다)
동작: 현재 계정(state.current, 없으면 순차상 다음)의 usage 를 /api/oauth/usage 로 읽어
      주간·세션 각각 넘은 단계(50/70/90)를 그 리셋 구간에 한 번씩만 알린다(스팸 방지).
      리셋되면(리셋 시각이 바뀌면) 다시 무장돼 다음 구간에 또 알린다.
      한 번에 여러 단계를 넘었으면(예: 첫 확인에 92%) 가장 높은 단계 하나만 알린다.
상태: alert_state.json 에 "이미 알린 (계정, 종류, 리셋구간, 단계)" 를 기록.
자격: 사용률만 읽고, 텔레그램 자격은 notify 가 파일에서 읽음.
마지막 점검: 2026-07-27
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import pool_state as st  # noqa: E402
import usage_check  # noqa: E402
import notify  # noqa: E402

ALERT_STATE = BASE / "alert_state.json"
THRESHOLDS = [50, 70, 90]  # 넘을 때마다 알리는 단계
HISTORY_KEEP = 80
_WEEKDAY = "월화수목금토일"


def _fmt_reset_full(epoch) -> str:
    """' 8/1 (토) 20:59에 초기화 됨' 형식. 없으면 빈 문자열."""
    if not epoch:
        return ""
    import time
    lt = time.localtime(float(epoch))
    return f" {lt.tm_mon}/{lt.tm_mday} ({_WEEKDAY[lt.tm_wday]}) {time.strftime('%H:%M', lt)}에 초기화 됨"


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


def _check(kind: str, pct, reset, label: str, alerted: dict) -> str | None:
    """넘은 단계 중 아직 안 알린 가장 높은 단계를 메시지로. 넘은 단계는 모두 마킹."""
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
    name = "주간" if kind == "wk" else "세션(5시간)"
    return f"{name} 남은 사용량 {max(0, 100 - pct):.0f}% ({pct:.0f}% 사용됨){_fmt_reset_full(reset)}"


def main() -> int:
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

    alerted = _load_alerted()
    msgs = []
    m = _check("wk", u.get("weekly_pct"), u.get("weekly_reset"), label, alerted)
    if m:
        msgs.append(m)
    m = _check("se", u.get("session_pct"), u.get("session_reset"), label, alerted)
    if m:
        msgs.append(m)

    if msgs:
        body = f"[클로드 사용량 경고] 계정 {label} - " + ", ".join(msgs)
        # 나머지 계정들의 주간 남은량도 같이 보여준다
        others = []
        for l in st.account_labels():
            if l == label:
                continue
            oc = BASE / "creds" / f"{l}.json"
            if not oc.exists():
                continue
            ou = usage_check.get_usage(oc)
            if not ou or ou.get("weekly_pct") is None:
                continue
            rem = max(0, 100 - ou["weekly_pct"])
            tag = " (소진)" if rem <= 0 else ""
            others.append(f"계정 {l} {rem:.0f}%{tag}")
        if others:
            body += "\n남은 계정 주간 사용량 :\n" + "\n".join(others)
        notify.send(body)
        _save_alerted(alerted)
        print(f"알림 보냄: {label} " + ", ".join(msgs))
    else:
        _save_alerted(alerted)  # 마킹 갱신은 저장
        print(f"새 단계 없음: {label} (주간 {u.get('weekly_pct')}%, 세션 {u.get('session_pct')}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
