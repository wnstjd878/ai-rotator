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
의도적 미구현: 창 안에서 /model 로 모델을 바꾼 경우의 자리 이동(자리는 켤 때 정해진다.
      바꾼 모델은 그 자리 계정의 그 칸을 쓴다). 자리 폴더 안 history.jsonl 은 사본이라 갈라진다.
마지막 점검: 2026-09-12
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
    return out


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

def pick(state: dict, bucket: str, exclude: str | None = None, now: float | None = None) -> str | None:
    """그 자리에 앉힐 계정. 저장본이 있고, 계정도 그 칸도 안 막힌 것 중 앞순위."""
    now = time.time() if now is None else now
    cands = [l for l in stored_labels(bucket)
             if l != exclude and not st.is_blocked(state, label=l, now=now)
             and not st.is_scoped_blocked(state, l, bucket, now)]
    order = st.priority_order(state, cands, now)
    return order[0] if order else None


def _revive(state: dict, bucket: str, below: float) -> list:
    """저장본이 있는데 기록상 막힌 계정 중 실제로는 그 칸이 열린 것을 풀어 준다."""
    if usage_check is None:
        return []
    freed = []
    now = time.time()
    for l in stored_labels(bucket):
        if not (st.is_blocked(state, label=l, now=now) or st.is_scoped_blocked(state, l, bucket, now)):
            continue
        u = usage_check.get_usage_for(store_path(bucket, l), is_current=False)
        if not u:
            continue
        if usage_check.which_limit(u, threshold=below, bucket=bucket) is None:
            box = dict(state.get("scoped_blocked") or {})
            if l in box and bucket in (box.get(l) or {}):
                box[l] = {k: v for k, v in box[l].items() if k != bucket}
                state["scoped_blocked"] = box
            blocked = dict(state.get("blocked") or {})
            blocked.pop(l, None)
            state["blocked"] = blocked
            freed.append(l)
            log(f"자리 {bucket}: '{l}' 는 실제로 열려 있어 막힘을 풉니다")
    return freed


ENSURE_SWITCHED = 3
# 되돌릴 계정의 그 칸 사용량 상한. claude_pool.PREEMPT_PCT(97) 와 같아야 한다. 이보다 높은
# 계정으로 되돌리면 창 지키기가 3분 안에 도로 밀어내 켤 때마다 왕복한다.
RECLAIM_MAX_PCT = 97.0


def _exhausted_kind(state: dict, cur: str, bucket: str, threshold: float):
    """자리 계정의 사용량을 읽어 그 칸이 바닥났는지. (kind 또는 None, 읽었는지)."""
    u = usage_check.get_usage_for(store_path(bucket, cur), is_current=False)
    if not u:
        # 못 읽었다. 기록상 막혀 있으면 기록으로 갈아탄다(2026-09-12 사고와 같은 갈래).
        if st.is_scoped_blocked(state, cur, bucket):
            return "model:" + bucket, False
        if st.is_blocked(state, cur):
            return "blocked", False
        log(f"자리 {bucket}: '{cur}' 사용량을 못 읽어 그대로 둔다"
            f"(사유={getattr(usage_check, 'last_error', None)})")
        return None, False
    st.note_weekly_reset(state, cur, u.get("weekly_reset"))
    st.note_usage(state, cur, u)
    kind = usage_check.which_limit(u, threshold=threshold, bucket=bucket)
    _mark_exhausted(state, cur, bucket, kind, u)
    return kind, True


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


def _earlier_than(state: dict, bucket: str, cur: str, now: float | None = None) -> list:
    """지금 자리 계정보다 주간 초기화가 이른 계정들(이른 순). 기록상 막힌 것은 뺀다.

    순서는 priority_order 한 벌을 그대로 쓴다(초기화가 이른 것부터, 시각을 모르면 뒤로).
    지금 계정이 기록상 막혀 있으면 되돌리기가 아니라 일반 교체가 처리하므로 빈 목록.
    """
    now = time.time() if now is None else now
    cands = [l for l in stored_labels(bucket)
             if not st.is_blocked(state, label=l, now=now)
             and not st.is_scoped_blocked(state, l, bucket, now)]
    order = st.priority_order(state, cands, now)
    if cur not in order:
        return []
    return order[:order.index(cur)]


def _reclaim(state: dict, bucket: str, cur: str) -> str | None:
    """켤 때: 초기화가 이른 계정이 그 칸을 아직 쓸 수 있으면 그쪽으로 옮긴다. 옮긴 라벨 또는 None.

    기록만 믿고 옮기지 않는다. 후보의 사용량을 실제로 읽어 그 칸이 RECLAIM_MAX_PCT 미만일 때만
    옮긴다(막힌 걸 모르고 옮기면 첫 요청부터 거절받고 다시 갈아탄다). 읽은 값이 한도면 기록에
    막아 두어 다음 켤 때는 안 읽는다. 못 읽은 후보(429 등)는 건너뛰고 그 다음 후보를 본다.
    """
    for cand in _earlier_than(state, bucket, cur):
        u = usage_check.get_usage_for(store_path(bucket, cand), is_current=False)
        if not u:
            log(f"자리 {bucket}: 초기화가 이른 '{cand}' 사용량을 못 읽어 되돌리지 않는다"
                f"(사유={getattr(usage_check, 'last_error', None)})")
            continue
        st.note_weekly_reset(state, cand, u.get("weekly_reset"))
        st.note_usage(state, cand, u)
        kind = usage_check.which_limit(u, threshold=RECLAIM_MAX_PCT, bucket=bucket)
        if kind:
            _mark_exhausted(state, cand, bucket, kind, u)
            continue
        # 방금 읽은 초기화 시각으로 다시 견줘 본다(기록이 낡아 앞섰던 것일 수 있다)
        if st.priority_order(state, [cand, cur])[0] != cand:
            continue
        if switch(state, bucket, cand, "주간 초기화가 이른 계정으로 되돌림(켜기 전)"):
            return cand
    return None


def _pick_or_revive(state: dict, bucket: str, exclude: str | None, threshold: float):
    nxt = pick(state, bucket, exclude=exclude)
    if nxt is not None:
        return nxt
    box = _slots(state).setdefault(bucket, {})
    if time.time() - float(box.get("revive_at") or 0) >= 1800:
        box["revive_at"] = time.time()
        if _revive(state, bucket, threshold):
            nxt = pick(state, bucket, exclude=exclude)
    return nxt


def ensure(bucket: str, threshold: float = 100.0, force_next: bool = False,
           reclaim: bool = False) -> tuple:
    """자리의 계정이 그 칸에서 바닥났으면 갈아끼운다. (종료코드, 지금 계정).

    종료코드: 0 그대로 / 3 갈아탔음 / 2 갈아탈 데 없음 / 1 교체 실패.
    `threshold` 97 은 창 지키기(거절받기 전에 미리), 100 은 켜기 전 확인.
    `force_next` 면 지금 계정을 두고 무조건 다른 계정으로(창이 한도로 끝난 뒤 되켤 때).
    `reclaim` 이면(켤 때만) 지금 계정이 멀쩡해도 주간 초기화가 더 이른 계정이 그 칸을 쓸 수
    있으면 그쪽으로 옮긴다(_reclaim). 이때 종료코드는 0 이다. 3 은 "바닥나서 갈아탔음" 이고
    cc 는 창이 끝난 뒤 3 을 한도로 본다.
    갈아탄 계정도 바닥일 수 있으므로 쓸 만한 계정이 나올 때까지 이어서 본다(--ensure 와 같은 규율).
    """
    if not enabled() or usage_check is None:
        return 0, None
    if not stored_labels(bucket):
        return 2, None
    switched = False
    with st.with_lock():
        state = st.prune_blocked(st.load_state())
        cur = slot_current(state, bucket)
        if cur and cur not in stored_labels(bucket):
            cur = None
        if cur is None:
            nxt = _pick_or_revive(state, bucket, None, threshold)
            if not nxt or not switch(state, bucket, nxt, "자리 처음 앉힘"):
                st.save_state(state)
                return (2 if not nxt else 1), None
            cur, switched = nxt, True
        for _ in range(len(stored_labels(bucket)) + 1):
            note = sync_creds(bucket, cur)
            if "되저장" in note or "밀어 넣음" in note:
                log(f"자리 {bucket}: {note}")
            if force_next:
                kind, force_next = "forced", False
            else:
                kind, read = _exhausted_kind(state, cur, bucket, threshold)
                if read:
                    sync_creds(bucket, cur)   # 저장본을 재발급했을 수 있다(401 뒤). 자리로 밀어 넣는다
            if not kind:
                break
            nxt = _pick_or_revive(state, bucket, cur, threshold)
            if nxt is None:
                st.save_state(state)
                log(f"자리 {bucket}: '{cur}' 의 {bucket} 칸이 바닥났지만 갈아탈 계정이 없다")
                return 2, cur
            why = "미리 넘김" if threshold < 100.0 else ("한도로 끝난 뒤" if kind == "forced" else "켜기 전 확인")
            if not switch(state, bucket, nxt, f"{bucket} 칸 소진, {why}"):
                st.save_state(state)
                return 1, cur
            cur, switched = nxt, True
        if reclaim and not switched and cur:
            got = _reclaim(state, bucket, cur)
            if got:
                cur = got
        st.save_state(state)
    return (ENSURE_SWITCHED if switched else 0), cur


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
        rc, _ = ensure(b, threshold=threshold)
        if rc == ENSURE_SWITCHED:
            switched.append(b)
    return switched


def usable_now(bucket: str, threshold: float) -> bool | None:
    """자리의 지금 계정으로 그 칸을 쓸 수 있나. 자리가 없으면 None(공용 판단으로)."""
    if not enabled() or usage_check is None or not bucket:
        return None
    try:
        state = st.load_state()
    except Exception:
        return None
    cur = slot_current(state, bucket)
    if not cur or not slot_claims(state, bucket):
        return None
    u = usage_check.get_usage_for(store_path(bucket, cur), is_current=False)
    if not u:
        return False
    return usage_check.which_limit(u, threshold=threshold, bucket=bucket) is None


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
