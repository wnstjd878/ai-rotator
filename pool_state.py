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
      manual       : 사람이 --use 로 직접 고른 계정 (있으면 되돌리기가 건드리지 않는다)
      reclaim_at   : 마지막 되돌리기 시도 시각(epoch) — 잦은 왕복을 막는 간격 기준
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

# 로그인 거절을 이어서 몇 번 받았는지 세는 창(초). 이 시간 안에 다시 거절받으면 이어서 센다.
AUTH_REJECT_WINDOW = 600

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
    global SAVED_LABELS_FN
    layout = _LAYOUT.get(namespace)
    if not layout:
        return
    if namespace != NAMESPACE:
        SAVED_LABELS_FN = None  # 제공자가 바뀌면 남의 판단 기준을 물려받지 않는다
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
RECLAIM_MIN_GAP = 10 * 60  # 앞순위 계정으로 되돌리는 시도의 최소 간격(초)


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


def retired_labels() -> list[str]:
    """구독을 해지해 일부러 뺀 계정(accounts.json 의 "retired": true).

    creds_audit 는 점검에서 빼고, status_text 는 목록 표시에서 뺀다.
    재구독하면 retired 를 지우고 save_account.py 로 저장해 되살린다.
    """
    return [str(a["label"]).strip() for a in load_accounts() if a.get("retired")]


SAVED_LABELS_FN = None  # 제공자가 "쓸 수 있는 계정"을 다르게 판단할 때 끼우는 자리


def saved_labels() -> list[str]:
    """지금 쓸 수 있는 계정 라벨.

    클로드는 자격 저장본(creds/<라벨>.json)이 있는 라벨.
    지피티는 계정 폴더마다 로그인이 따로 살아 있어서 판단 기준이 다르다
    (codex_pool 이 SAVED_LABELS_FN 을 끼워 자기 방식으로 판단한다).
    """
    if SAVED_LABELS_FN is not None:
        try:
            return SAVED_LABELS_FN()
        except Exception:
            return []
    if not CREDS_DIR.exists():
        return []
    return sorted(f.stem for f in CREDS_DIR.glob("*.json") if f.stat().st_size > 0)


def usable_labels() -> list[str]:
    """지금 순환에 넣을 수 있는 계정. 저장본이 있고 구독을 해지하지 않은 것.

    **고르는 함수(pick_next/better_candidate)와 알리는 함수(earliest_reset)가 같은
    기준을 써야 한다.** 한쪽만 바꾸면 알림이 말한 계정과 실제로 고르는 계정이 어긋난다
    (2026-08-20 코덱스 검수에서 두 번 지적, 둘 다 재현됨). 그래서 기준을 여기 한 벌만 둔다.
    구독 해지(retired) 계정은 골라 봐야 거절만 받는다. 재구독하면 accounts.json 의
    retired 를 지우고 save_account.py 로 저장해 되살린다.
    """
    saved = set(saved_labels())
    retired = set(retired_labels())
    return [l for l in account_labels() if l in saved and l not in retired]


def _blank_state() -> dict:
    return {"current": None, "blocked": {}, "switched_at": None, "history": [],
            "notified_for": None, "notified_at": None,
            "manual": None, "reclaim_at": None, "auth_rejects": {},
            "scoped_blocked": {}}


def _clean_scoped(raw) -> dict:
    """모델 칸별 막힘을 {라벨: {칸이름: 해제시각}} 으로 다듬는다. 이상한 값은 버린다."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for label, buckets in raw.items():
        if not isinstance(buckets, dict):
            continue
        clean = {}
        for name, until in buckets.items():
            try:
                clean[str(name)] = float(until)
            except (TypeError, ValueError):
                continue
        if clean:
            out[str(label)] = clean
    return out


def mark_scoped_blocked(state: dict, label: str, bucket: str, until_epoch: float) -> dict:
    """그 계정의 **특정 모델 칸만** 막혔다고 적는다(계정 전체는 그대로 쓸 수 있다).

    계정 전체를 막는 mark_blocked 와 갈라 둔 이유: Fable 이 다 떨어져도 그 계정으로
    Opus 는 멀쩡히 쓸 수 있다. 여기서 계정을 통째로 막으면 남은 Opus 몫을 버리게 된다.
    """
    if not label or not bucket:
        return state
    try:
        until = float(until_epoch)
    except (TypeError, ValueError):
        return state
    box = _clean_scoped(state.get("scoped_blocked"))
    prev = box.get(str(label), {}).get(str(bucket))
    box.setdefault(str(label), {})[str(bucket)] = max(float(prev), until) if prev else until
    state["scoped_blocked"] = box
    _push_history(state, label, "scoped_blocked", f"{bucket} 칸 소진")
    if _shared_on():  # 세 머신이 같이 본다. 안 올리면 머신마다 첫 거절을 한 번씩 겪는다
        shared_state.set_scoped_blocked(label, bucket, until, provider=NAMESPACE)
    return state


def is_scoped_blocked(state: dict, label: str, bucket: str | None,
                      now: float | None = None) -> bool:
    """그 계정의 그 모델 칸이 아직 막혀 있는지. 칸을 안 주면 항상 False."""
    if not bucket:
        return False
    now = time.time() if now is None else now
    until = (_clean_scoped(state.get("scoped_blocked")).get(str(label)) or {}).get(str(bucket))
    return bool(until and until > now)


def scoped_blocked_buckets(state: dict, label: str, now: float | None = None) -> set:
    """그 계정에서 지금 막혀 있는 모델 칸 이름들. 없으면 빈 집합.

    알림 문구가 '다른 모델로 켜면 된다' 고 단정하기 전에 확인하는 데 쓴다.
    """
    now = time.time() if now is None else now
    box = _clean_scoped(state.get("scoped_blocked")).get(str(label)) or {}
    return {name for name, until in box.items() if until > now}


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
    if not isinstance(out.get("auth_rejects"), dict):
        out["auth_rejects"] = {}
    out["scoped_blocked"] = _clean_scoped(out.get("scoped_blocked"))
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
    # 모델 칸별 막힘도 같이 턴다. 안 털면 Fable 이 다시 채워져도 그 계정을 계속 건너뛴다.
    scoped = {}
    for label, buckets in _clean_scoped(state.get("scoped_blocked")).items():
        alive = {name: until for name, until in buckets.items() if until > now}
        if alive:
            scoped[label] = alive
    if _shared_on():
        # 공유 저장소의 칸 막힘도 병합한다(2026-08-14). 안 하면 다른 머신이 이미 겪은
        # 칸 소진을 이 머신이 또 겪는다(거절 1번 + 전환 알림 1건 낭비).
        remote = shared_state.get_scoped_blocked(now, provider=NAMESPACE)
        if remote is not None:  # 읽기 실패면 로컬만 유지
            known = set(account_labels())
            for label, buckets in remote.items():
                if label not in known:
                    continue  # 이 제공자 계정 목록에 없는 라벨은 안 받는다
                for name, until in buckets.items():
                    prev = scoped.get(label, {}).get(name)
                    scoped.setdefault(label, {})[name] = \
                        max(float(prev), float(until)) if prev else float(until)
    state["scoped_blocked"] = scoped
    if not blocked and not scoped:
        # 계정 막힘도 칸 막힘도 다 풀렸을 때만 지운다. 칸만 막힌 소진은 blocked 가
        # 늘 비어 있어서, 예전 조건이면 표시가 매번 지워져 같은 알림이 봇 호출마다
        # 나갔다(2026-08-20 코덱스 지적, 재현됨. 공유가 켜져 있으면 공유 쪽
        # claim_notify 가 막아줘서 실전에서는 안 터졌다).
        state["notified_for"] = None
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
    """한 계정의 막힘을 푼다(로컬 + 공유). 사람이 --use 로 고른 계정 등.

    모델 칸별 막힘도 같이 푼다 — 공유 쪽을 안 지우면 다음 병합(prune_blocked)이
    지운 값을 도로 살려낸다.
    """
    blocked = dict(state.get("blocked") or {})
    blocked.pop(label, None)
    state["blocked"] = blocked
    scoped = _clean_scoped(state.get("scoped_blocked"))
    scoped.pop(str(label), None)
    state["scoped_blocked"] = scoped
    if _shared_on():
        shared_state.clear_blocked(_to_shared(label))
        shared_state.clear_scoped_blocked(label, provider=NAMESPACE)
    return state


def note_auth_reject(state: dict, label: str | None, now: float | None = None,
                     window: float = AUTH_REJECT_WINDOW) -> tuple[int, float]:
    """로그인 거절을 계정별로 센다. (이어진 횟수, 처음 거절받은 뒤 흐른 초)를 돌려준다.

    횟수보다 흐른 시간이 중요하다. 자격 파일을 갈아끼우는 순간에 호출이 겹쳐 나는 거절은
    한 번의 호출 안에서 몇 번씩 연달아 나지만 몇 초면 풀린다. 반대로 자격이 진짜 못 쓰게
    되면 몇 분이 지나도 계속 거절받는다. 그래서 두 상황은 횟수로는 못 가르고 시간으로 갈린다.

    마지막 거절로부터 창(기본 10분)이 지나면 처음부터 다시 센다.
    호출이 성공하면 clear_auth_reject 로 지운다. 잠금 안에서 부를 것 — 상태를 바꾼다.
    """
    if not label:
        return 0, 0.0
    now = time.time() if now is None else now
    box = state.get("auth_rejects")
    if not isinstance(box, dict):
        box = {}
        state["auth_rejects"] = box
    rec = box.get(str(label))
    last, first, n = 0.0, 0.0, 0
    if isinstance(rec, dict):
        try:
            last = float(rec.get("last") or 0)
            first = float(rec.get("first") or 0)
            n = int(rec.get("n") or 0)
        except (TypeError, ValueError):
            last, first, n = 0.0, 0.0, 0
    if now - last > window or not first or first > now:
        n, first = 0, now
    n += 1
    box[str(label)] = {"n": n, "first": first, "last": now}
    return n, max(0.0, now - first)


def clear_auth_reject(state: dict, label: str | None = None) -> dict:
    """거절 기록을 지운다. 라벨을 안 주면 전부. 강제 교체 누적도 같이 지운다(성공=회복)."""
    box = state.get("auth_rejects")
    if not isinstance(box, dict):
        state["auth_rejects"] = {}
    elif label is None:
        state["auth_rejects"] = {}
    else:
        box.pop(str(label), None)
    forced = state.get("forced_rotates")
    if isinstance(forced, dict):
        if label is None:
            state["forced_rotates"] = {}
        else:
            forced.pop(str(label), None)
    return state


def note_forced_rotate(state: dict, label: str | None, now: float | None = None,
                       window: float = 24 * 3600) -> int:
    """로그인 거절로 강제 교체된 횟수를 계정별로 센다(창 안 누적, 이번 포함).

    1회는 일시 문제(자격 자리 어긋남 등)로 되비추기가 스스로 고칠 수 있어 짧게 쉬게만 하고,
    같은 계정이 창 안에서 또 걸리면 저장본이 진짜 죽은 것으로 보고 격리한다(호출부 판단).
    성공하면 clear_auth_reject 가 같이 지운다. 잠금 안에서 부를 것 — 상태를 바꾼다.
    """
    if not label:
        return 0
    now = time.time() if now is None else now
    box = state.get("forced_rotates")
    if not isinstance(box, dict):
        box = {}
        state["forced_rotates"] = box
    rec = box.get(str(label))
    times = [t for t in rec if isinstance(t, (int, float)) and now - t < window] \
        if isinstance(rec, list) else []
    times.append(now)
    box[str(label)] = times[-10:]
    return len(times)


def auth_reject_count(state: dict, label: str | None, now: float | None = None,
                      window: float = AUTH_REJECT_WINDOW) -> int:
    """지금까지 센 거절 횟수를 읽기만 한다(창이 지났으면 0)."""
    if not label:
        return 0
    now = time.time() if now is None else now
    rec = (state.get("auth_rejects") or {}).get(str(label))
    if not isinstance(rec, dict):
        return 0
    try:
        if now - float(rec.get("last") or 0) > window:
            return 0
        return int(rec.get("n") or 0)
    except (TypeError, ValueError):
        return 0


def set_current(state: dict, label: str, reason: str = "", manual: bool = False) -> dict:
    """지금 쓸 계정을 정한다.

    manual=True 는 사람이 --use 로 직접 고른 경우다. 그 표시가 남아 있는 동안은
    되돌리기(better_candidate)가 그 계정을 뒤집지 않는다. 자동 교체가 한 번이라도
    일어나면 표시가 지워져 다시 순서 규칙대로 돈다.
    """
    state["current"] = label
    state["switched_at"] = time.time()
    state["manual"] = label if manual else None
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


def note_usage(state: dict, label: str, usage, now: float | None = None) -> dict:
    """그 계정에서 마지막으로 읽은 사용량을 기억한다(보여주기 전용).

    지금 안 쓰는 계정은 저장본의 출입증(access token)이 8시간이라 대개 조회가 안 된다.
    자격이 죽은 게 아니라 못 읽는 것뿐이라, 마지막으로 본 값에 '언제 기준' 을 붙여
    보여주려고 남긴다. 계정을 고르거나 막는 판단에는 절대 쓰지 않는다(낡은 값이므로).
    """
    if not usage:
        return state
    now = time.time() if now is None else now
    seen = dict(state.get("usage_seen") or {})
    seen[label] = {
        "weekly_pct": usage.get("weekly_pct"),
        "session_pct": usage.get("session_pct"),
        "weekly_reset": usage.get("weekly_reset"),
        "session_reset": usage.get("session_reset"),
        # 모델 칸(Fable 등)도 같이 남긴다. 이걸 안 남기면 계정 목록 화면에서
        # "주간 40% 남음" 만 보여 그 계정으로 Fable 이 되는지 알 수가 없다.
        "scoped": usage.get("scoped") or {},
        "at": round(now),
    }
    state["usage_seen"] = seen
    return state


def remember_usage(pairs) -> None:
    """읽어둔 사용량과 초기화 시각을 한 번의 잠금으로 상태 파일에 반영한다.

    이미 잠금을 쥔 곳에서는 부르지 말 것(중첩). 거기서는 note_usage 를 직접 쓴다.
    """
    pairs = [(l, u) for l, u in pairs if u]
    if not pairs:
        return
    with with_lock():
        s = prune_blocked(load_state())
        for l, u in pairs:
            note_usage(s, l, u)
            note_weekly_reset(s, l, u.get("weekly_reset"))
        save_state(s)


def pick_next(state: dict, exclude: str | None = None, now: float | None = None,
              bucket: str | None = None) -> str | None:
    """지금 쓸 계정 라벨 하나. 없으면 None.

    `bucket` 을 주면 **그 모델 칸이 막힌 계정도 건너뛴다**. 쓰려는 게 Fable 인데
    Fable 만 바닥난 계정으로 갈아타면 갈아탄 의미가 없다. 칸을 안 주면 예전과 똑같이
    계정 단위 막힘만 본다(Opus 로 쓰는 경우가 그대로 유지되는 지점이다).

    순차 소진: 한 계정을 다 태우고 다음으로 간다(분산하지 않는다).
    순서는 **주간 초기화가 이른 계정부터**. 곧 초기화되는 계정의 남은 양은 안 쓰면 그냥
    사라지기 때문이다(2026-07-27 사용자 지적: 8/1 초기화인 A 보다 7/31 초기화인 B 를
    먼저 썼어야 했다). 초기화 시각을 아직 모르는 계정은 뒤로 밀고 accounts.json 순서를 따른다.
    막힌 계정은 건너뛴다. 5시간 한도로 잠깐 막힌 계정은 곧 풀려 다시 후보가 된다.
    공유 저장소가 켜져 있으면(prune_blocked 가 병합) 세 머신이 같은 막힘을 보고 같은 계정을 고른다.
    """
    now = time.time() if now is None else now
    candidates = [l for l in usable_labels()
                  if l != exclude and not is_blocked(state, label=l, now=now)
                  and not is_scoped_blocked(state, l, bucket, now)]
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


def better_candidate(state: dict, cur: str | None, now: float | None = None,
                     bucket: str | None = None):
    """지금 쓰는 계정보다 순서가 앞선 계정. 없으면 None.

    세션(5시간) 한도로 잠깐 비켜났던 앞순위 계정이 풀렸을 때 그쪽으로 되돌리기 위한 것.
    되돌리지 않으면 뒷순위 계정의 주간 잔여를 먼저 깎게 되고, 앞순위 계정의 남은 양은
    안 쓰인 채 초기화되어 사라진다(순차 소진의 목적과 반대).
    사람이 --use 로 고른 계정은 그 계정이 막힐 때까지 존중한다.
    `bucket` 은 이번에 쓰려는 모델 칸이다. 그 칸이 막힌 계정으로 되돌리면 다음 호출이
    곧바로 다시 넘기면서 전환 알림이 왕복마다 나간다(2026-08-14 실사고: Mac 이 A 의
    Fable 칸 소진을 무시하고 A 로 되돌렸다가 매번 D 로 재전환 + 알림 반복).
    """
    now = time.time() if now is None else now
    if not cur or state.get("manual") == cur:
        return None
    saved = set(saved_labels())
    if cur not in saved or is_blocked(state, label=cur, now=now):
        return None  # 지금 계정이 막혔으면 되돌리기가 아니라 일반 교체가 처리한다
    candidates = [l for l in usable_labels()
                  if not is_blocked(state, label=l, now=now)
                  and not is_scoped_blocked(state, l, bucket, now)]
    order = priority_order(state, candidates, now)
    if not order or order[0] == cur:
        return None
    return order[0]


def may_reclaim(state: dict, now: float | None = None) -> bool:
    """되돌리기를 시도해도 되는 때인지. 잦은 왕복과 헛걸음 반복을 막는다."""
    now = time.time() if now is None else now
    try:
        last = float(state.get("reclaim_at") or 0)
    except (TypeError, ValueError):
        last = 0.0
    return (now - last) >= RECLAIM_MIN_GAP


def note_reclaim(state: dict, now: float | None = None) -> dict:
    """되돌리기를 시도했다고 기록한다. 교체가 실패해도 기록한다(재시도 폭주 방지)."""
    state["reclaim_at"] = time.time() if now is None else now
    return state


def earliest_reset(state: dict, bucket: str | None = None) -> float | None:
    """막힌 계정이 가장 빨리 다시 쓸 수 있게 되는 시각(epoch). 없으면 None.

    `bucket` 을 주면 **그 모델 칸이 풀리는 시각까지 같이 본다**. 계정 단위 막힘만 보면
    Fable 칸이 세 계정 다 바닥인데도 5시간 한도가 풀리는 시각이 찍혀 13시간이나 이른
    시각을 알린다(2026-08-20 사용자 지적: 알림엔 08/21 03:51, 실제로 Fable 이 풀리는
    가장 이른 시각은 08/21 16:00). 한 계정을 그 모델로 다시 쓰려면 계정 막힘과 그 칸
    막힘이 **둘 다** 풀려야 하므로 계정마다 늦은 쪽을 취하고, 그중 가장 이른 것을 고른다.
    저장본이 없거나 구독을 해지한 계정은 후보에서 뺀다 — 영영 안 돌아오는 계정의 시각을
    '가장 빨리 풀림' 으로 알리면 안 된다.
    """
    blocked = state.get("blocked") or {}

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    if not bucket:
        vals = [_num(v) for v in blocked.values()]
        vals = [v for v in vals if v > 0]
        return min(vals) if vals else None

    scoped = _clean_scoped(state.get("scoped_blocked"))
    # 후보 기준은 실제로 계정을 고르는 pick_next 와 **한 벌을 같이 쓴다**(usable_labels).
    times = []
    for label in usable_labels():
        until = max(_num(blocked.get(label)), _num((scoped.get(label) or {}).get(bucket)))
        if until > 0:
            times.append(until)
    if times:
        return min(times)
    # 쓸 수 있는 계정이 하나도 없는 드문 경우(저장본이 전부 빠짐)엔 예전 방식으로 떨어진다
    vals = [_num(v) for v in blocked.values()]
    vals = [v for v in vals if v > 0]
    return min(vals) if vals else None


def note_scoped_full(state: dict, now: float | None = None) -> list:
    """그 모델 칸으로 쓸 계정이 하나도 없는 상태를 적어두고, **다시 열린 칸**을 알려준다.

    돌려주는 값: [(칸 이름, 직전에 적어둔 해제 시각)] — 직전 회차엔 전부 바닥이었는데
    이번 회차에 쓸 계정이 생긴 칸들. 주기 작업(usage_alert)이 이걸 보고 한 번 알린다.
    사용자 요청 2026-08-21: 소진 알림만 오고, 언제 다시 쓸 수 있는지는 사람이 시각을
    외워두고 기다려야 했다.

    **그 칸 때문에 막힌 계정이 있을 때만 바닥으로 센다.** 계정이 통째로 막혀서 후보가
    없는 것까지 칸 소진으로 세면, 계정이 풀릴 때마다 안 막혔던 칸이 열렸다고 알린다.
    """
    now = time.time() if now is None else now
    prev = state.get("scoped_full") or {}
    scoped = _clean_scoped(state.get("scoped_blocked"))
    buckets = set(prev) | {b for box in scoped.values() for b in box}
    usable = usable_labels()
    fresh, opened = {}, []
    for bucket in sorted(buckets):
        by_bucket = any(is_scoped_blocked(state, l, bucket, now) for l in usable)
        if by_bucket and pick_next(state, bucket=bucket, now=now) is None:
            fresh[bucket] = float(earliest_reset(state, bucket=bucket) or 0.0)
        elif bucket in prev:
            try:
                opened.append((bucket, float(prev.get(bucket) or 0.0)))
            except (TypeError, ValueError):
                opened.append((bucket, 0.0))
    state["scoped_full"] = fresh
    return opened


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
