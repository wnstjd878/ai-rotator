"""
== 운영 맥락 ==
용도: 구독 계정 순환의 "현재 상태"를 한 파일로 관리한다. 여러 봇이 동시에 호출해도
      계정 교체가 겹치지 않도록 잠금(대기 파일)으로 직렬화한다.
      claude_pool.py 가 이 모듈을 통해서만 상태를 읽고 쓴다.
상태 파일: state.json
      current      : 지금 자격이 꽂혀 있는 계정 라벨
      blocked      : {라벨: 한도가 풀리는 시각(epoch 초)} — 지금 못 쓰는 계정
      switched_at  : 마지막 교체 시각(epoch)
      history      : 최근 교체 기록 20건 (라벨, 시각, 사유)
동시성: with_lock() 안에서만 상태를 바꾼다. 잠금은 state.lock 파일이며
      macOS/리눅스는 fcntl, Windows 는 msvcrt 로 건다.
외부 의존: 없음 (표준 라이브러리만)
제공자 구분(2026-07-28 추가): 클로드와 지피티(Codex)가 같은 뼈대를 쓰되 상태는 따로 둔다.
      use("codex") 를 부르면 상태·계정 목록·자격 폴더가 codex 쪽으로 바뀐다.
      아무것도 안 부르면 예전 그대로(클로드) — 기존 호출부는 손댈 필요가 없다.
      공유 저장소는 한 테이블을 같이 쓰고 라벨 앞에 "codex:" 를 붙여 갈라놓는다.
의도적 미구현: 상태를 DB 로 옮기지 않는다. 한 머신 안에서만 쓰는 값이라 파일로 충분.
마지막 점검: 2026-07-28
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

try:  # 공유 저장소(세 머신이 같이 보는 한도 상태). 없어도 로컬 단독으로 돈다.
    import shared_state
except Exception:
    shared_state = None


def _shared_on() -> bool:
    return bool(shared_state and shared_state.enabled())


def claim_notice(key: str) -> bool:
    """알림을 지금 보낼 차례인지. 공유가 켜져 있으면 세 머신 통틀어 key 당 1회.

    공유가 꺼진 단독 머신이면 항상 True(그 머신 혼자라 중복 없음).
    """
    if _shared_on():
        try:
            return shared_state.claim_once(_to_shared(key))
        except Exception:
            return False
    return True

BASE = Path(__file__).resolve().parent

# 제공자별 자리. 기본은 클로드(예전 경로 그대로).
NAMESPACE = "claude"
SHARED_PREFIX = ""          # 공유 저장소 라벨 앞에 붙는 것. 클로드는 예전 데이터 호환으로 빈 값.
STATE_FILE = BASE / "state.json"
LOCK_FILE = BASE / "state.lock"
ACCOUNTS_FILE = BASE / "accounts.json"
CREDS_DIR = BASE / "creds"

_LAYOUT = {
    "claude": {"state": "state.json", "lock": "state.lock",
               "accounts": "accounts.json", "creds": "creds", "prefix": ""},
    "codex": {"state": "codex_state.json", "lock": "codex_state.lock",
              "accounts": "codex_accounts.json", "creds": "creds/codex", "prefix": "codex:"},
}


def use(namespace: str) -> None:
    """이 프로세스가 다룰 제공자를 정한다. codex_* 모듈이 불러오자마자 부른다.

    한 프로세스가 둘을 번갈아 쓰지는 않는다(가로채기도 래퍼도 한쪽만 다룬다).
    모르는 이름을 주면 예전 자리(클로드)를 그대로 쓴다 — 상태 파일이 엉뚱한 데
    생기는 것보다 낫다.
    """
    global NAMESPACE, SHARED_PREFIX, STATE_FILE, LOCK_FILE, ACCOUNTS_FILE, CREDS_DIR
    layout = _LAYOUT.get(namespace)
    if not layout:
        return
    NAMESPACE = namespace
    SHARED_PREFIX = layout["prefix"]
    STATE_FILE = BASE / layout["state"]
    LOCK_FILE = BASE / layout["lock"]
    ACCOUNTS_FILE = BASE / layout["accounts"]
    CREDS_DIR = BASE / layout["creds"]


def _to_shared(label: str) -> str:
    """이 제공자의 라벨을 공유 저장소용 라벨로."""
    return f"{SHARED_PREFIX}{label}"


def _from_shared(key: str):
    """공유 저장소 라벨을 이 제공자의 라벨로. 남의 제공자 것이면 None.

    클로드는 접두가 없으므로 콜론이 든 라벨(=다른 제공자)을 걸러야 한다.
    이게 없으면 지피티 계정이 막힌 걸 클로드가 자기 계정으로 잘못 읽는다.
    """
    key = str(key)
    if SHARED_PREFIX:
        return key[len(SHARED_PREFIX):] if key.startswith(SHARED_PREFIX) else None
    return None if ":" in key else key


HISTORY_MAX = 20
LOCK_TIMEOUT = 60  # 잠금 대기 상한(초). 넘으면 예외로 알린다.


class LockTimeout(RuntimeError):
    pass


def _lock_posix(fh, timeout):
    import fcntl

    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.time() > deadline:
                raise LockTimeout(f"계정 교체 잠금을 {timeout}초 동안 못 얻었습니다.")
            time.sleep(0.2)


def _lock_windows(fh, timeout):
    import msvcrt

    deadline = time.time() + timeout
    while True:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.time() > deadline:
                raise LockTimeout(f"계정 교체 잠금을 {timeout}초 동안 못 얻었습니다.")
            time.sleep(0.2)


def _unlock_windows(fh):
    import msvcrt

    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def with_lock(timeout: int = LOCK_TIMEOUT):
    """계정 교체 구간을 직렬화한다. 잠금 안에서만 상태를 바꿀 것."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "a+b")
    try:
        fh.seek(0)
        if os.name == "nt":
            fh.write(b"0")
            fh.flush()
            fh.seek(0)
            _lock_windows(fh, timeout)
        else:
            _lock_posix(fh, timeout)
        yield
    finally:
        if os.name == "nt":
            _unlock_windows(fh)
        fh.close()


def load_accounts() -> list[dict]:
    """accounts.json 의 계정 목록. 파일이 없거나 깨졌으면 빈 목록."""
    if not ACCOUNTS_FILE.exists():
        return []
    try:
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [a for a in data if isinstance(a, dict) and str(a.get("label", "")).strip()]


def account_labels() -> list[str]:
    return [str(a["label"]).strip() for a in load_accounts()]


def saved_labels() -> list[str]:
    """자격 저장본(creds/<라벨>.json)이 실제로 있는 라벨만."""
    if not CREDS_DIR.exists():
        return []
    return sorted(f.stem for f in CREDS_DIR.glob("*.json") if f.stat().st_size > 0)


def _blank_state() -> dict:
    return {"current": None, "blocked": {}, "switched_at": None, "history": [],
            "notified_for": None, "notified_at": None}


def load_state() -> dict:
    """상태 파일을 읽는다. 없거나 깨졌으면 기본값으로 시작한다(예외를 올리지 않는다)."""
    if not STATE_FILE.exists():
        return _blank_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _blank_state()
    if not isinstance(data, dict):  # 목록이나 숫자가 들어 있어도 죽지 않게
        return _blank_state()

    out = _blank_state()
    out.update({k: v for k, v in data.items()})
    if not isinstance(out.get("blocked"), dict):
        out["blocked"] = {}
    else:
        # 숫자가 아닌 해제 시각은 버린다. 그대로 두면 나중에 float 변환에서 터진다.
        clean = {}
        for k, v in out["blocked"].items():
            try:
                clean[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        out["blocked"] = clean
    if not isinstance(out.get("history"), list):
        out["history"] = []
    if not isinstance(out.get("current"), (str, type(None))):
        out["current"] = None
    return out


def save_state(state: dict) -> None:
    """원자 저장 — 임시 파일에 쓴 뒤 바꿔치기해서 반쯤 쓰인 파일이 안 남게."""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def prune_blocked(state: dict, now: float | None = None) -> dict:
    """한도가 풀린 계정을 막힘 목록에서 빼고, 공유 저장소의 막힘도 병합한다.

    공유가 켜져 있으면 세 머신이 같은 막힘 목록을 보게 되어 같은 계정에 수렴한다.
    공유를 못 읽으면(저장소 다운 등) 로컬 목록만으로 버틴다.
    """
    now = time.time() if now is None else now
    blocked = {}
    for k, v in (state.get("blocked") or {}).items():
        try:
            until = float(v)
        except (TypeError, ValueError):
            continue  # 숫자가 아닌 값은 버린다
        if until > now:
            blocked[k] = until

    if _shared_on():
        remote = shared_state.get_blocked(now)
        if remote is not None:  # 읽기 실패면 로컬만 유지
            for k, v in remote.items():
                label = _from_shared(k)  # 다른 제공자 것은 건너뛴다
                if label is None:
                    continue
                blocked[label] = max(blocked.get(label, 0.0), float(v))

    state["blocked"] = blocked
    if not blocked:
        state["notified_for"] = None  # 하나라도 풀렸으면 다음 소진 때 다시 알린다
    return state


def mark_notified(state: dict, reset_at: float | None, now: float | None = None) -> bool:
    """전 계정 소진 알림을 지금 보내야 하는지. 보낼 차례면 True 를 주고 표시한다.

    같은 소진 구간에서는 한 번만 보낸다. 봇 55곳이 각자 부르면 알림이 쏟아지기 때문.
    공유가 켜져 있으면 세 머신을 통틀어 한 번만 나가게 공유 저장소로 판단한다.
    """
    if _shared_on():
        # 제공자마다 따로 센다. 한 쪽 소진 알림이 다른 쪽 알림을 막으면 안 된다.
        got = shared_state.claim_notify(reset_at, meta_key=f"rotator_notified{'_' + NAMESPACE if SHARED_PREFIX else ''}")
        # 공유로 한 번 잡았으면 로컬에도 표시해 이 머신 안에서도 중복을 막는다
        if got:
            state["notified_for"] = round(float(reset_at)) if reset_at else None
            state["notified_at"] = time.time()
        return got

    now = time.time() if now is None else now
    key = round(float(reset_at)) if reset_at else None
    prev_key = state.get("notified_for")
    prev_at = state.get("notified_at")

    if key is not None and prev_key == key:
        return False
    if key is None and prev_at:
        try:
            if now - float(prev_at) < 3600:  # 해제 시각을 모를 땐 한 시간에 한 번
                return False
        except (TypeError, ValueError):
            pass
    state["notified_for"] = key
    state["notified_at"] = now
    return True


def is_blocked(state: dict, label: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    until = (state.get("blocked") or {}).get(label)
    return until is not None and float(until) > now


def mark_blocked(state: dict, label: str, until_epoch: float, reason: str = "") -> dict:
    """계정을 한도 도달로 표시(로컬 + 공유). 이미 더 늦은 해제 시각이 있으면 늦은 쪽을 유지."""
    blocked = dict(state.get("blocked") or {})
    prev = float(blocked.get(label, 0))
    blocked[label] = max(prev, float(until_epoch))
    state["blocked"] = blocked
    _push_history(state, label, "blocked", reason)
    if _shared_on():
        shared_state.set_blocked(_to_shared(label), until_epoch, reason)  # 세 머신이 같이 본다
    return state


def clear_blocked_label(state: dict, label: str) -> dict:
    """한 계정의 막힘을 푼다(로컬 + 공유). 사람이 --use 로 고른 계정 등."""
    blocked = dict(state.get("blocked") or {})
    blocked.pop(label, None)
    state["blocked"] = blocked
    if _shared_on():
        shared_state.clear_blocked(_to_shared(label))
    return state


def set_current(state: dict, label: str, reason: str = "") -> dict:
    state["current"] = label
    state["switched_at"] = time.time()
    _push_history(state, label, "switched", reason)
    return state


def _push_history(state: dict, label: str, kind: str, reason: str) -> None:
    hist = list(state.get("history") or [])
    hist.append({
        "at": time.time(),
        "label": label,
        "kind": kind,
        "reason": (reason or "")[:200],
    })
    state["history"] = hist[-HISTORY_MAX:]


def note_weekly_reset(state: dict, label: str, epoch, now: float | None = None) -> dict:
    """그 계정의 주간 초기화 시각을 기억한다. 다음 계정을 고르는 순서 기준이 된다.

    사용량을 읽는 곳(사용량 경고, 상태 보기, 켜기 전 확인, 전환)에서 부르면 저절로 쌓인다.
    이걸 위해 따로 조회하지는 않는다 — 조회가 잦으면 429 가 난다.
    """
    if not epoch:
        return state
    try:
        epoch = float(epoch)
    except (TypeError, ValueError):
        return state
    now = time.time() if now is None else now
    if epoch <= now:
        return state  # 이미 지난 시각은 낡은 값이다. 다음 조회 때 새 주기로 갱신된다
    r = dict(state.get("weekly_reset") or {})
    r[label] = round(epoch)
    state["weekly_reset"] = r
    return state


def remember_resets(pairs) -> None:
    """읽어둔 초기화 시각들을 상태 파일에 반영한다. 잠금은 저장하는 순간만 잡는다.

    이미 잠금을 쥔 곳에서는 부르지 말 것(중첩). 거기서는 note_weekly_reset 을 직접 쓴다.
    """
    pairs = [(l, e) for l, e in pairs if e]
    if not pairs:
        return
    with with_lock():
        s = prune_blocked(load_state())
        for l, e in pairs:
            note_weekly_reset(s, l, e)
        save_state(s)


def pick_next(state: dict, exclude: str | None = None, now: float | None = None) -> str | None:
    """지금 쓸 계정 라벨 하나. 없으면 None.

    순차 소진: 한 계정을 다 태우고 다음으로 간다(분산하지 않는다).
    순서는 **주간 초기화가 이른 계정부터**. 곧 초기화되는 계정의 남은 양은 안 쓰면 그냥
    사라지기 때문이다(2026-07-27 사용자 지적: 8/1 초기화인 A 보다 7/31 초기화인 B 를
    먼저 썼어야 했다). 초기화 시각을 아직 모르는 계정은 뒤로 밀고 accounts.json 순서를 따른다.
    막힌 계정은 건너뛴다. 5시간 한도로 잠깐 막힌 계정은 곧 풀려 다시 후보가 된다.
    공유 저장소가 켜져 있으면(prune_blocked 가 병합) 세 머신이 같은 막힘을 보고 같은 계정을 고른다.
    """
    now = time.time() if now is None else now
    saved = set(saved_labels())
    candidates = [l for l in account_labels()
                  if l != exclude and l in saved and not is_blocked(state, label=l, now=now)]
    order = priority_order(state, candidates, now)
    return order[0] if order else None


def priority_order(state: dict, labels=None, now: float | None = None) -> list:
    """계정을 실제로 고르는 순서로 정렬해 돌려준다.

    주간 초기화가 이른 것부터, 시각을 모르면 뒤로 밀고 accounts.json 순서.
    pick_next 와 알림의 '(다음)' 표시가 같은 규칙을 쓰게 하려고 여기 한 벌만 둔다.
    """
    now = time.time() if now is None else now
    base = account_labels()
    labels = list(base) if labels is None else list(labels)
    resets = state.get("weekly_reset") or {}

    def key(label):
        idx = base.index(label) if label in base else len(base)
        try:
            r = float(resets.get(label))
        except (TypeError, ValueError):
            r = None
        if r is None or r <= now:  # 모르거나 낡은 값이면 뒤로
            return (1, 0.0, idx)
        return (0, r, idx)

    return sorted(labels, key=key)


def earliest_reset(state: dict) -> float | None:
    """막힌 계정 중 가장 빨리 풀리는 시각(epoch). 막힌 게 없으면 None."""
    blocked = state.get("blocked") or {}
    if not blocked:
        return None
    return min(float(v) for v in blocked.values())


def describe(state: dict, now: float | None = None) -> str:
    """사람이 읽는 현재 상태 한 덩어리."""
    now = time.time() if now is None else now
    labels = account_labels()
    saved = set(saved_labels())
    lines = []
    cur = state.get("current")
    for label in labels:
        marks = []
        if label == cur:
            marks.append("현재 사용 중")
        if label not in saved:
            marks.append("자격 저장본 없음")
        until = (state.get("blocked") or {}).get(label)
        if until and float(until) > now:
            left = int(float(until) - now)
            marks.append(f"한도 도달, {left // 3600}시간 {left % 3600 // 60}분 뒤 풀림")
        elif label in saved:
            marks.append("사용 가능")
        lines.append(f"  {label}: " + ", ".join(marks))
    return "\n".join(lines) if lines else "  (accounts.json 에 계정이 없습니다)"
