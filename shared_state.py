"""
== 운영 맥락 ==
용도: 계정별 한도 상태(어느 계정이 언제까지 막혔나)를 세 머신(Windows 본체·Mac·winserver)이
      한 곳에서 같이 보게 한다. Supabase 테이블 하나를 공유 칠판으로 쓴다.
      이게 있어야 세 머신이 같은 계정을 순서대로(A→B→C→D) 태운다. 없으면 각자
      다른 계정에 앉아 흩어진다.
왜 공유가 필요한가: 순차 소진(한 계정을 주간한도까지 다 쓰고 다음)은 세 머신이 "지금 어느
      계정이 막혔는지"를 같이 알아야 같은 계정을 고른다. 자격(로그인)은 머신마다 따로지만,
      한도는 계정 단위라 세 머신이 공유하는 게 맞다.
설정: 환경변수 두 개가 있어야 켜진다(없으면 이 머신은 로컬 단독으로 돈다).
      ROTATOR_SUPABASE_URL   = https://<ref>.supabase.co
      ROTATOR_SUPABASE_KEY   = service_role 키 (각 머신 .env 에 직접 넣는다. 채팅 금지)
테이블(신규 Supabase 에 한 번 만든다):
      rotator_accounts(label text primary key, blocked_until timestamptz, reason text,
                       updated_at timestamptz default now())
      rotator_meta(key text primary key, value text, updated_at timestamptz default now())
동작 원칙: 공유 저장소가 죽어도 봇을 멈추지 않는다. 읽기 실패는 None 을 돌려주고, 호출부는
      로컬 상태로 버틴다. 쓰기 실패는 False 를 돌려주고 로그만 남긴다.
외부 의존: 표준 라이브러리만(urllib). requests 없어도 된다.
자격 취급: 키는 환경변수에서만 읽고 화면·기록·명령 인자에 싣지 않는다.
마지막 점검: 2026-07-26
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent

URL_ENV = "ROTATOR_SUPABASE_URL"
KEY_ENV = "ROTATOR_SUPABASE_KEY"


def _env_file_val(key: str) -> str | None:
    """ai-rotator/.env 에서 값을 읽는다. 봇·스케줄러 환경을 안 건드려도 되게.

    환경변수가 우선이고(_base/_key), 없을 때만 이 파일을 본다.
    """
    p = BASE / ".env"
    if not p.exists():
        return None
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except Exception:
        return None
    return None
ACCOUNTS_TABLE = "rotator_accounts"
META_TABLE = "rotator_meta"
TIMEOUT = 8  # 공유 저장소 통신 상한(초). 넘으면 로컬로 버틴다.


def _base() -> str | None:
    url = (os.environ.get(URL_ENV) or _env_file_val(URL_ENV) or "").strip().rstrip("/")
    return url or None


def _key() -> str | None:
    return (os.environ.get(KEY_ENV) or _env_file_val(KEY_ENV) or "").strip() or None


def enabled() -> bool:
    """공유 저장소를 쓸 수 있는 설정인지."""
    return bool(_base() and _key())


def _headers(extra: dict | None = None) -> dict:
    key = _key() or ""
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _request(method: str, path: str, *, params: str = "", body=None, headers=None):
    """Supabase REST 한 번 호출. 실패하면 None(호출부가 로컬로 버틴다)."""
    base = _base()
    if not base or not _key():
        return None
    url = f"{base}/rest/v1/{path}"
    if params:
        url += f"?{params}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_headers(headers))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else []
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
        return None


def _to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


def _from_iso(text: str) -> float | None:
    if not text:
        return None
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def get_blocked(now: float | None = None) -> dict | None:
    """{라벨: 한도 풀리는 시각(epoch)}. 지금 막힌 계정만. 읽기 실패면 None."""
    now = time.time() if now is None else now
    rows = _request("GET", ACCOUNTS_TABLE, params="select=label,blocked_until")
    if rows is None:
        return None
    out = {}
    for r in rows:
        until = _from_iso(r.get("blocked_until") or "")
        if until is not None and until > now:
            out[str(r.get("label"))] = until
    return out


def set_blocked(label: str, until_epoch: float, reason: str = "") -> bool:
    """계정을 한도 도달로 표시(공유). 이미 더 늦은 해제 시각이 있으면 그대로 둔다."""
    # 먼저 현재 값을 보고 더 늦은 쪽을 유지한다. 못 읽으면 그냥 새 값으로 upsert.
    cur = _request("GET", ACCOUNTS_TABLE,
                   params=f"label=eq.{label}&select=blocked_until")
    until = float(until_epoch)
    if cur:
        prev = _from_iso((cur[0] or {}).get("blocked_until") or "")
        if prev is not None:
            until = max(until, prev)
    body = [{
        "label": label,
        "blocked_until": _to_iso(until),
        "reason": (reason or "")[:180],
        "updated_at": _to_iso(time.time()),
    }]
    res = _request("POST", ACCOUNTS_TABLE, body=body,
                   headers={"Prefer": "resolution=merge-duplicates"})
    return res is not None


def clear_blocked(label: str) -> bool:
    """계정 한도 표시를 푼다(공유). 사람이 --use 로 고른 계정 등."""
    body = {"blocked_until": None, "reason": "", "updated_at": _to_iso(time.time())}
    res = _request("PATCH", ACCOUNTS_TABLE, params=f"label=eq.{label}", body=body)
    return res is not None


def claim_notify(reset_at: float | None, meta_key: str = "rotator_notified") -> bool:
    """전 계정 소진 알림을 지금 이 머신이 보낼 차례인지(공유). 소진 구간당 한 번만 True.

    세 머신이 동시에 소진을 만나도 rotator_meta 의 notified_for 로 한 건만 나가게 한다.
    경합으로 두 건 나가는 극단은 허용한다(알림이 하나 더 오는 정도).
    meta_key 는 제공자별로 다르다(클로드는 예전 이름 그대로, 지피티는 뒤에 codex 가 붙는다).
    """
    key = str(round(float(reset_at))) if reset_at else "unknown"
    cur = _request("GET", META_TABLE,
                   params=f"key=eq.{urllib.parse.quote(meta_key, safe='')}&select=value")
    if cur is None:
        return False  # 공유 못 읽으면 알림은 로컬 판단에 맡긴다
    prev = (cur[0].get("value") if cur else None)
    if prev == key:
        return False
    body = [{"key": meta_key, "value": key, "updated_at": _to_iso(time.time())}]
    res = _request("POST", META_TABLE, body=body,
                   headers={"Prefer": "resolution=merge-duplicates"})
    return res is not None


def claim_once(key: str) -> bool:
    """어떤 알림을 이 머신이 처음 보내는지(공유). 세 머신 통틀어 key 당 한 번만 True.

    계정 전환 알림이 세 머신에서 각자 나가지 않게 한다. 공유를 못 읽으면 False(중복 회피).
    """
    q = urllib.parse.quote(key, safe="")
    cur = _request("GET", META_TABLE, params=f"key=eq.{q}&select=value")
    if cur is None:
        return False  # 공유 못 읽으면 알림을 보내지 않는다(중복보다 누락이 낫다)
    if cur:
        return False  # 이미 누군가 보냈다
    res = _request("POST", META_TABLE,
                   body=[{"key": key, "value": "1", "updated_at": _to_iso(time.time())}],
                   headers={"Prefer": "resolution=merge-duplicates"})
    return res is not None


USAGE_KEY_PREFIX = "usage:"
CURRENT_KEY_PREFIX = "current:"
CURRENT_STALE = 6 * 3600  # 이 시간 넘게 소식 없는 머신은 그 계정을 쓴다고 보지 않는다

# 기계 이름은 사람이 알아보기 어렵다. 화면에 찍을 때만 바꿔 쓴다(값 자체는 기계 이름 그대로).
HOST_ALIAS = {
    "DESKTOP-LR51NVM": "본체",
    "DESKTOP-FIVP2HL": "winserver",
    "gwon-ui-Macmini": "Mac",
}


def _hostname() -> str:
    import socket
    try:
        return socket.gethostname().split(".")[0]
    except Exception:
        return "?"


def host_label(host: str) -> str:
    """기계 이름을 화면용 이름으로. 모르는 이름은 그대로 둔다."""
    return HOST_ALIAS.get(str(host or ""), str(host or "?"))


def put_usage(label: str, usage: dict, host: str | None = None) -> bool:
    """그 계정에서 방금 읽은 사용량을 공유 칠판에 적는다(보여주기 전용).

    지금 안 쓰는 계정은 저장본의 출입증이 8시간이라 그 머신에서는 조회가 안 된다.
    그런데 세 머신이 서로 다른 계정에 앉아 있으므로, 각자 읽은 값을 여기 올려두면
    아무 머신에서나 계정 넷의 잔여량을 다 볼 수 있다. 자격을 새로 발급받을 필요가 없다.
    **계정을 고르거나 막는 판단에는 쓰지 않는다** — 남의 머신이 읽은 시점의 값이라 낡을 수 있다.
    """
    if not usage:
        return False
    body = [{
        "key": USAGE_KEY_PREFIX + label,
        "value": json.dumps({
            "weekly_pct": usage.get("weekly_pct"),
            "session_pct": usage.get("session_pct"),
            "weekly_reset": usage.get("weekly_reset"),
            "session_reset": usage.get("session_reset"),
            "at": round(time.time()),
            "host": (host or _hostname())[:40],
        }, ensure_ascii=False),
        "updated_at": _to_iso(time.time()),
    }]
    res = _request("POST", META_TABLE, body=body,
                   headers={"Prefer": "resolution=merge-duplicates"})
    return res is not None


def get_usage_all() -> dict | None:
    """{라벨: 사용량} 공유 칠판에 올라온 값 전체. 읽기 실패면 None(호출부가 로컬로 버틴다).

    행 수가 몇 개뿐이라 전체를 받아 걸러낸다(패턴 조회보다 확실하다).
    """
    rows = _request("GET", META_TABLE, params="select=key,value")
    if rows is None:
        return None
    out = {}
    for r in rows:
        key = str(r.get("key") or "")
        if not key.startswith(USAGE_KEY_PREFIX):
            continue
        try:
            val = json.loads(r.get("value") or "")
        except (ValueError, TypeError):
            continue
        if isinstance(val, dict):
            out[key[len(USAGE_KEY_PREFIX):]] = val
    return out


def put_current(label: str, host: str | None = None) -> bool:
    """이 머신이 지금 어느 계정에 물려 있는지 공유 칠판에 적는다.

    자격은 머신마다 따로라, 세 머신이 서로 다른 계정에 앉아 있는 게 정상이다. 그런데
    한 화면에서는 자기 계정만 보여서 "지금 누가 어디에 붙어 있나" 를 알 수 없었다.
    머신당 한 줄(`current:<기계이름>`)만 쓰므로 행이 늘지 않는다.
    """
    if not label:
        return False
    body = [{
        "key": CURRENT_KEY_PREFIX + (host or _hostname()),
        "value": json.dumps({"label": label, "at": round(time.time())}, ensure_ascii=False),
        "updated_at": _to_iso(time.time()),
    }]
    res = _request("POST", META_TABLE, body=body,
                   headers={"Prefer": "resolution=merge-duplicates"})
    return res is not None


def get_current_all(now: float | None = None) -> dict | None:
    """{라벨: [머신 이름...]} 지금 그 계정에 물려 있는 머신들. 읽기 실패면 None.

    오래된 기록은 뺀다. 꺼진 머신이 몇 달 전 계정을 아직 쓰는 것처럼 보이면 안 된다.
    """
    now = time.time() if now is None else now
    rows = _request("GET", META_TABLE, params="select=key,value")
    if rows is None:
        return None
    out = {}
    for r in rows:
        key = str(r.get("key") or "")
        if not key.startswith(CURRENT_KEY_PREFIX):
            continue
        try:
            val = json.loads(r.get("value") or "")
            at = float(val.get("at") or 0)
        except (ValueError, TypeError, AttributeError):
            continue
        if not isinstance(val, dict) or not val.get("label"):
            continue
        if at and now - at > CURRENT_STALE:
            continue
        out.setdefault(str(val["label"]), []).append(host_label(key[len(CURRENT_KEY_PREFIX):]))
    for labels in out.values():
        labels.sort()
    return out


def snapshot() -> list | None:
    """상태 점검용 전체 행. --status 에서 쓴다."""
    return _request("GET", ACCOUNTS_TABLE,
                    params="select=label,blocked_until,reason,updated_at&order=label")


SCHEMA_SQL = """
-- 신규 Supabase SQL Editor 에 한 번 붙여넣어 실행한다.
create table if not exists rotator_accounts (
  label        text primary key,
  blocked_until timestamptz,
  reason       text,
  updated_at   timestamptz default now()
);
create table if not exists rotator_meta (
  key        text primary key,
  value      text,
  updated_at timestamptz default now()
);
-- service_role 로만 접근하므로 RLS 는 켜고 정책은 두지 않는다(서버 키는 RLS 우회).
alter table rotator_accounts enable row level security;
alter table rotator_meta enable row level security;
""".strip()


if __name__ == "__main__":
    import sys

    if not enabled():
        print(f"공유 저장소가 꺼져 있습니다. {URL_ENV} / {KEY_ENV} 를 .env 에 넣으세요.")
        print("\n[테이블 만들기] 신규 Supabase SQL Editor 에 아래를 붙여넣어 실행:\n")
        print(SCHEMA_SQL)
        sys.exit(1)
    rows = snapshot()
    if rows is None:
        print("공유 저장소에 접속하지 못했습니다. URL/키/네트워크를 확인하세요.")
        sys.exit(1)
    print("공유 저장소 연결 정상. 현재 계정 상태:")
    now = time.time()
    for r in rows or []:
        until = _from_iso(r.get("blocked_until") or "")
        if until and until > now:
            left = int(until - now)
            mark = f"막힘({left // 3600}시간 {left % 3600 // 60}분 뒤 풀림)"
        else:
            mark = "사용 가능"
        print(f"  {r.get('label')}: {mark}")
    if not rows:
        print("  (아직 기록 없음. 계정이 한도에 걸리면 채워집니다)")
