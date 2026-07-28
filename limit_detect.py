"""
== 운영 맥락 ==
용도: claude CLI 호출 결과가 "구독 한도에 걸려 거절된 것"인지 판정하고,
      한도가 언제 풀리는지(해제 시각)를 문구에서 뽑아낸다.
      claude_pool.py 가 계정을 바꿀지 말지 결정하는 근거로 쓴다.
입력: 종료 코드 + 표준출력/오류출력 문자열
출력: classify() → "ok" | "limit" | "auth" | "error"
      reset_epoch() → 한도 해제 시각(epoch 초). 못 읽으면 기본값(5시간 뒤).
오판을 막는 두 겹:
      (1) 실패인지 판정할 때 claude 자신의 결과 껍데기(type=="result")만 인정한다.
          모델이 쓴 답변 안에 {"is_error": true} 같은 JSON 이 들어 있어도 실패로 보지 않는다.
          봇이 "이 로그를 JSON 으로 판정해줘" 같은 일을 시키면 실제로 그런 답변이 나온다.
      (2) 한도 문구는 claude 가 낸 오류 자리(결과 껍데기의 result/error, 오류출력)에서만 찾는다.
          모델 답변 본문이나 웹에서 가져온 글에 'rate limit' 이 있어도 넘어가지 않는다.
      이 두 겹이 없으면 정상 응답 한 번으로 계정 네 개가 통째로 막힌다.
의도적 미구현: 남은 사용량을 미리 재서 선제 전환하지 않는다. 정확한 잔여량을 알 방법이
      없어 오차가 크고, 거절이 오면 그때 넘기는 쪽이 단순하고 확실하다.
마지막 점검: 2026-07-22
"""

from __future__ import annotations

import json
import re
import time

DEFAULT_COOLDOWN = 5 * 3600  # 해제 시각을 못 읽었을 때 쉬게 할 시간(초)
MAX_COOLDOWN = 8 * 24 * 3600  # 문구에서 읽은 값이 이보다 멀면 잘못 읽은 것으로 보고 버림

# 한도 거절로 보는 문구. claude 가 낸 오류 자리에서만 찾는다.
# 앞의 다섯 줄은 claude 실행 파일 안에 실제로 박혀 있는 표기를 확인해 넣은 것이다
# (usage limit reached / session limit / weekly limit / Opus limit / Sonnet limit,
#  five_hour=session limit, seven_day=weekly limit, billing_error, rate_limit).
LIMIT_PATTERNS = [
    r"usage limit reached",
    r"\b(session|weekly|opus|sonnet) limit\b",
    r"\b(five_hour|seven_day(_opus|_sonnet)?)\b",
    r"billing_error",
    r"credit balance (is )?too low",
    r"usage limit has been reached",
    r"claude (ai )?usage limit",
    r"\b5-?hour limit\b",
    r"limit will reset",
    r"rate[_ ]limit",
    r"rate limited",
    r"too many requests",
    r"quota (exceeded|exhausted)",
    r"out of (usage|credits)",
    r"upgrade to continue",
    r"사용량 한도",
    r"한도에 도달",
]

# 자격 문제로 보는 문구 (계정을 바꾼다고 풀리지 않음. 다시 로그인해야 함)
AUTH_PATTERNS = [
    r"not logged in",
    r"please (run )?/?login",
    r"authentication (failed|error)",
    r"invalid (api key|token|credentials)",
    r"oauth token (expired|revoked)",
    r"unauthorized",
]

# 저장본이 죽은 경우(그 계정만 못 씀). 다른 계정으로 갈아타면 풀린다.
# 위의 AUTH 와 갈라야 한다. AUTH 는 자격이 아예 없는 상태라 갈아타도 소용이 없다.
DEAD_PATTERNS = [
    r"oauth session (has )?expired",
    r"could not be refreshed",
    r"failed to authenticate",
    r"invalid_grant",
    r"refresh token (is )?(invalid|expired|revoked)",
]

# 계정을 바꿔도 안 풀리는 "한도" 들. 판정 전에 본문에서 지운다.
# claude 실행 파일 안에 실제로 들어 있는 표기들이며, 그냥 두면 프롬프트가 길거나
# 예산 상한에 걸렸을 때 멀쩡한 계정을 줄줄이 막아버린다.
IGNORE_PATTERNS = [
    r"context (window )?limit( reached| exceeded)?",
    r"subagent (nesting )?limit reached",
    r"nesting limit reached",
    r"recursion limit reached",
    r"size limit reached",
    r"concurrent export limit reached",
    r"spend limit (reached|unavailable|exceeded)",
    r"max[_ -]?budget",
    r"budget (exceeded|limit)",
    r"turn limit",
    r"max[_ -]?turns",
    r"(input|output|token)s? limit",
    r"prompt is too long",
    r"exceeds size limits",
]

_LIMIT_RE = [re.compile(p, re.I) for p in LIMIT_PATTERNS]
_AUTH_RE = [re.compile(p, re.I) for p in AUTH_PATTERNS]
_DEAD_RE = [re.compile(p, re.I) for p in DEAD_PATTERNS]
_IGNORE_RE = re.compile("|".join(IGNORE_PATTERNS), re.I)


def _result_envelope(stdout: str) -> dict | None:
    """claude 가 낸 결과 껍데기({"type":"result", ...})를 찾아 돌려준다.

    --output-format json 은 통째로 한 덩어리, stream-json 은 줄마다 한 덩어리라
    본문 전체와 마지막 몇 줄을 후보로 본다. 모델 답변 안의 JSON 은 type 이 result 가
    아니므로 여기서 걸러진다.
    """
    text = (stdout or "").strip()
    if not text:
        return None
    lines = text.splitlines()
    candidates = [text]
    candidates.extend(reversed(lines[-5:]))
    for chunk in candidates:
        chunk = chunk.strip()
        if not chunk.startswith("{"):
            continue
        try:
            data = json.loads(chunk)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("type") == "result":
            return data
    return None


def _envelope_error_text(env: dict) -> str:
    """결과 껍데기에서 오류 사유가 담기는 자리만 모은다. 모델 답변 본문은 안 쓴다."""
    parts = []
    for key in ("result", "error", "error_message", "subtype", "api_error_status", "stop_reason"):
        val = env.get(key)
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, dict):
            parts.append(json.dumps(val, ensure_ascii=False))
    return "\n".join(parts)


def is_failure(returncode: int, stdout: str = "") -> bool:
    """호출이 실패로 끝났는지. 종료 코드가 0 이면 결과 껍데기의 오류 표시만 본다."""
    if returncode != 0:
        return True
    env = _result_envelope(stdout)
    if env is None:
        return False
    return bool(env.get("is_error") or env.get("subtype") == "error")


def failure_text(stdout: str = "", stderr: str = "") -> str:
    """한도/자격 문구를 찾을 자리. 오류출력 + 결과 껍데기의 오류 자리.

    결과 껍데기가 있으면 그 안의 오류 자리만 본다. 모델 답변 본문, 붙임 도구가 가져온 글,
    되울려 나온 프롬프트는 여기 안 들어간다.
    껍데기가 없으면(텍스트 모드나 CLI 가 뜨기도 전에 죽은 경우) 표준출력을 그대로 본다.
    이 함수는 이미 실패로 판정된 호출에만 쓰므로, 그때의 표준출력은 오류 문구로 본다.
    """
    parts = [stderr or ""]
    env = _result_envelope(stdout)
    if env is not None:
        parts.append(_envelope_error_text(env))
    else:
        parts.append(stdout or "")
    return "\n".join(parts)


def strip_ignored(text: str) -> str:
    """계정을 바꿔도 안 풀리는 한도 문구를 지운 본문. 오판을 막는다."""
    return _IGNORE_RE.sub(" ", text or "")


def classify(returncode: int, stdout: str = "", stderr: str = "") -> str:
    """호출 결과를 ok / limit / dead / auth / error 중 하나로 판정.

    dead = 그 계정 저장본이 죽음(갈아타면 풀린다)
    auth = 로그인 자체가 안 된 상태(갈아타도 소용없다)
    """
    if not is_failure(returncode, stdout):
        return "ok"

    text = failure_text(stdout, stderr)
    # 죽은 저장본을 먼저 본다. auth 보다 좁고 뜻이 분명하다.
    for rx in _DEAD_RE:
        if rx.search(text):
            return "dead"
    for rx in _AUTH_RE:
        if rx.search(text):
            return "auth"
    cleaned = strip_ignored(text)
    for rx in _LIMIT_RE:
        if rx.search(cleaned):
            return "limit"
    return "error"


def _from_epoch_token(text: str, now: float) -> float | None:
    """'...reached|1753200000' 처럼 문구에 박힌 시각 숫자를 읽는다."""
    for m in re.finditer(r"\b(\d{10,13})\b", text):
        raw = int(m.group(1))
        val = raw / 1000 if raw > 10_000_000_000 else raw
        if now < val <= now + MAX_COOLDOWN:
            return float(val)
    return None


_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _from_date_clock(text: str, now: float) -> float | None:
    """'resets Jul 31 at 8am' / 'resets Jul 31, 8am' 처럼 월 이름+일+시각을 읽는다.

    claude 주간 한도 응답이 이 형식이다. 못 읽으면 기본 5시간으로 떨어져 시각이 틀린다.
    타임존은 시스템 로컬(한국 머신이면 KST)로 계산한다.
    """
    m = re.search(
        r"resets?\s+([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:,)?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)",
        text, re.I)
    if not m:
        return None
    mon = _MONTHS.get(m.group(1)[:3].lower())
    if not mon:
        return None
    day = int(m.group(2))
    hour = int(m.group(3))
    minute = int(m.group(4) or 0)
    ap = m.group(5).lower()
    if ap == "pm" and hour < 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0
    if not (1 <= day <= 31 and 0 <= hour <= 23):
        return None
    lt = time.localtime(now)
    for yr in (lt.tm_year, lt.tm_year + 1):  # 연말이면 내년으로
        try:
            t = time.mktime((yr, mon, day, hour, minute, 0, 0, 0, -1))
        except (OverflowError, ValueError):
            continue
        if t > now - 3600:
            return float(t)
    return None


def _from_clock(text: str, now: float) -> float | None:
    """'resets 3pm' / 'resets at 11:30 PM' 처럼 적힌 시계 시각을 다음 도래 시각으로.

    'resets at 2026-07-23 09:00' 처럼 날짜가 붙은 형태는 연도를 시각으로 잘못 읽으므로
    여기서 다루지 않는다(숫자형 시각 쪽이나 기본값으로 넘긴다).
    """
    m = re.search(
        r"resets?\s*(?:at\s*)?(?!\d{4}\b)(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?", text, re.I
    )
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower().replace(".", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))
    if target <= now:
        target += 24 * 3600  # 이미 지난 시각이면 내일 그 시각
    return float(target)


def reset_epoch(stdout: str = "", stderr: str = "", now: float | None = None) -> float:
    """한도가 풀리는 시각(epoch 초). 문구에서 못 읽으면 기본 5시간 뒤."""
    now = time.time() if now is None else now
    text = failure_text(stdout, stderr) or f"{stdout or ''}\n{stderr or ''}"
    for parse in (_from_epoch_token, _from_date_clock, _from_clock):
        val = parse(text, now)
        if val:
            return val
    return now + DEFAULT_COOLDOWN


def short_reason(stdout: str = "", stderr: str = "", limit: int = 160) -> str:
    """기록용 사유 한 줄. 자격정보가 섞이지 않도록 길이를 자른다."""
    text = failure_text(stdout, stderr).strip().replace("\n", " ")
    for rx in _LIMIT_RE + _AUTH_RE:
        m = rx.search(text)
        if m:
            start = max(0, m.start() - 40)
            return text[start: start + limit].strip()
    return text[:limit].strip()
