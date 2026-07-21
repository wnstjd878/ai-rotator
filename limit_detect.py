"""
== 운영 맥락 ==
용도: claude CLI 호출 결과가 "구독 한도에 걸려 거절된 것"인지 판정하고,
      한도가 언제 풀리는지(해제 시각)를 문구에서 뽑아낸다.
      claude_pool.py 가 계정을 바꿀지 말지 결정하는 근거로 쓴다.
입력: 종료 코드 + 표준출력/오류출력 문자열 (형식은 텍스트든 json 이든 상관 없음)
출력: classify() → "ok" | "limit" | "auth" | "error"
      reset_epoch() → 한도 해제 시각(epoch 초). 못 읽으면 기본값(5시간 뒤).
오탐 방지: 성공한 호출은 절대 한도로 보지 않는다. 종료 코드가 0 이고 오류 표시도
      없으면 본문에 어떤 단어가 있든 "ok". 사용자 프롬프트에 '한도' 같은 말이
      들어가 답변에 되풀이될 수 있기 때문.
의도적 미구현: 남은 사용량을 미리 재서 선제 전환하지 않는다. 정확한 잔여량을 알
      방법이 없어 오차가 크고, 거절이 오면 그때 넘기는 쪽이 단순하고 확실하다.
마지막 점검: 2026-07-22
"""

from __future__ import annotations

import json
import re
import time

DEFAULT_COOLDOWN = 5 * 3600  # 해제 시각을 못 읽었을 때 쉬게 할 시간(초)
MAX_COOLDOWN = 8 * 24 * 3600  # 문구에서 읽은 값이 이보다 멀면 잘못 읽은 것으로 보고 버림

# 한도 거절로 보는 문구. 실패한 호출에서만 찾는다.
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

# 자격 문제로 보는 문구 (계정을 바꾼다고 풀리지 않음 — 다시 로그인해야 함)
AUTH_PATTERNS = [
    r"not logged in",
    r"please (run )?/?login",
    r"authentication (failed|error)",
    r"invalid (api key|token|credentials)",
    r"oauth token (expired|revoked)",
    r"unauthorized",
]

# 계정을 바꿔도 안 풀리는 "한도" 들. 이 문구는 판정 전에 본문에서 지운다.
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
_IGNORE_RE = re.compile("|".join(IGNORE_PATTERNS), re.I)


def _looks_error(stdout: str) -> bool:
    """--output-format json 응답이 오류를 표시하는지. 텍스트 출력이면 False."""
    text = (stdout or "").strip()
    if not text:
        return False
    for chunk in (text, text.splitlines()[-1] if text.splitlines() else ""):
        try:
            data = json.loads(chunk)
        except Exception:
            continue
        if isinstance(data, dict) and (data.get("is_error") or data.get("subtype") == "error"):
            return True
    return False


def haystack(stdout: str, stderr: str) -> str:
    return f"{stdout or ''}\n{stderr or ''}"


def strip_ignored(text: str) -> str:
    """계정을 바꿔도 안 풀리는 한도 문구를 지운 본문. 오판을 막는다."""
    return _IGNORE_RE.sub(" ", text or "")


def classify(returncode: int, stdout: str = "", stderr: str = "") -> str:
    """호출 결과를 ok / limit / auth / error 중 하나로 판정."""
    failed = returncode != 0 or _looks_error(stdout)
    if not failed:
        return "ok"

    text = haystack(stdout, stderr)
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


def _from_clock(text: str, now: float) -> float | None:
    """'resets 3pm' / 'resets at 11:30 PM' 처럼 적힌 시계 시각을 다음 도래 시각으로."""
    m = re.search(r"resets?\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?", text, re.I)
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
    text = haystack(stdout, stderr)
    for parse in (_from_epoch_token, _from_clock):
        val = parse(text, now)
        if val:
            return val
    return now + DEFAULT_COOLDOWN


def short_reason(stdout: str = "", stderr: str = "", limit: int = 160) -> str:
    """기록용 사유 한 줄. 자격정보가 섞이지 않도록 길이를 자른다."""
    text = haystack(stdout, stderr).strip().replace("\n", " ")
    for rx in _LIMIT_RE + _AUTH_RE:
        m = rx.search(text)
        if m:
            start = max(0, m.start() - 40)
            return text[start: start + limit].strip()
    return text[:limit].strip()
