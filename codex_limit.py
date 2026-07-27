"""
== 운영 맥락 ==
용도: codex 호출 결과가 "구독 한도에 걸려 거절된 것"인지 판정한다.
      codex_pool.py 가 계정을 바꿀지 말지 결정하는 근거로 쓴다.
입력: 종료 코드 + 표준출력/오류출력 문자열
출력: classify() → "ok" | "limit" | "auth" | "error"
      confirm_limit(cred_path) → True(진짜 한도) / False(아님) / None(못 읽음)

오판을 막는 세 겹 (클로드 쪽에서 실제로 사고가 났던 자리다):
      (1) 계정을 바꿔도 안 풀리는 "한도"를 판정 전에 지운다. codex 안에는 문맥 창,
          하위 작업 깊이, 재시도, 예산 같은 다른 한도 문구가 여럿 있다. 그냥 두면
          프롬프트가 길거나 예산에 걸렸을 때 멀쩡한 계정이 통째로 막힌다.
      (2) 한도 문구는 오류출력과 표준출력 꼬리(마지막 몇 줄)에서만 찾는다. codex 는
          모델 답변을 표준출력에 그대로 흘리므로, 답변 본문 한가운데의 'usage limit'
          같은 글자에 넘어가면 안 된다.
      (3) 그러고도 계정을 막기 전에 서버에 사용량을 한 번 물어 확증한다(confirm_limit).
          클로드에는 이 확증 단계가 없었다. 지피티는 조회 주소가 있어 넣을 수 있다.
의도적 미구현: 남은 사용량을 미리 재서 선제 전환하지 않는다(2026-07-28 사용자 결정).
      사용률은 알림으로만 알리고, 계정 교체는 거절을 받은 뒤에만 한다.
마지막 점검: 2026-07-28
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import limit_detect  # 해제 시각 읽기(문구형)는 클로드 쪽 한 벌을 그대로 쓴다  # noqa: E402

DEFAULT_COOLDOWN = limit_detect.DEFAULT_COOLDOWN  # 해제 시각을 못 읽었을 때 쉬게 할 시간(초)
TAIL_LINES = 25  # 표준출력에서 오류 문구를 찾을 꼬리 길이

# 구독 한도 거절로 보는 문구. codex 실행 파일 안에 실제로 박혀 있는 표기를 확인해 넣었다.
LIMIT_PATTERNS = [
    r"hit your usage limit",
    r"usage limit reached",
    r"reached your usage limit",
    r"usage_limit_reached",
    r"usage_limit_exceeded",
    r"usageLimitExceeded",
    r"workspace_(owner|member)_usage_limit_reached",
    r"rate_limit_exceeded",
    r"rate limit exceeded",
    r"too many requests",
    r"\b429\b",
    r"quota_exceeded",
    r"usage_not_included",
    r"out of credits",
    r"credit balance",
    r"사용량 한도",
    r"한도에 도달",
]

# 자격 문제로 보는 문구 (계정을 바꾼다고 풀리지 않음. 다시 로그인해야 함)
AUTH_PATTERNS = [
    r"not (logged in|authenticated)",
    r"please (run )?codex login",
    r"run `?codex login`?",
    r"authentication (required|failed|error)",
    r"invalid (api key|token|credentials)",
    r"(oauth|session) (token )?(expired|revoked)",
    # 실측 문구(2026-07-28): 낡은 자격 사본을 꽂으면 이게 나온다.
    # "Your access token could not be refreshed because your refresh token was revoked."
    r"refresh token was revoked",
    r"could not be refreshed",
    r"log out and sign in again",
    r"\bunauthorized\b",
    r"\b401\b",
]

# 계정을 바꿔도 안 풀리는 "한도" 들. 판정 전에 본문에서 지운다.
IGNORE_PATTERNS = [
    r"context[ _]?window (exceeded|limit)",
    r"contextWindowExceeded",
    r"agent (thread|depth) limit reached",
    r"depth limit reached",
    r"recursion limit reached",
    r"retry limit reached",
    r"not enough retry quota",
    r"connection limit reached",
    r"session[_ ]?budget[_ ]?exceeded",
    r"sessionBudgetExceeded",
    r"budget[_ ]?limit(ed|ExceededQ)?",
    r"budget exceeded",
    r"spend[_ ]?control",
    r"memory limit reached",
    r"out of memory",
    r"file watch limit reached",
    r"size limit reached",
    r"turn limit",
    r"prompt is too long",
]

_LIMIT_RE = [re.compile(p, re.I) for p in LIMIT_PATTERNS]
_AUTH_RE = [re.compile(p, re.I) for p in AUTH_PATTERNS]
_IGNORE_RE = re.compile("|".join(IGNORE_PATTERNS), re.I)


def is_failure(returncode: int, stdout: str = "") -> bool:
    """호출이 실패로 끝났는지.

    codex 는 한도 거절을 0 이 아닌 종료 코드로 낸다. 종료 코드가 0 인데 답변 안에
    한도 얘기가 적혀 있는 경우는 실패가 아니다(모델이 그 얘기를 한 것뿐).
    """
    return returncode != 0


def failure_text(stdout: str = "", stderr: str = "") -> str:
    """한도/자격 문구를 찾을 자리. 오류출력 전부 + 표준출력의 꼬리 몇 줄.

    codex 는 모델 답변을 표준출력에 그대로 흘린다. 본문 한가운데를 다 뒤지면
    "사용량 한도에 걸린 로그를 분석해줘" 같은 일을 시켰을 때 그 답변에 넘어간다.
    거절은 항상 마지막에 찍히므로 꼬리만 본다.
    """
    tail = "\n".join((stdout or "").splitlines()[-TAIL_LINES:])
    return f"{stderr or ''}\n{tail}"


def strip_ignored(text: str) -> str:
    """계정을 바꿔도 안 풀리는 한도 문구를 지운 본문. 오판을 막는다."""
    return _IGNORE_RE.sub(" ", text or "")


def classify(returncode: int, stdout: str = "", stderr: str = "") -> str:
    """호출 결과를 ok / limit / auth / error 중 하나로 판정 (1차, 문구 기반)."""
    if not is_failure(returncode, stdout):
        return "ok"

    text = failure_text(stdout, stderr)
    for rx in _AUTH_RE:
        if rx.search(text):
            return "auth"
    cleaned = strip_ignored(text)
    for rx in _LIMIT_RE:
        if rx.search(cleaned):
            return "limit"
    return "error"


def confirm_limit(cred_path, *, is_current: bool = True):
    """진짜 구독 한도인지 서버에 물어 확증한다. True / False / None(못 읽음).

    문구만 믿고 계정을 막으면, 답변 꼬리에 우연히 한도 얘기가 실렸을 때 멀쩡한 계정이
    막힌다. 조회를 못 하면 None 을 주고, 호출부는 문구 판정을 그대로 따른다
    (조회 실패 때문에 순환이 멈추면 안 된다).
    """
    try:
        import codex_usage
    except Exception:
        return None
    u = codex_usage.get_usage_for(cred_path, is_current=is_current)
    if not u:
        return None
    if u.get("limit_reached") or u.get("reached_type"):
        return True
    for key in ("weekly_pct", "session_pct"):
        pct = u.get(key)
        if pct is not None and float(pct) >= 99.0:
            return True
    return False


def reset_epoch_from_usage(usage: dict | None, kind: str | None = None):
    """사용량 응답에서 한도 해제 시각(epoch)을 읽는다. 못 읽으면 None."""
    if not usage:
        return None
    order = [kind] if kind else []
    order += ["weekly", "session"]
    now = time.time()
    for k in order:
        if not k:
            continue
        val = usage.get(f"{k}_reset")
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        if val > now:
            return val
    return None


def reset_epoch(stdout: str = "", stderr: str = "", now: float | None = None) -> float:
    """문구에서 읽는 해제 시각. 못 읽으면 기본 5시간 뒤. (사용량 조회가 1순위)"""
    now = time.time() if now is None else now
    text = failure_text(stdout, stderr)
    for parse in (limit_detect._from_epoch_token, limit_detect._from_date_clock,
                  limit_detect._from_clock):
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
