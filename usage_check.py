"""
== 운영 맥락 ==
용도: 계정별 사용량(세션 5시간 한도 %, 주간 한도 %)을 claude 서버에서 읽는다.
      claude Code 설정 화면의 "현재 세션 / 주간 한도" 와 같은 데이터다.
호출: /api/oauth/usage (Bearer accessToken). creds/<라벨>.json 의 accessToken 사용.
      응답: five_hour.utilization(세션 %), seven_day.utilization(주간 %), 각 resets_at.
출력: get_usage(cred_path) → {session_pct, weekly_pct, session_reset(epoch), weekly_reset(epoch)}
      실패(네트워크·토큰 만료 등)면 None. 봇을 멈추지 않는다.
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
