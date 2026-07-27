"""
== 운영 맥락 ==
용도: 계정별 사용량(세션 5시간 한도 %, 주간 한도 %)을 claude 서버에서 읽는다.
      claude Code 설정 화면의 "현재 세션 / 주간 한도" 와 같은 데이터다.
호출: /api/oauth/usage (Bearer accessToken). creds/<라벨>.json 의 accessToken 사용.
      응답: five_hour.utilization(세션 %), seven_day.utilization(주간 %), 각 resets_at.
출력: get_usage(cred_path) → {session_pct, weekly_pct, session_reset(epoch), weekly_reset(epoch)}
      실패(네트워크·토큰 만료 등)면 None. 봇을 멈추지 않는다.
      알림 문구용 공용 표기도 여기 둔다: fmt_reset_full(초기화 시각+남은 기간),
      pct_text(남은량), left_phrase(남은 기간), weekly_left(주간 남은량).
      사용량 경고와 계정 전환 알림이 같은 표기를 쓰게 하려는 것이다.
자격 취급: accessToken 은 파일에서 읽어 헤더에만 싣는다. 값은 화면·기록에 안 남긴다.
          돌려주는 건 사용률 %(자격 아님)뿐.
마지막 점검: 2026-07-27
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
UA = "claude-cli/2.1.0 (external, cli)"
TIMEOUT = 10
_WEEKDAY = "월화수목금토일"


def _iso_to_epoch(text):
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def get_usage(cred_path) -> dict | None:
    """creds/<라벨>.json 의 계정 사용량을 읽는다. 실패하면 None."""
    try:
        d = json.load(open(cred_path, encoding="utf-8"))
    except Exception:
        return None
    tok = (d.get("claudeAiOauth") or {}).get("accessToken")
    if not tok:
        return None
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + tok,
        "User-Agent": UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            j = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
        return None
    fh = j.get("five_hour") or {}
    sd = j.get("seven_day") or {}
    return {
        "session_pct": fh.get("utilization"),
        "session_reset": _iso_to_epoch(fh.get("resets_at")),
        "weekly_pct": sd.get("utilization"),
        "weekly_reset": _iso_to_epoch(sd.get("resets_at")),
    }


def which_limit(usage: dict | None, threshold: float = 95.0) -> str | None:
    """도달(임박)한 한도 종류. 'session'(5시간) / 'weekly'(주간) / None."""
    if not usage:
        return None
    wk = usage.get("weekly_pct")
    se = usage.get("session_pct")
    if wk is not None and wk >= threshold:
        return "weekly"
    if se is not None and se >= threshold:
        return "session"
    return None


def left_phrase(sec: float) -> str:
    """남은 시간을 '5일 뒤' / '3시간 뒤' / '20분 뒤' 로. 지났으면 '곧'."""
    if sec <= 0:
        return "곧"
    if sec < 3600:
        return f"{int(sec // 60)}분 뒤"
    if sec < 86400:
        return f"{int(sec // 3600)}시간 뒤"
    return f"{int(sec // 86400)}일 뒤"


def fmt_reset_full(epoch) -> str:
    """'8/1 토 21:00 초기화, 5일 뒤' 형식. 없으면 빈 문자열.

    알림 문구를 만드는 곳이 여럿이라 여기 한 벌만 둔다(문구가 갈라지면 한쪽만 고치게 된다).
    """
    if not epoch:
        return ""
    import time as _t
    lt = _t.localtime(float(epoch))
    when = f"{lt.tm_mon}/{lt.tm_mday} {_WEEKDAY[lt.tm_wday]} {_t.strftime('%H:%M', lt)}"
    return f"{when} 초기화, {left_phrase(float(epoch) - _t.time())}"


def pct_text(rem: float) -> str:
    """남은량 표기. 0.4% 를 '0%' 로 반올림하면 아직 쓸 수 있는 계정이 소진으로 보인다."""
    if rem <= 0:
        return "0%"
    if rem < 1:
        return "1% 미만"
    return f"{rem:.0f}%"


def weekly_left(cred_path):
    """그 계정의 주간 남은량(%). 못 읽으면 None."""
    u = get_usage(cred_path)
    if not u or u.get("weekly_pct") is None:
        return None
    return max(0.0, 100 - float(u["weekly_pct"]))


def _fmt_reset(epoch) -> str:
    """리셋 시각을 ' (MM/DD HH:MM 풀림)' 로. 없으면 빈 문자열."""
    if not epoch:
        return ""
    import time
    return f" ({time.strftime('%m/%d %H:%M', time.localtime(float(epoch)))} 풀림)"


def summarize(usage: dict | None) -> str:
    """사람이 읽는 한 줄. 남은량 앞, 리셋 시각 뒤.
    예: '주간 남은 1% (08/01 20:59 풀림), 세션 남은 95% (07/27 20:59 풀림)'"""
    if not usage:
        return "사용량 못 읽음"
    wk = usage.get("weekly_pct")
    se = usage.get("session_pct")
    parts = []
    if wk is not None:
        parts.append(f"주간 남은 {max(0, 100 - wk):.0f}%{_fmt_reset(usage.get('weekly_reset'))}")
    if se is not None:
        parts.append(f"세션 남은 {max(0, 100 - se):.0f}%{_fmt_reset(usage.get('session_reset'))}")
    return ", ".join(parts) if parts else "사용량 없음"


if __name__ == "__main__":
    import sys
    from pathlib import Path

    creds = Path(__file__).resolve().parent / "creds"
    for f in sorted(creds.glob("*.json")):
        print(f.stem, "->", summarize(get_usage(f)))
