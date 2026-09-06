"""
== 운영 맥락 ==
용도: 계정별 사용량(세션 5시간 한도 %, 주간 한도 %)을 claude 서버에서 읽는다.
      claude Code 설정 화면의 "현재 세션 / 주간 한도" 와 같은 데이터다.
호출: /api/oauth/usage (Bearer accessToken). creds/<라벨>.json 의 accessToken 사용.
      응답: five_hour.utilization(세션 %), seven_day.utilization(주간 %), 각 resets_at,
      그리고 limits[] 안의 모델별 주간 칸(kind=="weekly_scoped", 화면 표시는 'Fable').
출력: get_usage(cred_path) → {session_pct, weekly_pct, session_reset(epoch), weekly_reset(epoch),
      scoped: {"Fable": {"pct": 100.0, "reset": epoch}}}
모델별 칸: 계정 전체 주간이 60% 여유여도 Fable 만 100% 소진일 수 있다(2026-08-12 실제).
      그래서 which_limit(..., bucket="Fable") 로 그 칸만 따로 물어볼 수 있게 뒀다.
      칸 정보가 안 오면 None 이다 — 모르는 걸 소진으로 보면 멀쩡한 계정을 막는다.
      실패(네트워크·토큰 만료 등)면 None. 봇을 멈추지 않는다.
      알림 문구용 공용 표기도 여기 둔다: fmt_reset_full(초기화 시각+남은 기간),
      pct_text(남은량), left_phrase(남은 기간), weekly_left(주간 남은량).
      사용량 경고와 계정 전환 알림이 같은 표기를 쓰게 하려는 것이다.
자격 취급: accessToken 은 파일에서 읽어 헤더에만 싣는다. 값은 화면·기록에 안 남긴다.
          돌려주는 건 사용률 %(자격 아님)뿐.
마지막 점검: 2026-07-27
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
UA = "claude-cli/2.1.0 (external, cli)"
TIMEOUT = 10
_WEEKDAY = "월화수목금토일"
last_error = None  # 마지막 조회 실패 사유(종류만, 자격 값은 절대 안 담는다)
last_status = None  # 마지막 조회의 서버 응답 번호. 401(자격 만료)만 갱신을 부른다.

# 같은 계정을 여러 부름(주기 작업 --tend, 봇 shim 켜기 전 확인, cs)이 잇달아 조회하면
# 서버가 429(조회 제한)로 막는다(2026-09-06 실측: --tend 를 켠 직후부터 시작).
# 상태줄 조회기(usage_probe.py)처럼 짧은 TTL 캐시 + 429 백오프를 둬서 조회 수를 줄인다.
# 캐시는 계정 라벨별로 마지막 성공값과 429 쉬는 시각만 담는다(자격 값은 절대 안 담는다).
_CACHE_TTL = 120      # 이 초 안에 읽어둔 성공값이 있으면 다시 조회하지 않는다
_BACKOFF_429 = 90     # 429 를 받으면 이 초 동안 그 계정을 다시 두드리지 않는다
_CACHE_FILE = Path.home() / ".claude" / ".rotator-usage-cache.json"


def _label_of(cred_path) -> str:
    """creds/<라벨>.json 에서 '<라벨>'. 지금 계정 살아있는 자격(.credentials)도 제 이름으로 담는다."""
    try:
        return Path(cred_path).stem
    except Exception:
        return str(cred_path)


def _cache_load() -> dict:
    try:
        return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cache_save(cache: dict) -> None:
    """원자적으로 쓴다(찢긴 쓰기를 다음 읽기가 무시하게). 실패해도 조용히 넘긴다 - 캐시는 편의다."""
    tmp = None
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_CACHE_FILE.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, str(_CACHE_FILE))
    except Exception:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


# 상태줄 조회기(usage_probe.py)가 현재 계정을 이미 180초마다 조회해 여기에 적는다.
# 순환기가 그 값을 재사용하면 같은 창구(/api/oauth/usage)를 두 번 두드리지 않는다 —
# 이 두 겹침이 2026-09-06 429 폭주의 원인이었다(tend 켜기 전엔 조회기 하나뿐이라 없었다).
_PROBE_CACHE = Path.home() / ".claude" / ".usage-cache.json"
_PROBE_TTL = 200      # 조회기가 적어둔 값을 이 초 안이면 그대로 쓴다(제 조회를 안 한다)


def _probe_usage(label: str):
    """상태줄 조회기가 적어둔 현재 계정 사용량을 순환기 형태로 돌려준다. 없거나 낡거나
    다른 계정이면 None(그러면 제 조회로 떨어진다). 그 파일은 현재 계정만 적으므로
    account 가 이 라벨일 때만 쓴다. 세션·주간이 둘 다 없으면(옛 판) 계정 전체 한도를
    몰라 세션 소진을 놓치므로 안 쓴다."""
    try:
        d = json.loads(_PROBE_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if d.get("account") != label:
        return None
    if (time.time() - float(d.get("at") or 0)) >= _PROBE_TTL:
        return None
    se = d.get("session") or {}
    wk = d.get("weekly") or {}
    if se.get("pct") is None and wk.get("pct") is None:
        return None
    scoped = {}
    for row in (d.get("scoped") or []):
        nm = row.get("name")
        if nm:
            scoped[nm] = {"pct": row.get("used_pct"), "reset": row.get("resets_at")}
    return {
        "session_pct": se.get("pct"), "session_reset": se.get("reset"),
        "weekly_pct": wk.get("pct"), "weekly_reset": wk.get("reset"),
        "scoped": scoped,
    }


def _iso_to_epoch(text):
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def get_usage(cred_path) -> dict | None:
    """creds/<라벨>.json 의 계정 사용량을 읽는다. 실패하면 None.

    실패 사유는 last_error 에 남는다(값이 아니라 종류만). 조회가 너무 잦으면 429 가 나는데,
    사유 없이 None 만 돌려주면 토큰 만료와 구분이 안 돼 엉뚱한 데를 파게 된다.
    """
    global last_error, last_status
    last_error = None
    last_status = None

    # 잇단 조회를 줄인다: 최근 성공값이 살아 있으면 통신 없이 그대로 돌려주고,
    # 방금 429 를 받았으면 쉬는 동안 다시 두드리지 않는다. 판단은 실제 거절로도 잡히므로
    # 최대 _CACHE_TTL 초 낡은 값으로 결정해도 안전하다(상태줄 조회기와 같은 규율).
    label = _label_of(cred_path)
    now = time.time()
    cache = _cache_load()
    ent = cache.get(label) or {}
    if ent.get("usage") and (now - float(ent.get("at") or 0)) < _CACHE_TTL:
        return copy.deepcopy(ent["usage"])
    if float(ent.get("backoff_until") or 0) > now:
        last_status = 429
        last_error = "조회 제한(429). 잠시 뒤 다시"
        return None  # 쉬는 중: 통신하지 않는다(멀쩡한 계정을 막지 않게 값도 지어내지 않는다)

    # 상태줄 조회기가 방금 이 계정을 조회해 뒀으면 그 값을 재사용한다(제 조회를 안 해 429 를 피한다).
    pu = _probe_usage(label)
    if pu is not None:
        cache[label] = {"at": now, "usage": pu}
        _cache_save(cache)
        return pu

    try:
        d = json.load(open(cred_path, encoding="utf-8"))
    except Exception:
        last_error = "자격 파일 못 읽음"
        return None
    tok = (d.get("claudeAiOauth") or {}).get("accessToken")
    if not tok:
        last_error = "자격 파일에 접근 토큰 없음"
        return None
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + tok,
        "User-Agent": UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            j = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        last_status = e.code
        last_error = "조회 제한(429). 잠시 뒤 다시" if e.code == 429 else f"HTTP {e.code}"
        if e.code == 429:
            ent["backoff_until"] = now + _BACKOFF_429  # 그 계정을 잠시 쉬게 한다(이전 성공값은 남겨둔다)
            cache[label] = ent
            _cache_save(cache)
        return None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        last_error = type(e).__name__
        return None
    fh = j.get("five_hour") or {}
    sd = j.get("seven_day") or {}
    usage = {
        "session_pct": fh.get("utilization"),
        "session_reset": _iso_to_epoch(fh.get("resets_at")),
        "weekly_pct": sd.get("utilization"),
        "weekly_reset": _iso_to_epoch(sd.get("resets_at")),
        "scoped": _scoped(j),
    }
    cache[label] = {"at": now, "usage": usage}  # 성공하면 백오프도 지운다
    _cache_save(cache)
    return usage


def _scoped(body: dict) -> dict:
    """모델별 주간 칸. {"Fable": {"pct": 100.0, "reset": epoch}} 모양.

    서버는 이걸 `seven_day_opus` 로도 주지만 그 자리는 비어 있을 때가 있고 모델 이름이 없다.
    이름이 붙어 오는 건 `limits[]` 쪽뿐이라 그쪽만 읽는다.
    없으면 빈 것으로 둔다 — 정보가 없는 걸 소진으로 보면 멀쩡한 계정을 막는다.
    """
    out = {}
    for item in (body.get("limits") or []):
        if not isinstance(item, dict) or item.get("kind") != "weekly_scoped":
            continue
        name = (((item.get("scope") or {}).get("model") or {}).get("display_name") or "").strip()
        if not name:
            continue
        try:
            pct = float(item.get("percent"))
        except (TypeError, ValueError):
            continue
        out[name] = {"pct": pct, "reset": _iso_to_epoch(item.get("resets_at"))}
    return out


def bucket_for_model(model: str | None) -> str | None:
    """쓰려는 모델 이름 → 그 모델이 걸리는 칸 이름. 모르면 None(계정 전체 한도만 본다).

    `fable[1m]`, `claude-fable-5`, `Fable 5` 처럼 표기가 제각각이라 포함 여부로 본다.
    Opus·Sonnet 은 따로 칸이 잡혀 오면 그 칸을, 안 오면 계정 전체 한도만 보게 된다.
    """
    if not model:
        return None
    low = str(model).lower()
    for name in ("Fable", "Opus", "Sonnet", "Haiku"):
        if name.lower() in low:
            return name
    return None


def scoped_pct(usage: dict | None, bucket: str | None):
    """그 칸을 얼마나 썼는지(%). 칸 정보가 없으면 None."""
    if not usage or not bucket:
        return None
    row = (usage.get("scoped") or {}).get(bucket)
    if not isinstance(row, dict):
        return None
    pct = row.get("pct")
    return None if pct is None else float(pct)


def scoped_reset(usage: dict | None, bucket: str | None):
    """그 칸이 언제 다시 채워지는지(epoch). 없으면 None."""
    if not usage or not bucket:
        return None
    row = (usage.get("scoped") or {}).get(bucket)
    return row.get("reset") if isinstance(row, dict) else None


def get_usage_for(cred_path, *, is_current: bool = False,
                  allow_refresh: bool = True) -> dict | None:
    """사용량을 읽는다. 자격이 낡아 거절당하면 계정에 따라 갈래가 다르다.

    creds/<라벨>.json 의 accessToken 은 수명이 짧다(실측 약 8시간).
    - **지금 쓰는 계정**: claude 가 ~/.claude/.credentials.json 을 갱신해 주므로 그 파일로
      한 번 더 시도한다. 여기서 우리가 자격을 갱신하면 claude 가 들고 있는 값이 폐기되므로
      갱신은 하지 않는다(2026-07-31 Mac 봇 한 시간 정지가 그 유형이다).
      살아있는 파일을 읽는 이 갈래는 현재 계정에만 쓴다 — 다른 계정에 쓰면 남의 사용량을
      그 계정 것으로 잘못 보여준다.
    - **나머지 계정**: 아무도 안 갱신해 주는 자리다. 저장된 재발급 열쇠로 새 접근 열쇠를 받아
      한 번만 다시 읽는다(2026-08-27 추가). 이게 없으면 몇 시간 뒤부터 화면이 낡은 기록으로
      떨어지고, 사용량 경고도 조용히 안 나간다(2026-07-27 실제로 그랬다).
      자격 만료(401)일 때만 시도한다 — 조회 제한(429)이나 네트워크 문제에 열쇠를 태우지 않는다.
    """
    u = get_usage(cred_path)
    if u:
        return u
    if is_current:
        live = Path.home() / ".claude" / ".credentials.json"
        if not live.exists():
            return None
        # 지금 계정이 429 로 쉬는 중이면 살아있는 자격도 두드리지 않는다. 안 그러면 저장 자격이
        # 쉬는 동안 이 갈래가 .credentials 라는 딴 키로 한 번 더 두드려 폭주가 반만 잡힌다(2026-09-06).
        ent = _cache_load().get(_label_of(cred_path)) or {}
        if float(ent.get("backoff_until") or 0) > time.time():
            globals()["last_status"] = 429
            globals()["last_error"] = "조회 제한(429). 잠시 뒤 다시"
            return None
        return get_usage(live)
    if allow_refresh and last_status == 401:
        why = None
        try:
            import cred_refresh

            if cred_refresh.refresh_file(cred_path):
                return get_usage(cred_path)
            why = cred_refresh.last_error
        except Exception as e:
            why = type(e).__name__
        # 갱신이 안 되면 화면은 기록으로 떨어지면 된다. 다만 **사유는 남긴다**:
        # 자격 저장이 깨져 그 계정을 잃은 경우가 섞여 있어서, 조용히 넘기면
        # 왜 계속 낡은 값만 보이는지 아무도 모른다.
        if why:
            globals()["last_error"] = f"자격 만료, 갱신도 실패({why})"
    return None


def which_limit(usage: dict | None, threshold: float = 95.0,
                bucket: str | None = None) -> str | None:
    """도달(임박)한 한도 종류. 'session' / 'weekly' / 'model:<칸이름>' / None.

    `bucket` 을 주면 그 모델 칸이 찼는지도 본다. 계정 전체는 여유가 남았는데 그 모델만
    바닥난 경우가 실제로 있고(2026-08-12 Fable), 그때 이 인자가 없으면 안 걸린다.
    계정 전체 한도를 먼저 보는 이유: 그쪽이 막히면 어느 모델도 못 쓰기 때문이다.
    """
    if not usage:
        return None
    wk = usage.get("weekly_pct")
    se = usage.get("session_pct")
    if wk is not None and wk >= threshold:
        return "weekly"
    if se is not None and se >= threshold:
        return "session"
    mp = scoped_pct(usage, bucket)
    if mp is not None and mp >= threshold:
        return f"model:{bucket}"
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


def fmt_reset(epoch) -> str:
    """리셋 시각을 ' (MM/DD HH:MM 풀림)' 로. 없으면 빈 문자열.

    서버에서 방금 읽은 값(summarize)과 기록해둔 값(usage_view.format_parts)이 **같은 문구**를
    쓰게 하려고 공개해 둔다. 두 벌로 갈라 두면 한쪽만 고치게 된다.
    """
    if not epoch:
        return ""
    import time
    return f" ({time.strftime('%m/%d %H:%M', time.localtime(float(epoch)))} 풀림)"


_fmt_reset = fmt_reset  # 옛 이름(이 파일 안 호출부용)


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
    # 모델 칸(Fable 등). 주간이 40% 남았어도 이 칸은 0% 일 수 있어서 따로 적는다.
    for name, row in sorted((usage.get("scoped") or {}).items()):
        if not isinstance(row, dict) or row.get("pct") is None:
            continue
        parts.append(f"{name} 남은 {max(0, 100 - float(row['pct'])):.0f}%"
                     f"{_fmt_reset(row.get('reset'))}")
    return ", ".join(parts) if parts else "사용량 없음"


if __name__ == "__main__":
    import sys
    from pathlib import Path

    creds = Path(__file__).resolve().parent / "creds"
    for f in sorted(creds.glob("*.json")):
        print(f.stem, "->", summarize(get_usage(f)))
