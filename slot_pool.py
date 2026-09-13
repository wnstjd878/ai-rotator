# style-guard:off
"""
== 운영 맥락 ==
용도: 사람 창을 **모델별 자리(slot)** 로 나눠 창마다 다른 계정을 쓰게 한다(2026-09-12).
      그동안은 이 PC 의 모든 창이 자격 파일 한 벌(~/.claude/.credentials.json)을 같이 읽어
      계정이 PC 단위로 하나였다. Opus 창을 위해 A 로 바꾸면 Fable 창도 A 로 끌려갔고, 두
      모델을 다 만족하는 계정이 없는 날은 창 지키기가 4시간 넘게 아무것도 못 했다.
      사용자 결정: "Opus 로 쓰면 Opus 남은 계정, Fable 로 쓰면 Fable 남은 계정을 찾아라."
방식: claude 는 설정 폴더를 CLAUDE_CONFIG_DIR 로 바꿀 수 있고 자격 파일은 그 폴더 것을 읽는다.
      자리마다 폴더 ~/.claude-pool/<칸>/ 을 두고, 그 안의 .credentials.json 만 진짜 파일이며
      나머지 폴더(projects·sessions·hooks…)는 ~/.claude 로 가는 연결(junction), 설정 파일은
      사본(원본이 새로워지면 다시 복사)이다. cc 가 켜지는 창은 그 모델의 자리 폴더를 물려받는다.
자격: **자리마다 계정별로 따로 로그인한 자격**을 쓴다(creds/slots/<칸>/<라벨>.json).
      같은 자격을 두 곳에 복사하면 한쪽이 갱신할 때 재발급 열쇠가 바뀌어 다른 쪽이 죽는다
      (실측 2026-09-12: claude 가 자격 파일을 임시파일+바꿔치기로 써서 hardlink 도 끊긴다).
      로그인은 `python relogin.py --slot <칸> <라벨>` (전용 크롬으로 자동 승인).
동작: 자리 폴더의 자격이 그 칸에서 97%(창 지키기) 또는 100%(켜기 전) 이면 그 칸이 남은 다른
      계정 자격으로 갈아끼운다. 갈아끼우기 전에 갱신분을 저장본에 되저장한다. 다른 자리·봇은
      건드리지 않는다. Mac 은 이 기능이 없다(enabled() 가 False → 전부 예전 방식).
되돌리기(2026-09-12 사용자 요구): cc 로 켤 때 지금 자리 계정이 멀쩡해도 **주간 초기화가 더 이른
      계정**이 그 칸을 아직 쓸 수 있으면 그쪽으로 옮긴다. 곧 초기화되는 계정의 남은 양은 안 쓰면
      그냥 사라지므로 그것부터 태운다(공용 자격의 _reclaim_if_better 와 같은 원칙). 켤 때만 한다.
      창이 끝난 뒤 확인(--slot-check)·창 지키기(tend)·한도로 끝난 뒤(--slot-next)에서는 안 한다.
자리 창의 모델(2026-09-13): 자리 계정은 **자리 칸 + 그 자리 창들이 지금 쓰는 칸**을 다 채워야 한다
      (slot_buckets). Fable 로 켜서 창 안에서 Opus 로 바꾼 창은 Fable 자리에 남는데, 창 지키기가
      Fable 칸만 보고 "Fable 이 남은 계정이 없다" 며 A 의 5시간 한도에 막힌 Opus 창 둘을 3시간 넘게
      그대로 뒀다(D 는 Opus 가 넉넉했다). 어느 계정에서도 못 쓰는 칸은 조건에서 뺀다(_dead,
      claude_pool._dead_everywhere 와 같은 기준). 종료코드 3 은 **자리 칸이** 바닥나 갈아탔을 때만이다.
의도적 미구현: 창 안에서 /model 로 모델을 바꾼 경우의 자리 이동(자리는 켤 때 정해진다.
      바꾼 모델은 그 자리 계정의 그 칸을 쓰고, 자리 계정 고르기가 그 칸도 따진다).
      자리 폴더 안 history.jsonl 은 사본이라 갈라진다.
마지막 점검: 2026-09-13
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import limit_detect  # noqa: E402
import pool_state as st  # noqa: E402
import switch_account as sa  # noqa: E402

try:
    import usage_check
except Exception:  # pragma: no cover
    usage_check = None

CLAUDE_HOME = Path.home() / ".claude"
POOL_ROOT = Path.home() / ".claude-pool"
STORE_ROOT = BASE / "creds" / "slots"
LOG_FILE = BASE / "rotator.log"
BUCKETS = ("Opus", "Fable", "Sonnet", "Haiku")
# 원본이 새로워지면 자리 폴더로 다시 복사하는 설정 파일들
COPY_FILES = ("settings.json", "settings.local.json", "CLAUDE.md", "keybindings.json")
# 처음 한 번만 심는 파일(그 뒤로는 자리마다 따로 자란다)
SEED_ONCE = ("history.jsonl",)
CRED_NAME = ".credentials.json"


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def enabled() -> bool:
    """이 기능이 도는 머신인가. 콘솔 입력·junction 이 있는 Windows 만."""
    return os.name == "nt" and not os.environ.get("AIROTATOR_NO_SLOTS")


# ── 경로 ─────────────────────────────────────────────────────────────────────

def slot_dir(bucket: str) -> Path:
    return POOL_ROOT / str(bucket)


def live_path(bucket: str) -> Path:
    return slot_dir(bucket) / CRED_NAME


def store_path(bucket: str, label: str) -> Path:
    return STORE_ROOT / str(bucket) / f"{label}.json"


def account_path(bucket: str, label: str) -> Path:
    """로그인 때 claude 가 적어둔 계정 정보(oauthAccount)의 사본. 자격은 안 담긴다."""
    return STORE_ROOT / str(bucket) / f"{label}.account.json"


def stored_labels(bucket: str) -> list:
    """그 자리에 쓸 자격이 저장된 계정(순환 대상이면서 저장본이 있는 것)."""
    return [l for l in st.usable_labels() if store_path(bucket, l).exists()]


def slot_of_dir(path) -> str | None:
    """CLAUDE_CONFIG_DIR 값 → 자리(칸) 이름. 자리 폴더가 아니면 None."""
    if not path:
        return None
    try:
        p = Path(path).resolve()
    except Exception:
        return None
    for b in BUCKETS:
        try:
            if p == slot_dir(b).resolve():
                return b
        except Exception:
            continue
    return None


def slot_of_env() -> str | None:
    return slot_of_dir(os.environ.get("CLAUDE_CONFIG_DIR"))


# ── 자리 폴더 ──────────────────────────────────────────────────────────────────

def _junction(dst: Path, src: Path) -> bool:
    if dst.exists():
        return True
    try:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                           capture_output=True, timeout=20,   # 출력은 안 읽는다(콘솔 글자표가 달라 깨진다)
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:
        return False


def ensure_dir(bucket: str) -> Path:
    """자리 폴더를 만들거나 최신으로 맞춘다. 자격 파일은 여기서 건드리지 않는다.

    - ~/.claude 의 모든 하위 폴더 → 같은 이름의 연결(junction). 기록·메모리·훅·등록 창 파일이
      한 곳에 남아야 --continue 와 이어가기(sessions/<번호>.json)가 자리와 무관하게 돈다.
    - 설정 파일은 원본이 더 새로우면 복사한다(자리에서 바꾼 값은 다음 원본 변경 때 덮인다).
    - ~/.claude.json 은 설정 폴더 안으로 따라오는 파일이라 처음 한 번 심는다.
    """
    d = slot_dir(bucket)
    d.mkdir(parents=True, exist_ok=True)
    for src in CLAUDE_HOME.iterdir():
        if src.is_dir() and not src.name.startswith("."):
            _junction(d / src.name, src)
    for name in COPY_FILES:
        src, dst = CLAUDE_HOME / name, d / name
        if not src.exists():
            continue
        try:
            if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime + 1:
                shutil.copy2(src, dst)
        except OSError:
            pass
    for name in SEED_ONCE:
        src, dst = CLAUDE_HOME / name, d / name
        if src.exists() and not dst.exists():
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass
    root_cfg, dst = Path.home() / ".claude.json", d / ".claude.json"
    if root_cfg.exists() and not dst.exists():
        try:
            shutil.copy2(root_cfg, dst)
        except OSError:
            pass
    return d


# ── 상태 ─────────────────────────────────────────────────────────────────────

def _slots(state: dict) -> dict:
    box = state.get("slots")
    if not isinstance(box, dict):
        box = {}
        state["slots"] = box
    return box


def slot_current(state: dict, bucket: str) -> str | None:
    cur = (_slots(state).get(bucket) or {}).get("current")
    return str(cur) if cur else None


def slot_claims(state: dict, bucket: str, now: float | None = None) -> list:
    """그 자리에서 떠 있는 창들의 프로세스 번호."""
    now = time.time() if now is None else now
    out = []
    for key, item in st._clean_claims(state.get("session_claims")).items():
        if item.get("slot") != bucket:
            continue
        if now - item["at"] > st.CLAIM_MAX_AGE or not st._pid_alive(key):
            continue
        if st._same_process(key, item.get("started")) is False:
            continue
        out.append(key)
    return _has_window(out)


def _has_window(keys: list, procs: dict | None = None) -> list:
    """표시 번호 중 지금 claude 창이 딸린 것만. 프로세스 목록을 못 읽으면 그대로 돌려준다.

    cc 는 창 표시를 셸 번호로 적는다. 창이 끝날 때 표시 지우기(--unclaim)가 잠금 대기로 실패하면
    셸이 살아 있는 동안 표시가 남고, 정리하는 곳이 그 시각을 계속 새로 찍어 준다(2026-09-13 검수 지적).
    그 유령 표시의 칸이 자리 계정 고르기를 좁히지 않게, 번호가 claude 자체이거나 claude 자식을 둔 셸일 때만 센다.
    """
    if not keys or (procs is None and os.name != "nt"):
        return keys
    if procs is None:
        try:
            import session_nudge as sn
            procs = sn._process_table()
        except Exception:
            return keys
    shells = {pp for pp, exe in procs.values() if exe == "claude.exe"}
    return [k for k in keys
            if int(k) in shells or (procs.get(int(k)) or (0, ""))[1] == "claude.exe"]


def slot_buckets(state: dict, bucket: str, now: float | None = None) -> tuple:
    """그 자리 계정이 채워 줘야 하는 모델 칸: 자리 칸 + 그 자리 창들이 지금 쓰는 칸.

    자리는 켤 때 모델로 정해지고 창 안에서 /model 로 바꿔도 안 옮긴다. 그래서 Fable 자리에 Opus 로
    바꾼 창이 앉아 있을 수 있다. 창의 칸은 상태줄이 /model 변경 때 표시를 갱신해 둔다.
    """
    names = [str(bucket)]
    claims = st._clean_claims(state.get("session_claims"))
    for key in slot_claims(state, bucket, now):
        name = (claims.get(key) or {}).get("bucket")
        if name and name not in names:
            names.append(name)
    return tuple(names)


# ── 자격 ─────────────────────────────────────────────────────────────────────

def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def sync_creds(bucket: str, label: str | None) -> str:
    """자리의 살아있는 자격과 저장본을 같은 값(더 새 것)으로 맞춘다. 기록용 한 줄.

    claude 가 자리 안에서 자격을 갱신하면 저장본은 낡는다(되저장). 반대로 순환기가 저장본을
    갱신(401 뒤 재발급)했으면 자리로 밀어 넣는다 — 떠 있는 창은 파일이 바뀌면 다시 읽는다.
    """
    if not label:
        return "자리 계정을 몰라 맞추지 않음"
    live, store = live_path(bucket), store_path(bucket, label)
    lv, sv = _load(live), _load(store)
    if lv is None and sv is None:
        return "둘 다 없음"
    if lv is not None and sa._obj_fingerprint(lv) == sa._obj_fingerprint(sv) \
            and sa._issued_at(lv) == sa._issued_at(sv):
        return f"'{label}' 변화 없음"
    # 저장본이 다른 라벨의 저장본과 같은 자격이면 라벨이 섞인 것이다 — 덮어쓰지 않는다
    if lv is not None:
        for other in stored_labels(bucket):
            if other != label and sa._obj_fingerprint(_load(store_path(bucket, other))) == sa._obj_fingerprint(lv):
                return f"자리 자격이 '{label}' 이 아니라 '{other}' 저장본과 같아 되저장 안 함"
    if sv is None or (lv is not None and sa._issued_at(lv) >= sa._issued_at(sv)):
        prev = store.parent / "prev"
        prev.mkdir(parents=True, exist_ok=True)
        if store.exists():
            shutil.copy2(store, prev / store.name)
        sa._write_owner_only(store, lv)
        return f"'{label}' 갱신분을 저장본에 되저장"
    _put_live(bucket, sv)
    return f"'{label}' 저장본이 더 새로워 자리에 밀어 넣음"


def _put_live(bucket: str, obj) -> None:
    """자리의 자격 파일을 한 번에 바꿔 끼운다(떠 있는 창이 반쯤 쓰인 파일을 읽지 않게)."""
    d = slot_dir(bucket)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (".credentials.swap")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(obj))
    os.replace(tmp, live_path(bucket))


def _patch_account(bucket: str, label: str) -> None:
    """자리 폴더의 .claude.json 에 적힌 계정 정보를 그 라벨 것으로 맞춘다(표시용, 실패해도 무방)."""
    info = _load(account_path(bucket, label))
    cfg_path = slot_dir(bucket) / ".claude.json"
    cfg = _load(cfg_path)
    if not isinstance(info, dict) or not isinstance(cfg, dict):
        return
    cfg["oauthAccount"] = info
    try:
        tmp = cfg_path.with_suffix(".json.swap")
        tmp.write_text(json.dumps(cfg), encoding="utf-8")
        os.replace(tmp, cfg_path)
    except OSError:
        pass


def switch(state: dict, bucket: str, label: str, reason: str = "") -> bool:
    """자리의 계정을 바꾼다(잠금 안에서 부른다). 성공하면 True."""
    src = store_path(bucket, label)
    obj = _load(src)
    if not isinstance(obj, dict) or not (obj.get("claudeAiOauth") or {}).get("refreshToken"):
        log(f"자리 {bucket}: '{label}' 저장본이 없거나 깨져 갈아끼우지 못함")
        return False
    leaving = slot_current(state, bucket)
    if leaving and leaving != label:
        log(f"자리 {bucket}: [save-back] {sync_creds(bucket, leaving)}")
    ensure_dir(bucket)
    _put_live(bucket, obj)
    _patch_account(bucket, label)
    _slots(state)[bucket] = {"current": label, "since": time.time()}
    st._push_history(state, label, f"slot:{bucket}", reason or f"{leaving} → {label}")
    log(f"자리 {bucket}: '{leaving or '없음'}' → '{label}' ({reason or '교체'})")
    return True


# ── 고르기·확인 ───────────────────────────────────────────────────────────────

def pick(state: dict, bucket: str, exclude: str | None = None, now: float | None = None,
         need=None) -> str | None:
    """그 자리에 앉힐 계정. 저장본이 있고, 계정도 `need` 칸(안 주면 자리 칸)도 안 막힌 것 중 앞순위."""
    now = time.time() if now is None else now
    need = tuple(need or (bucket,))
    cands = [l for l in stored_labels(bucket)
             if l != exclude and not st.is_blocked(state, label=l, now=now)
             and not st.is_scoped_blocked(state, l, need, now)]
    order = st.priority_order(state, cands, now)
    return order[0] if order else None


def _dead(state: dict, bucket: str, names, now: float | None = None) -> set:
    """그 칸들 중 이 자리 저장본 계정 **전부**에서 기록상 막힌 칸. 조회 없이 기록만 본다.

    그 칸은 어느 계정으로 가도 못 쓰니 고르는 조건에서 뺀다. 빼지 않으면 Fable 이 네 계정 모두
    바닥난 날 Fable 자리의 Opus 창까지 살릴 계정을 못 찾는다(2026-09-13). 지금 계정만 그 칸을
    쓸 수 있으면 막힌 게 아니므로 빠지지 않는다. claude_pool._dead_everywhere 와 같은 기준이다.
    """
    now = time.time() if now is None else now
    labels = [l for l in stored_labels(bucket) if not st.is_blocked(state, label=l, now=now)]
    return {n for n in names if all(st.is_scoped_blocked(state, l, n, now) for l in labels)}


def _need(state: dict, bucket: str, wanted) -> tuple:
    """자리 계정이 채워야 하는 칸 중 어딘가에서는 쓸 수 있는 칸(순서 유지)."""
    dead = _dead(state, bucket, wanted)
    return tuple(n for n in wanted if n not in dead)


def _revive(state: dict, bucket: str, below: float, need=None) -> list:
    """저장본이 있는데 기록상 막힌 계정 중 실제로는 `need` 칸(안 주면 자리 칸)이 다 열린 것을 풀어 준다.

    계정마다 사용량은 한 번만 읽는다(조회가 429 로 막히는 날이 잦다).
    """
    if usage_check is None:
        return []
    need = tuple(need or (bucket,))
    freed = []
    now = time.time()
    for l in stored_labels(bucket):
        if not (st.is_blocked(state, label=l, now=now) or st.is_scoped_blocked(state, l, need, now)):
            continue
        u = usage_check.get_usage_for(store_path(bucket, l), is_current=False)
        if not u:
            continue
        if any(usage_check.which_limit(u, threshold=below, bucket=n) for n in need):
            continue
        box = dict(state.get("scoped_blocked") or {})
        if l in box and any(n in (box.get(l) or {}) for n in need):
            box[l] = {k: v for k, v in box[l].items() if k not in need}
            state["scoped_blocked"] = box
        blocked = dict(state.get("blocked") or {})
        blocked.pop(l, None)
        state["blocked"] = blocked
        freed.append(l)
        log(f"자리 {bucket}: '{l}' 는 실제로 열려 있어 막힘을 풉니다({', '.join(need)})")
    return freed


ENSURE_SWITCHED = 3
# 되돌릴 계정의 그 칸 사용량 상한. claude_pool.PREEMPT_PCT(97) 와 같아야 한다. 이보다 높은
# 계정으로 되돌리면 창 지키기가 3분 안에 도로 밀어내 켤 때마다 왕복한다.
RECLAIM_MAX_PCT = 97.0


def _exhausted_kinds(state: dict, cur: str, bucket: str, threshold: float, names=None) -> tuple:
    """자리 계정의 사용량을 한 번 읽어 칸마다 바닥났는지. ({칸: 종류}, 읽었는지). 바닥난 칸만 담는다."""
    names = tuple(names or (bucket,))
    u = usage_check.get_usage_for(store_path(bucket, cur), is_current=False)
    if not u:
        # 못 읽었다. 기록상 막혀 있으면 기록으로 갈아탄다(2026-09-12 사고와 같은 갈래).
        if st.is_blocked(state, cur):
            return {n: "blocked" for n in names}, False
        got = {n: "model:" + n for n in names if st.is_scoped_blocked(state, cur, n)}
        if not got:
            log(f"자리 {bucket}: '{cur}' 사용량을 못 읽어 그대로 둔다"
                f"(사유={getattr(usage_check, 'last_error', None)})")
        return got, False
    st.note_weekly_reset(state, cur, u.get("weekly_reset"))
    st.note_usage(state, cur, u)
    out = {}
    for n in names:
        kind = usage_check.which_limit(u, threshold=threshold, bucket=n)
        if kind:
            _mark_exhausted(state, cur, n if kind.startswith("model:") else bucket, kind, u)
            out[n] = kind
    return out, True


def _mark_exhausted(state: dict, label: str, bucket: str, kind, u: dict) -> None:
    """읽은 사용량이 한도라면 그 종류대로 기록에 막아 둔다(이미 막혀 있으면 그대로)."""
    if not kind or (st.is_scoped_blocked(state, label, bucket) if kind.startswith("model:")
                    else st.is_blocked(state, label)):
        return
    if kind.startswith("model:"):
        until = usage_check.scoped_reset(u, bucket)
        st.mark_scoped_blocked(state, label, bucket,
                               until or (time.time() + limit_detect.DEFAULT_COOLDOWN))
    else:
        until = u.get("weekly_reset") if kind == "weekly" else u.get("session_reset")
        st.mark_blocked(state, label, until or (time.time() + limit_detect.DEFAULT_COOLDOWN),
                        f"{'주간' if kind == 'weekly' else '세션(5시간)'} 한도 소진(자리 {bucket})")


def _earlier_than(state: dict, bucket: str, cur: str, now: float | None = None, need=None) -> list:
    """지금 자리 계정보다 주간 초기화가 이른 계정들(이른 순). 기록상 막힌 것은 뺀다.

    순서는 priority_order 한 벌을 그대로 쓴다(초기화가 이른 것부터, 시각을 모르면 뒤로).
    지금 계정이 기록상 막혀 있으면 되돌리기가 아니라 일반 교체가 처리하므로 빈 목록.
    `need` 는 채울 칸(안 주면 자리 칸). 그 칸 중 하나라도 막힌 계정은 뺀다.
    """
    now = time.time() if now is None else now
    need = tuple(need or (bucket,))
    cands = [l for l in stored_labels(bucket)
             if not st.is_blocked(state, label=l, now=now)
             and not st.is_scoped_blocked(state, l, need, now)]
    order = st.priority_order(state, cands, now)
    if cur not in order:
        return []
    return order[:order.index(cur)]


def _reclaim(state: dict, bucket: str, cur: str, need=None) -> str | None:
    """켤 때: 초기화가 이른 계정이 채울 칸을 아직 다 쓸 수 있으면 그쪽으로 옮긴다. 옮긴 라벨 또는 None.

    기록만 믿고 옮기지 않는다. 후보의 사용량을 실제로 읽어 채울 칸(`need`, 안 주면 자리 칸)이 전부
    RECLAIM_MAX_PCT 미만일 때만 옮긴다(막힌 걸 모르고 옮기면 첫 요청부터 거절받고 다시 갈아탄다.
    자리의 다른 창 칸을 빼먹으면 창 지키기가 3분 안에 도로 밀어내 켤 때마다 왕복한다). 읽은 값이
    한도면 기록에 막아 두어 다음 켤 때는 안 읽는다. 못 읽은 후보(429 등)는 건너뛰고 다음 후보를 본다.
    """
    need = tuple(need or (bucket,))
    for cand in _earlier_than(state, bucket, cur, need=need):
        u = usage_check.get_usage_for(store_path(bucket, cand), is_current=False)
        if not u:
            log(f"자리 {bucket}: 초기화가 이른 '{cand}' 사용량을 못 읽어 되돌리지 않는다"
                f"(사유={getattr(usage_check, 'last_error', None)})")
            continue
        st.note_weekly_reset(state, cand, u.get("weekly_reset"))
        st.note_usage(state, cand, u)
        bad = [(n, k) for n in need
               for k in [usage_check.which_limit(u, threshold=RECLAIM_MAX_PCT, bucket=n)] if k]
        if bad:
            for n, kind in bad:
                _mark_exhausted(state, cand, n if kind.startswith("model:") else bucket, kind, u)
            continue
        # 방금 읽은 초기화 시각으로 다시 견줘 본다(기록이 낡아 앞섰던 것일 수 있다)
        if st.priority_order(state, [cand, cur])[0] != cand:
            continue
        if switch(state, bucket, cand, "주간 초기화가 이른 계정으로 되돌림(켜기 전)"):
            return cand
    return None


def _pick_or_revive(state: dict, bucket: str, exclude: str | None, threshold: float, need=None):
    nxt = pick(state, bucket, exclude=exclude, need=need)
    if nxt is not None:
        return nxt
    box = _slots(state).setdefault(bucket, {})
    if time.time() - float(box.get("revive_at") or 0) >= 1800:
        box["revive_at"] = time.time()
        if _revive(state, bucket, threshold, need):
            nxt = pick(state, bucket, exclude=exclude, need=need)
    return nxt


def ensure(bucket: str, threshold: float = 100.0, force_next: bool = False,
           reclaim: bool = False) -> tuple:
    """자리의 계정이 바닥났으면 갈아끼운다. (종료코드, 지금 계정). (style-guard:off, 자리=slot 코드 용어)

    종료코드: 0 그대로(다른 창의 칸 때문에만 갈아탄 경우 포함) / 3 자리 칸이 바닥나 갈아탔음 /
    2 자리 칸이 바닥났는데 갈아탈 데 없음 / 1 교체 실패.
    `threshold` 97 은 창 지키기(거절받기 전에 미리), 100 은 켜기 전 확인.
    `force_next` 면 지금 계정을 두고 무조건 다른 계정으로(창이 한도로 끝난 뒤 되켤 때).
    `reclaim` 이면(켤 때만) 지금 계정이 멀쩡해도 주간 초기화가 더 이른 계정이 채울 칸을 다 쓸 수
    있으면 그쪽으로 옮긴다(_reclaim). 이때 종료코드는 0 이다.
    채울 칸 = 자리 칸 + 그 자리 창들이 쓰는 칸(slot_buckets) 중 어딘가에서는 쓸 수 있는 칸(_need).
    3 을 자리 칸이 바닥났을 때만 내는 이유: cc 는 창이 끝난 뒤(--slot-check) 3 을 **그 창의** 한도로
    보고 --continue 로 다시 켠다. 다른 창의 칸 때문에 갈아탄 걸 3 으로 알리면 사람이 닫은 창이 되살아난다.
    다 채울 계정이 없으면 자리 칸이 바닥났을 때만 자리 칸만 보고 고른다. 자리 칸이 멀쩡하면 그대로
    둔다(자리 칸 창을 밀면 다음 회차가 되밀어 왕복한다. 공용 계정의 strict 와 같은 규율).
    갈아탄 계정도 바닥일 수 있으므로 쓸 만한 계정이 나올 때까지 이어서 본다(--ensure 와 같은 규율).
    """
    if not enabled() or usage_check is None:
        return 0, None
    if not stored_labels(bucket):
        return 2, None
    switched = own_switched = own_dead = False
    entry_hit = None   # 들어올 때 앉아 있던 계정이 자리 칸에서 바닥났나(3 을 낼지는 이것만 본다)
    with st.with_lock():
        state = st.prune_blocked(st.load_state())
        wanted = slot_buckets(state, bucket)
        cur = slot_current(state, bucket)
        if cur and cur not in stored_labels(bucket):
            cur = None
        if cur is None:
            nxt = _pick_or_revive(state, bucket, None, threshold, _need(state, bucket, wanted) or (bucket,))
            if not nxt or not switch(state, bucket, nxt, "자리 처음 앉힘"):
                st.save_state(state)
                return (2 if not nxt else 1), None
            cur, switched, own_switched = nxt, True, True
        for _ in range(len(stored_labels(bucket)) + 1):
            note = sync_creds(bucket, cur)
            if "되저장" in note or "밀어 넣음" in note:
                log(f"자리 {bucket}: {note}")
            forced, bad = force_next, {}
            if force_next:
                force_next = False
            else:
                bad, read = _exhausted_kinds(state, cur, bucket, threshold, wanted)
                if read:
                    sync_creds(bucket, cur)   # 저장본을 재발급했을 수 있다(401 뒤). 자리로 밀어 넣는다
            need = _need(state, bucket, wanted)   # 방금 막은 기록까지 반영해 다시 계산한다
            own_hit = forced or bucket in bad
            if own_hit and bucket not in need and _pick_or_revive(state, bucket, cur, threshold, (bucket,)):
                # 기록상 어디서도 못 쓰는 자리 칸도 기록이 낡았을 수 있다. 30분에 한 번 실제 사용량을 읽어
                # 풀린 계정이 있으면 다시 계산한다(옛 코드가 하던 복구. 검수 재현: 기록만 보고 A 에 눌러앉음)
                need = _need(state, bucket, wanted)
            own_dead = own_hit and bucket not in need   # 자리 칸이 바닥인데 어느 계정에서도 못 쓴다
            if entry_hit is None:
                # 기록이 낡아 잘못 고른 계정을 다시 옮긴 것은 3 으로 안 센다. 그걸 세면 멀쩡히 닫은 창을
                # cc 가 한도로 보고 되켠다(시험 [58](15) 에서 재현: A 멀쩡 → C(기록 낡음) → B 가 3 으로 나옴).
                entry_hit = own_hit and not own_dead
            hit = [n for n in need if n in bad]
            if not forced and not hit:
                break
            nxt = _pick_or_revive(state, bucket, cur, threshold, need) if need else None
            if nxt is None and own_hit and bucket in need and len(need) > 1:
                nxt = _pick_or_revive(state, bucket, cur, threshold, (bucket,))
                if nxt:
                    log(f"자리 {bucket}: 창들이 쓰는 칸({', '.join(need)})을 다 채울 계정이 없어 "
                        f"{bucket} 칸만 보고 '{nxt}' 로 갑니다")
            if nxt is None:
                st.save_state(state)
                if own_hit:
                    log(f"자리 {bucket}: '{cur}' 의 {bucket} 칸이 바닥났지만 갈아탈 계정이 없다")
                    return 2, cur
                log(f"자리 {bucket}: 이 자리 창들이 쓰는 {', '.join(hit)} 칸이 '{cur}' 에서 바닥났지만 "
                    f"{bucket} 칸까지 채울 계정이 없어 그대로 둔다")
                return (ENSURE_SWITCHED if own_switched else 0), cur
            why = "미리 넘김" if threshold < 100.0 else ("한도로 끝난 뒤" if forced else "켜기 전 확인")
            what = f"{bucket} 칸 소진" if own_hit else f"이 자리 창들이 쓰는 {', '.join(hit)} 칸 소진"
            if not switch(state, bucket, nxt, f"{what}, {why}"):
                st.save_state(state)
                return 1, cur
            cur, switched = nxt, True
            if entry_hit:
                own_switched = True
        if own_dead:
            st.save_state(state)
            log(f"자리 {bucket}: '{cur}' 의 {bucket} 칸이 바닥났지만 갈아탈 계정이 없다")
            return 2, cur
        if reclaim and not switched and cur:
            need = _need(state, bucket, wanted)
            got = _reclaim(state, bucket, cur, need) if need else None
            if got:
                cur = got
        st.save_state(state)
    return (ENSURE_SWITCHED if own_switched else 0), cur


def prepare(bucket: str | None, reclaim: bool = True) -> tuple:
    """cc 가 켜기 직전에 부른다. (종료코드, 자리 폴더 경로 또는 None).

    저장본이 하나도 없는 칸이면 None — cc 는 예전 방식(공용 자격)으로 켠다.
    `reclaim=False`(--slot-check) 는 창이 끝난 뒤 "바닥났나" 만 보는 길이다. 거기서 되돌리면
    사람이 창을 닫은 직후에 헛 교체가 생긴다.
    """
    if not enabled() or not bucket or not stored_labels(bucket):
        return 0, None
    rc, cur = ensure(bucket, threshold=100.0, reclaim=reclaim)
    if cur is None:
        return rc, None
    ensure_dir(bucket)
    return rc, str(slot_dir(bucket))


def tend(threshold: float) -> list:
    """창 지키기: 떠 있는 창이 있는 자리마다 확인. 갈아탄 자리 이름 목록."""
    if not enabled():
        return []
    try:
        state = st.load_state()
    except Exception:
        return []
    switched = []
    for b in BUCKETS:
        if not slot_claims(state, b):
            continue
        before = slot_current(state, b)
        rc, cur = ensure(b, threshold=threshold)
        # 3 은 자리 칸이 바닥났을 때만이라, 다른 창의 칸 때문에 갈아탄 것은 계정이 바뀌었는지로 본다
        if rc == ENSURE_SWITCHED or (cur and cur != before):
            switched.append(b)
    return switched


def usable_now(bucket: str, threshold: float, model_bucket: str | None = None) -> bool | None:
    """자리 `bucket` 의 지금 계정으로 쓸 수 있나. 자리가 없으면 None(공용 판단으로).

    `model_bucket` 을 주면 그 창이 이 자리에 앉아 있는 걸 창 표시로 확인한 것이다. 그때는 자리 계정의
    그 모델 칸을 본다(Fable 자리의 Opus 창). 안 주면 옛 방식으로 짐작한다: 모델 이름과 같은 자리에
    창이 떠 있으면 그 자리 계정, 없으면 None.
    """
    if not enabled() or usage_check is None or not bucket:
        return None
    try:
        state = st.load_state()
    except Exception:
        return None
    cur = slot_current(state, bucket)
    if not cur:
        return None
    if model_bucket is None and not slot_claims(state, bucket):
        return None
    u = usage_check.get_usage_for(store_path(bucket, cur), is_current=False)
    if not u:
        return False
    return usage_check.which_limit(u, threshold=threshold, bucket=model_bucket or bucket) is None


def status_lines(state: dict) -> list:
    out = []
    if not enabled():
        return out
    for b in BUCKETS:
        labels = stored_labels(b)
        if not labels:
            continue
        cur = slot_current(state, b)
        n = len(slot_claims(state, b))
        out.append(f"  {b} 자리: 계정 {cur or '없음'} (저장본 {', '.join(labels)}, 창 {n}개)")
    return out
