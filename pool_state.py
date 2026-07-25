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
의도적 미구현: 상태를 DB 로 옮기지 않는다. 한 머신 안에서만 쓰는 값이라 파일로 충분.
마지막 점검: 2026-07-22
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

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "state.json"
LOCK_FILE = BASE / "state.lock"
ACCOUNTS_FILE = BASE / "accounts.json"
CREDS_DIR = BASE / "creds"

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
                blocked[k] = max(blocked.get(k, 0.0), float(v))

    state["blocked"] = blocked
    if not blocked:
        state["notified_for"] = None  # 하나라도 풀렸으면 다음 소진 때 다시 알린다
    return state


def mark_notified(state: dict, reset_at: float | None, now: float | None = None) -> bool:
    """전 계정 소진 알림을 지금 보내야 하는지. 보낼 차례면 True 를 주고 표시한다.

    같은 소진 구간에서는 한 번만 보낸다. 봇 55곳이 각자 부르면 알림이 쏟아지기 때문.
    """
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
        shared_state.set_blocked(label, until_epoch, reason)  # 세 머신이 같이 본다
    return state


def clear_blocked_label(state: dict, label: str) -> dict:
    """한 계정의 막힘을 푼다(로컬 + 공유). 사람이 --use 로 고른 계정 등."""
    blocked = dict(state.get("blocked") or {})
    blocked.pop(label, None)
    state["blocked"] = blocked
    if _shared_on():
        shared_state.clear_blocked(label)
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


def pick_next(state: dict, exclude: str | None = None, now: float | None = None) -> str | None:
    """지금 쓸 계정 라벨 하나. 없으면 None.

    순차 소진: accounts.json 순서(A→B→C→D)의 **최저 인덱스**부터 고른다. 분산하지 않는다.
    막힌 계정만 건너뛰므로, A 가 살아 있으면 항상 A 를 쓴다. A 가 주간한도로 오래 막히면
    B 로 넘어가 B 를 태운다. 5시간 한도로 잠깐 막힌 계정은 곧 풀려 다시 최저 인덱스가 되므로
    자연히 오래 막히는 주간한도 기준으로 순서가 넘어간다.
    공유 저장소가 켜져 있으면(prune_blocked 가 병합) 세 머신이 같은 막힘을 보고 같은 계정을 고른다.
    """
    now = time.time() if now is None else now
    labels = account_labels()  # accounts.json 에 적힌 순서 그대로
    saved = set(saved_labels())
    if not labels:
        return None

    for label in labels:
        if label == exclude:
            continue
        if label not in saved:
            continue
        if is_blocked(state, label, now):
            continue
        return label
    return None


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
