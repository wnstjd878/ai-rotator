"""
== 운영 맥락 ==
용도: 지피티(Codex) 계정별 사용량을 챗지피티 서버에서 읽는다.
      codex 화면의 /usage 표시와 같은 데이터다.
호출: GET https://chatgpt.com/backend-api/codex/usage  (Bearer = auth.json 의 access_token)
      응답 중 rate_limit.primary_window / secondary_window 를 쓴다.
      어느 쪽이 주간이고 어느 쪽이 5시간인지는 limit_window_seconds 로 가른다
      (604800=주간, 18000=5시간). Pro 는 주간 창 하나만 오고 secondary 가 없다.
출력: get_usage(cred_path) → {session_pct, weekly_pct, session_reset, weekly_reset,
                              limit_reached, reached_type, plan, email}
      실패(네트워크·토큰 만료·차단)면 None. 봇을 멈추지 않는다.
      표기 함수(남은량·초기화 시각)는 usage_check 한 벌을 그대로 쓴다 — 클로드 알림과
      지피티 알림의 문구가 갈라지지 않게 하려는 것이다.
자격 취급: access_token 은 파일에서 읽어 헤더에만 싣는다. 값은 화면·기록에 안 남긴다.
          돌려주는 건 사용률 %(자격 아님)뿐.
의도적 미구현: 토큰 갱신(refresh) 안 한다. 조회가 막히면 그 계정은 다시 로그인해야 한다
      — 갱신용 토큰을 여기서 굴리면 codex 본체가 쥔 값과 어긋나 로그인이 깨질 수 있다.
마지막 점검: 2026-07-28
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import usage_check  # 표기 함수 재사용 (pct_text / fmt_reset_full / left_phrase)  # noqa: E402

USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
UA = "codex_cli_rs/0.144.6 (Windows 11; x86_64) WindowsTerminal"
TIMEOUT = 12
WEEKLY_MIN_WINDOW = 86400  # 이보다 긴 창은 주간으로 본다 (5시간=18000, 주간=604800)

last_error = None  # 마지막 조회 실패 사유(종류만, 자격 값은 절대 안 담는다)

# 표기 한 벌 (클로드 쪽과 같은 문구를 쓴다)
pct_text = usage_check.pct_text
fmt_reset_full = usage_check.fmt_reset_full
left_phrase = usage_check.left_phrase


def _read_tokens(cred_path):
    """auth.json 형식 파일에서 접근 토큰과 계정 번호를 꺼낸다. 값은 돌려만 주고 안 찍는다."""
    try:
        d = json.load(open(cred_path, encoding="utf-8"))
    except Exception:
        return None, None
    tk = d.get("tokens") or {}
    return tk.get("access_token"), tk.get("account_id")


def _window_kind(win) -> str | None:
    """창 하나가 주간인지 세션(5시간)인지. 창 길이를 모르면 None."""
    if not isinstance(win, dict):
        return None
    try:
        secs = float(win.get("limit_window_seconds"))
    except (TypeError, ValueError):
        return None
    return "weekly" if secs >= WEEKLY_MIN_WINDOW else "session"


def get_usage(cred_path) -> dict | None:
    """그 계정의 사용량을 읽는다. 실패하면 None.

    실패 사유는 last_error 에 남는다(값이 아니라 종류만). 사유 없이 None 만 돌려주면
    토큰 만료와 차단이 구분되지 않아 엉뚱한 데를 파게 된다.
    """
    global last_error
    last_error = None
    tok, acct = _read_tokens(cred_path)
    if not tok:
        last_error = "자격 파일에 접근 토큰 없음"
        return None

    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + tok,
        "User-Agent": UA,
        "Accept": "application/json",
        "chatgpt-account-id": acct or "",
        "originator": "codex_cli_rs",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            last_error = "조회 제한(429). 잠시 뒤 다시"
        elif e.code in (401, 403):
            # 토큰이 죽었거나 앞단에서 막힌 경우. 본문이 HTML 이면 막힌 쪽이다.
            body = ""
            try:
                body = e.read(200).decode("utf-8", "replace")
            except Exception:
                pass
            last_error = "앞단 차단(HTML 응답)" if "<html" in body.lower() else f"자격 거절(HTTP {e.code})"
        else:
            last_error = f"HTTP {e.code}"
        return None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        last_error = type(e).__name__
        return None

    try:
        j = json.loads(raw)
    except Exception:
        last_error = "응답이 JSON 이 아님"
        return None

    rl = j.get("rate_limit") or {}
    out = {
        "session_pct": None, "session_reset": None,
        "weekly_pct": None, "weekly_reset": None,
        "limit_reached": bool(rl.get("limit_reached")),
        "reached_type": j.get("rate_limit_reached_type"),
        "plan": j.get("plan_type"),
        "email": j.get("email"),
    }
    for key in ("primary_window", "secondary_window"):
        win = rl.get(key)
        kind = _window_kind(win)
        if not kind:
            continue
        try:
            out[f"{kind}_pct"] = float(win.get("used_percent"))
        except (TypeError, ValueError):
            pass
        try:
            out[f"{kind}_reset"] = float(win.get("reset_at"))
        except (TypeError, ValueError):
            pass
    return out


def get_usage_for(cred_path, *, is_current: bool = False) -> dict | None:
    """get_usage 와 같다.

    계정마다 폴더가 따로라(codex_home) 그 폴더의 auth.json 이 늘 최신이다. 예전처럼
    "사본이 낡았으면 지금 쓰는 자격으로 한 번 더" 할 필요가 없어졌다.
    부르는 쪽 모양을 안 바꾸려고 이름만 남겨 둔다.
    """
    return get_usage(cred_path)


def which_limit(usage: dict | None, threshold: float = 95.0) -> str | None:
    """도달(임박)한 한도 종류. 'weekly' / 'session' / None.

    서버가 한도 도달을 직접 알려주면(reached_type) 그 값을 먼저 믿는다.
    """
    if not usage:
        return None
    rt = (usage.get("reached_type") or "").lower()
    if rt:
        if "week" in rt or "secondary" in rt:
            return "weekly"
        if "hour" in rt or "primary" in rt or "session" in rt:
            # primary 가 주간인 요금제(Pro)도 있어 창 길이로 한 번 더 확인한다
            return "weekly" if usage.get("weekly_pct") is not None and usage.get("session_pct") is None else "session"
    wk, se = usage.get("weekly_pct"), usage.get("session_pct")
    if wk is not None and wk >= threshold:
        return "weekly"
    if se is not None and se >= threshold:
        return "session"
    return None


def weekly_left(cred_path):
    """그 계정의 주간 남은량(%). 못 읽으면 None."""
    u = get_usage(cred_path)
    if not u or u.get("weekly_pct") is None:
        return None
    return max(0.0, 100 - float(u["weekly_pct"]))


def summarize(usage: dict | None) -> str:
    """사람이 읽는 한 줄. 남은량 앞, 초기화 시각 뒤."""
    if not usage:
        return "사용량 못 읽음"
    parts = []
    for key, name in (("weekly", "주간"), ("session", "세션")):
        pct = usage.get(f"{key}_pct")
        if pct is None:
            continue
        rem = max(0.0, 100 - float(pct))
        when = fmt_reset_full(usage.get(f"{key}_reset"))
        parts.append(f"{name} 남은 {pct_text(rem)}" + (f" ({when})" if when else ""))
    if usage.get("limit_reached"):
        parts.append("현재 한도 도달 상태")
    return ", ".join(parts) if parts else "사용량 없음"


if __name__ == "__main__":
    import codex_home

    for label in codex_home.labels():
        if not codex_home.is_logged_in(label):
            print(label, "-> 로그인 없음")
            continue
        u = get_usage(codex_home.auth_path(label))
        who = f" [{u.get('email')} / {u.get('plan')}]" if u else ""
        print(label, "->", summarize(u) + who, "" if u else f"({last_error})")
