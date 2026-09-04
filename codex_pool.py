"""
== 운영 맥락 ==
용도: codex 를 부를 때 구독 한도 거절이 오면 자동으로 다음 계정 자격으로 갈아끼우고
      같은 요청을 다시 보낸다. 클로드 쪽 claude_pool.py 와 같은 뼈대이고, 상태만
      따로 쓴다(codex_state.json / codex_accounts.json / creds/codex).
흐름: (1) codex 실행 → (2) 결과 판정(codex_limit) → (3) 한도로 보이면 서버에 한 번 물어
      확증 → (4) 진짜면 그 계정을 해제 시각까지 쉬게 표시하고 다음 계정으로 교체 →
      (5) 같은 요청 재시도 → (6) 쓸 계정이 없으면 AllAccountsExhausted + 텔레그램 알림.
실행: python codex_pool.py --status        지금 계정 상태와 사용률 보기
      python codex_pool.py --next          지금 계정을 한도로 보고 다음 계정으로 넘김
      python codex_pool.py --use A         A 계정으로 지금 바로 교체
      python codex_pool.py -- exec "안녕"   codex 를 순환 보호 아래서 실행
호출: from codex_pool import run;  proc = run([codex_bin, "exec", ...], timeout=600)
전제: 계정마다 codex 설정 폴더가 따로 있어야 한다(codex_accounts.json 의 codex_home).
      계정 추가는 `python codex_login.py <라벨>`. codex logout 은 쓰지 않는다.
전환 방식(2026-07-28 변경): 자격 파일을 복사하지 않는다. 어느 폴더를 쓸지(CODEX_HOME)만
      바꿔서 실행한다. 한 폴더에서 다른 계정으로 로그인하면 이전 계정 자격이 서버에서
      폐기되기 때문이다(실측). 폴더가 갈려 있으면 두 계정이 공존한다.
      그래서 자격이 낡거나 덮어써질 일이 없다 — 각 폴더의 codex 가 자기 자격을 갱신한다.
의도적 미구현(2026-07-28 사용자 결정): 사용률을 미리 보고 넘기는 선제 전환 없음.
      거절을 받은 뒤에만 넘기고, 남은량은 codex_alert.py 가 알림으로만 알린다.
      실행 중인 codex 창의 실시간 교체 없음 — 껐다 다시 실행해야 새 계정이 붙는다.
마지막 점검: 2026-07-28
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import pool_state as st  # noqa: E402

st.use("codex")  # 이 프로세스는 지피티 쪽 상태만 다룬다 (불러오는 즉시)

import codex_home  # noqa: E402
import codex_limit  # noqa: E402
import usage_view  # 남은 사용량 표시 글자(클로드 쪽과 한 벌)  # noqa: E402
try:
    import codex_usage  # 계정별 사용률 (없어도 순환은 됨)
except Exception:
    codex_usage = None

# "쓸 수 있는 계정"은 자격 저장본이 아니라 폴더별 로그인 여부로 판단한다
st.SAVED_LABELS_FN = codex_home.logged_in_labels

NL = "\n"  # 알림 문구의 줄바꿈
PROVIDER = "codex"  # 공유 칠판에서 클로드 값과 갈라 쓰는 이름(키에 codex: 접두가 붙는다)
LOG_FILE = BASE / "rotator.log"
BROKEN_CRED_COOLDOWN = 30 * 60  # 갈아끼운 저장본이 상했을 때 그 계정을 쉬게 할 시간(초)
CODEX_BINS = (
    os.path.expanduser("~/AppData/Roaming/npm/codex.cmd"),
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
    os.path.expanduser("~/.local/bin/codex"),
)


class AllAccountsExhausted(RuntimeError):
    """쓸 수 있는 계정이 하나도 안 남았을 때."""

    def __init__(self, message, reset_at=None, notify_message=None):
        super().__init__(message)
        self.reset_at = reset_at
        self.notify_message = notify_message  # 알림을 보낼 차례면 그 문구, 아니면 None


def log(msg: str) -> None:
    """기록은 파일에만 남긴다.

    오류 통로로 내보내면 부르는 쪽이 출력을 합쳐 받을 때(2>&1) 이 줄이 codex 응답 앞에
    섞여 결과 해석이 깨진다. 화면으로 보려면 ROTATOR_VERBOSE=1 을 준다.
    """
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [codex] {msg}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if os.environ.get("ROTATOR_VERBOSE") or os.environ.get("CLAUDE_ROTATOR_VERBOSE"):
        print(f"[codex-pool] {msg}", file=sys.stderr)


def codex_bin() -> str:
    p = shutil.which("codex")
    if p:
        return p
    for c in CODEX_BINS:
        if os.path.isfile(c):
            return c
    raise RuntimeError("codex CLI 를 못 찾았습니다 (PATH 및 알려진 경로에 없음)")


def cred_path(label: str) -> Path:
    """그 계정의 자격 파일 자리. 계정마다 폴더가 따로다(복사본이 아니라 원본)."""
    return codex_home.auth_path(label)


def _switch(label: str, current: str | None = None) -> bool:
    """쓸 계정을 바꾼다. 파일은 건드리지 않는다 — 다음 실행에 그 폴더를 쓰게 할 뿐이다.

    그 폴더에 로그인이 없으면 바꾸지 않는다(바꿔봐야 로그인 거절만 받는다).
    """
    if not codex_home.is_logged_in(label):
        log(f"계정 교체 실패({label}): 그 폴더에 로그인이 없습니다 "
            f"(python codex_login.py {label})")
        return False
    return True


def notify(message: str) -> None:
    """사람이 알아야 할 일을 텔레그램으로 보낸다. 자격은 notify.py 가 파일에서 직접 읽는다."""
    try:
        import notify as notifier

        notifier.send(message)
    except Exception as e:
        log(f"알림 전송 실패: {type(e).__name__}")


def _ago_text(sec: float) -> str:
    """지난 시간을 '방금 / 12분 전 / 3시간 전 / 2일 전' 으로."""
    return usage_view.ago_text(sec)


def _host_label(host) -> str:
    """기계 이름을 사람이 알아보는 이름으로(본체/Mac/winserver). 못 바꾸면 원래 이름."""
    return usage_view.host_label(host)


def _share_usage(label: str, usage: dict) -> None:
    """읽은 사용량을 공유 칠판에 올린다. 실패해도 조용히 넘어간다(표시용이라 없어도 된다).

    지피티는 계정마다 폴더가 따로라 대개 자기 기계에서 다 읽힌다. 그런데 **Mac 은 사용량
    조회가 앞단에서 막혀(HTML 응답) 아무 계정도 못 읽는다.** 그때 다른 기계가 올린 값을
    보여주려고 남긴다. 계정을 고르거나 막는 판단에는 쓰지 않는다(낡은 값이므로).
    """
    try:
        import shared_state

        if shared_state.enabled():
            shared_state.put_usage(label, usage, provider=PROVIDER)
    except Exception:
        pass


def _shared_usage() -> dict | None:
    """공유 칠판에 올라온 계정별 사용량. 못 읽으면 None."""
    try:
        import shared_state

        if not shared_state.enabled():
            return None
        return shared_state.get_usage_all(provider=PROVIDER)
    except Exception:
        return None


def _share_current(label: str | None) -> None:
    """이 기계가 지금 물려 있는 계정을 공유 칠판에 적는다. 실패해도 조용히 넘어간다."""
    if not label:
        return
    try:
        import shared_state

        if shared_state.enabled():
            shared_state.put_current(label, provider=PROVIDER)
    except Exception:
        pass


def _shared_current() -> dict | None:
    """{라벨: [그 계정에 물려 있는 기계...]}. 못 읽으면 None.

    세 기계가 같은 계정에 몰리면 세션 한도를 3배 빨리 태운다. 어디가 어느 계정에
    앉아 있는지는 자기 화면만 봐서는 알 수 없어서 공유 칠판으로 본다.
    """
    try:
        import shared_state

        if not shared_state.enabled():
            return None
        return shared_state.get_current_all(provider=PROVIDER)
    except Exception:
        return None


EXHAUSTED_PCT = 99.5  # 이 이상 쓴 창은 초기화 전까지 안 풀린다


def looks_exhausted(state: dict, label: str, now: float | None = None) -> float | None:
    """마지막으로 읽어둔 값만 보고 "지금도 다 쓴 상태"인지. 맞으면 풀리는 시각, 아니면 None.

    note_usage 는 낡은 값이라 계정을 고르는 판단에 쓰지 말라고 못박혀 있다. 여기서 쓰는 건
    그 반대 방향 하나뿐이다 — **다 썼다고 기록된 계정을 피하는 것**. 다 쓴 창은 시간이
    지난다고 저절로 풀리지 않고 초기화 시각에만 풀리므로, 값이 낡아도 "아직 초기화 시각
    전이면 지금도 다 쓴 상태"는 참이다. 반대로 "여유 있다"는 낡은 값은 그 사이 다 썼을 수
    있어 절대 안 믿는다(그래서 여유 쪽은 None 을 준다).

    이게 없으면 사용량 조회가 막힌 기계(Mac 은 앞단 차단이라 늘 그렇다)에서 다 쓴 계정을
    그대로 켜서 똑같이 거절받는다 (2026-08-29 실측).
    """
    now = time.time() if now is None else now
    entry = (state.get("usage_seen") or {}).get(label)
    if not isinstance(entry, dict):
        return None
    for pct_key, reset_key in (("weekly_pct", "weekly_reset"), ("session_pct", "session_reset")):
        try:
            pct = float(entry.get(pct_key))
        except (TypeError, ValueError):
            continue
        if pct < EXHAUSTED_PCT:
            continue
        try:
            reset = float(entry.get(reset_key))
        except (TypeError, ValueError):
            continue
        if reset > now:      # 아직 초기화 전이면 지금도 다 쓴 상태다
            return reset
    return None


def _usage_line(entry: dict, now: float) -> str:
    """기록해둔 사용량 한 줄. 초기화 시각이 지난 창은 남은 100% 로 본다. 없으면 빈 글."""
    return usage_view.usage_line(entry, now)


def _last_seen_usage(state: dict, shared: dict | None, label: str, now: float) -> str:
    """지금 못 읽을 때: 다른 기계가 올린 값과 이 기계의 기록 중 더 최신 것을 보여준다.

    언제 어디서 읽은 값인지 같이 적는다(낡은 값을 지금 값으로 오해하면 안 된다).
    글자 만드는 규칙은 클로드 쪽과 한 벌로 쓴다(usage_view). Mac 은 앞단 차단이라
    늘 이 경로로 온다.
    """
    return usage_view.last_seen(state, shared, label, now)


def status_text() -> str:
    """계정별 남은 사용량 화면. 그리는 규칙은 클로드 쪽과 한 벌(usage_view).

    2026-09-04 에 모양을 바꿨다. 한 줄에 상태·주간·세션·초기화 시각을 쉼표로 이어
    붙이던 것을 계정마다 머리글 한 줄 + 칸마다 막대 한 줄로 나눴다.
    """
    state = st.prune_blocked(st.load_state())
    cur = state.get("current")
    now = time.time()
    saved = set(st.saved_labels())
    color = usage_view.supports_color()
    _share_current(state.get("current"))  # 이 기계가 어디 물려 있는지 먼저 올린다
    shared = _shared_usage()           # 다른 기계가 자기 계정에서 읽어 올린 값
    holders = _shared_current() or {}  # 어느 기계가 어느 계정에 물려 있는지
    seen = []  # 여기서 읽은 사용량·초기화 시각은 다음 계정 고르는 순서와 표시에 쓰인다
    hidden = set(st.retired_labels())  # 구독을 끊어 뺀 계정은 목록에서 감춘다(지금 쓰는 중이면 예외)
    lines = []
    for label in st.account_labels():
        if label in hidden and label != cur:
            continue
        marks = []
        who = holders.get(label) or []
        if label == cur:
            marks.append("지금 사용 중" + (f" ({', '.join(who)})" if who else ""))
        elif who:
            marks.append(f"{', '.join(who)} 사용 중")
        else:
            marks.append("대기")
        if label not in saved:
            marks.append(f"로그인 없음 (python codex_login.py {label})")
        until = (state.get("blocked") or {}).get(label)
        if until and float(until) > now:
            left = int(float(until) - now)
            marks.append(f"막힘, {left // 3600}시간 {left % 3600 // 60}분 뒤 풀림")
        entry, note = None, ""
        if codex_usage and label in saved:
            u = codex_usage.get_usage_for(cred_path(label), is_current=(label == cur))
            if u:
                entry = u
                plan = str(u.get("plan") or "").lower()
                if plan and plan not in ("pro", "plus", "team", "business", "enterprise", "edu"):
                    # 등급은 서버가 늘 같이 주는데 그동안 아무 데서도 안 봤다. 유료가 아니면
                    # 한도가 통째로 다른 규칙이라(30일 창) 사람이 바로 알아야 한다.
                    marks.append(f"등급 {plan} (유료 아님)")
                if u.get("limit_reached"):
                    marks.append("지금 한도 도달 상태")
                if u.get("email"):
                    marks.append(str(u["email"]))
                seen.append((label, u))
                _share_usage(label, u)
            else:
                # 이 기계에서 못 읽는 경우(Mac 은 앞단 차단이라 늘 여기로 온다)
                entry, note = usage_view.entry_and_note(state, shared, label, now)
        parts = usage_view.usage_parts(entry, now)[0] if entry else []
        if lines:
            lines.append("")
        lines += usage_view.account_block(label, marks, parts, now,
                                          current=(label == cur), color=color, note=note)
    st.remember_usage(seen)
    if not lines:
        lines.append("  계정이 없습니다. --use <라벨> 로 한 번 정해 주세요.")
    elif not cur:
        lines += ["", "  쓸 계정이 안 정해져 있습니다. --use <라벨> 로 한 번 정해 주세요."]
    return "\n".join(lines)


def use(label: str) -> int:
    """지정한 계정으로 즉시 교체하고 상태에 기록한다."""
    if label not in st.saved_labels():
        print(f"'{label}' 자격 저장본이 없습니다. 저장된 계정: {', '.join(st.saved_labels()) or '(없음)'}")
        return 1
    with st.with_lock():
        state = st.prune_blocked(st.load_state())
        if not _switch(label, state.get("current")):
            return 1
        st.clear_blocked_label(state, label)  # 손으로 고른 계정은 막힘 표시를 푼다(로컬+공유)
        st.set_current(state, label, "수동 지정")
        st.save_state(state)
    print(f"'{label}' 계정으로 교체했습니다.")
    return 0


def next_account(cooldown: float = codex_limit.DEFAULT_COOLDOWN) -> int:
    """지금 계정을 한도 도달로 보고 다음 계정으로 넘긴다 (사람이 직접 쓸 때).

    해제 시각은 서버 사용량에서 읽고, 못 읽으면 기본값으로 쉬게 한다.
    """
    with st.with_lock():
        state = st.prune_blocked(st.load_state())
        cur = state.get("current")
        if cur:
            until = None
            if codex_usage:
                u = codex_usage.get_usage_for(cred_path(cur), is_current=True)
                if u:
                    st.note_weekly_reset(state, cur, u.get("weekly_reset"))
                    until = codex_limit.reset_epoch_from_usage(
                        u, codex_usage.which_limit(u, threshold=90.0))
            st.mark_blocked(state, cur, until or (time.time() + cooldown), "사람이 한도 도달로 지정")
        nxt = st.pick_next(state, exclude=cur)
        if nxt is None:
            st.save_state(state)
            reset_at = st.earliest_reset(state)
            when = time.strftime("%m월 %d일 %H:%M", time.localtime(reset_at)) if reset_at else "알 수 없음"
            print(f"쓸 수 있는 계정이 없습니다. 가장 빨리 풀리는 시각: {when}")
            print(st.describe(state))
            return 2
        if not _switch(nxt, cur):
            st.save_state(state)
            return 1
        st.set_current(state, nxt, f"{cur or '?'} → {nxt} (사람 지정)")
        st.save_state(state)
    print(f"'{cur or '?'}' → '{nxt}' 계정으로 교체했습니다.")
    print("열려있는 codex 를 종료하고 다시 실행하면 적용됩니다.")
    return 0


def _switch_if_blocked(state: dict):
    """지금 계정이 이미 막혀 있으면 부르기 전에 갈아탄다. 막힌 계정 목록만 보고 판단한다.

    막힘은 머신들이 공유하므로, 다른 머신이 한도로 막아둔 계정을 그대로 쓰다가 똑같이
    거절받는 헛걸음을 없앤다. 사용량 조회를 하지 않아 값싸고, 알림도 안 보낸다
    (막을 때 이미 한 번 나갔다). 남은량을 보고 미리 넘기는 선제 전환과는 다르다.
    """
    cur = state.get("current")
    if not cur or not st.is_blocked(state, cur):
        return cur
    with st.with_lock():
        s = st.prune_blocked(st.load_state())
        cur = s.get("current")
        if not cur or not st.is_blocked(s, cur):
            return cur  # 그 사이 다른 프로세스가 바꿨다
        nxt = st.pick_next(s, exclude=cur)
        if nxt is None or not _switch(nxt, cur):
            return cur  # 갈아탈 데가 없으면 기존 흐름(거절 → 소진 알림)이 처리한다
        st.set_current(s, nxt, f"{cur} → {nxt} (이미 막힌 계정이라 미리 교체)")
        st.save_state(s)
        log(f"'{cur}' 는 이미 막혀 있어 부르기 전에 '{nxt}' 로 교체")
        return nxt


def _rotate_after_failure(used_label: str | None, verdict: str, out: str, err: str) -> str:
    """실패한 계정을 쉬게 하고 다음 계정으로 교체. 교체된 라벨을 반환."""
    with st.with_lock():
        state = st.prune_blocked(st.load_state())
        current = state.get("current")

        # 내가 쓰던 계정이 이미 다른 프로세스에 의해 교체됐으면 그대로 재시도
        if used_label is not None and current is not None and current != used_label:
            log(f"이미 '{current}' 로 교체돼 있어 그대로 재시도합니다.")
            return current

        if used_label and verdict != "error":
            if verdict == "limit":
                # 어느 한도인지, 언제 풀리는지는 서버 사용량에서 읽는다.
                # 못 읽으면 응답 문구 파싱으로 떨어진다.
                until = None
                if codex_usage:
                    u = codex_usage.get_usage_for(cred_path(used_label), is_current=True)
                    if u:
                        st.note_weekly_reset(state, used_label, u.get("weekly_reset"))
                        until = codex_limit.reset_epoch_from_usage(
                            u, codex_usage.which_limit(u, threshold=90.0))
                if until is None:
                    until = codex_limit.reset_epoch(out, err)
                st.mark_blocked(state, used_label, until, codex_limit.short_reason(out, err))
                left = int(until - time.time())
                log(f"'{used_label}' 한도 도달. {left // 3600}시간 {left % 3600 // 60}분 뒤 풀림")
            else:  # auth: 갈아끼운 저장본이 상한 경우에만 여기로 온다
                st.mark_blocked(state, used_label, time.time() + BROKEN_CRED_COOLDOWN,
                                codex_limit.short_reason(out, err))
                log(f"'{used_label}' 저장본이 상했습니다(로그인 거절). "
                    f"{BROKEN_CRED_COOLDOWN // 60}분 쉬게 하고 다음 계정으로 넘깁니다. "
                    f"그 계정으로 codex login 한 뒤 codex_save.py 로 다시 저장하세요.")

        nxt = st.pick_next(state, exclude=used_label)
        if nxt is None:
            reset_at = st.earliest_reset(state)
            when = time.strftime("%m월 %d일 %H:%M", time.localtime(reset_at)) if reset_at else "알 수 없음"
            msg = f"지피티 구독 계정이 전부 한도에 걸렸습니다. 가장 빨리 풀리는 시각: {when}"
            log(msg)
            should_notify = st.mark_notified(state, reset_at)
            st.save_state(state)
            # 실제 전송은 잠금 밖에서 한다. 통신이 잠금을 붙잡으면 뒤에 온 호출이 대기하다 터진다.
            raise AllAccountsExhausted(msg, reset_at=reset_at,
                                       notify_message=msg if should_notify else None)

        if not _switch(nxt, used_label):
            st.save_state(state)
            raise AllAccountsExhausted(f"'{nxt}' 로 자격 교체에 실패했습니다.")

        st.set_current(state, nxt, f"{used_label or '?'} → {nxt} ({verdict})")
        st.save_state(state)
        log(f"'{used_label or '?'}' → '{nxt}' 계정 교체 완료")
        _notify_switch(used_label, nxt, verdict, state)
        return nxt


def switch_message(used_label, nxt, verdict, state) -> str:
    """계정 전환 알림 문구. 두괄식 — 첫 줄이 '어디서 어디로, 왜'."""
    reason = "한도 도달"
    used_until = None
    if verdict == "limit" and codex_usage:
        u_used = codex_usage.get_usage(cred_path(used_label))
        kind = codex_usage.which_limit(u_used, threshold=90.0)
        if kind == "session":
            reason = "세션(5시간) 한도 도달"
            used_until = (u_used or {}).get("session_reset")
        elif kind == "weekly":
            reason = "주간 한도 도달"
            used_until = (u_used or {}).get("weekly_reset")
    elif verdict != "limit":
        reason = "로그인 저장본 문제"

    lines = [f"[지피티] 계정 {used_label} → {nxt} 전환됨 ({used_label} {reason})", ""]

    if codex_usage:  # 새 계정이 얼마나 남았는지 (전환 직후라 이게 지금 쓰는 계정)
        u_next = codex_usage.get_usage_for(cred_path(nxt), is_current=True)
        rem = None if not u_next or u_next.get("weekly_pct") is None \
            else max(0.0, 100 - float(u_next["weekly_pct"]))
        if rem is not None:
            lines.append(f"{nxt} 주간 {codex_usage.pct_text(rem)} 남음")

    blocked = state.get("blocked") or {}
    if used_until is None:
        used_until = blocked.get(used_label)
    if used_until and codex_usage:
        if verdict == "limit":
            lines.append(f"{used_label} {codex_usage.fmt_reset_full(used_until)}")
        else:  # 한도가 아니라 잠깐 쉬게 한 것이라 '초기화' 라고 하면 안 된다
            left = codex_usage.left_phrase(float(used_until) - time.time())
            lines.append(f"{used_label} {left} 다시 시도")

    ok, dead = [], []
    for l in st.account_labels():
        if l in (used_label, nxt) or l in blocked:
            continue
        rem = codex_usage.weekly_left(cred_path(l)) if codex_usage else None
        if rem is None:
            ok.append(f"계정 {l}")  # 못 읽었으면 못 쓴다고 단정하지 않는다
        elif rem > 0:
            ok.append(f"계정 {l} {codex_usage.pct_text(rem)}")
        else:
            dead.append(f"계정 {l} 0%")
    if ok:
        lines += ["", "전환 가능"] + ok
    if dead:
        lines += ["", "사용 불가"] + dead
    if not ok and not dead:
        lines += ["", "남은 계정 없음"]
    return "\n".join(lines)


def _notify_switch(used_label, nxt, verdict, state):
    """계정이 바뀔 때 텔레그램으로 알린다. 머신들을 통틀어 전환당 한 번만."""
    if not used_label:
        return  # 시작점 지정 등, 전환이 아니면 알리지 않는다
    key = f"switch_{used_label}_{nxt}_{int(time.time() // 600)}"  # 10분 창당 1회
    if not st.claim_notice(key):
        return
    notify(switch_message(used_label, nxt, verdict, state))


def _verify_limit(used_label: str | None, verdict: str) -> str:
    """한도로 보이는 판정을 서버에 물어 확증한다. 아니면 error 로 낮춘다.

    codex 는 모델 답변을 그대로 흘리므로 문구만 믿으면 답변 내용에 넘어갈 수 있다.
    확증에 실패(조회 불가)하면 원래 판정을 그대로 둔다 — 조회가 막혔다고 순환이
    멈추면 안 되기 때문이다.
    """
    if verdict != "limit" or not used_label:
        return verdict
    conf = codex_limit.confirm_limit(cred_path(used_label), is_current=True)
    if conf is False:
        log(f"'{used_label}' 한도 문구가 보였지만 서버 사용량은 여유가 있습니다. "
            f"계정을 막지 않고 일반 실패로 처리합니다.")
        return "error"
    return verdict


def _notify_model_rejected(out: str, err: str) -> None:
    """모델 이름 거절을 사람에게 한 번만 알린다. 자동 호출이 조용히 죽는 걸 막는다."""
    if not st.claim_notice(f"model_rejected_{int(time.time() // 3600)}"):  # 1시간에 한 번
        return
    reason = codex_limit.short_reason(out, err, limit=200)
    notify("[지피티] 설정한 모델을 서버가 거절했습니다." + NL + NL
           + reason + NL + NL
           + "계정 한도 문제가 아닙니다. codex 설정 파일(config.toml)의 model 값을 "
             "쓸 수 있는 이름으로 바꿔야 합니다.")


def run(args, *, input=None, timeout=None, env=None, max_attempts=None, **kwargs):
    """codex 를 순환 보호 아래서 실행한다. subprocess.run 과 같은 결과를 돌려준다.

    한도 거절이면 계정을 바꿔 같은 요청을 다시 보낸다.
    로그인 거절은 두 가지로 갈린다. 처음부터 났으면 이 환경에서 로그인이 안 된 것이라
    계정을 안 바꾸고 멈추고, 계정을 갈아끼운 뒤에 났으면 그 저장본이 상한 것이라
    쉬게 하고 다음 계정으로 넘어간다.
    """
    labels = st.saved_labels()
    if max_attempts is None:
        max_attempts = max(2, len(labels) + 1)
    max_attempts = max(1, int(max_attempts))

    if "stdout" not in kwargs and "stderr" not in kwargs:
        kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")   # Windows 기본 코드페이지로 읽다 깨지는 것 방지
    kwargs.setdefault("errors", "replace")
    if not kwargs.get("capture_output") and "stdout" not in kwargs:
        log("출력을 안 잡는 호출이라 한도 거절을 알아볼 수 없습니다. 계정 교체가 안 됩니다.")

    want_check = bool(kwargs.pop("check", False))

    retried_plain = False
    switched_here = False  # 이 호출 안에서 계정을 갈아끼웠는지
    proc = None
    for attempt in range(max_attempts):
        state = st.prune_blocked(st.load_state())
        used_label = _switch_if_blocked(state)

        # 지금 계정의 폴더를 쓰게 한다. 이게 곧 계정 전환이다(파일 복사 없음).
        run_env = codex_home.env_for(used_label, env) if used_label else env
        proc = subprocess.run(args, input=input, timeout=timeout, env=run_env, **kwargs)
        verdict = codex_limit.classify(proc.returncode, proc.stdout, proc.stderr)
        verdict = _verify_limit(used_label, verdict)

        if verdict == "ok":
            return _finish(proc, args, want_check)

        if verdict == "model":
            # 계정을 바꿔도 안 풀린다. 재시도도 무의미하니 바로 돌려주고 고칠 곳을 알려준다.
            log("서버가 이 모델 이름을 거절했습니다. 계정 문제가 아니라 설정 문제입니다. "
                "config.toml 의 model 값을 쓸 수 있는 이름으로 바꾸세요 "
                "(쓸 수 있는 목록은 codex 의 /model 화면).")
            _notify_model_rejected(proc.stdout, proc.stderr)
            return _finish(proc, args, want_check)

        if verdict == "auth" and not switched_here:
            log("로그인이 안 된 상태입니다. 계정을 바꾸지 않습니다. "
                "그 머신에서 codex login 을 했는지 확인하세요.")
            return _finish(proc, args, want_check)

        if verdict == "error":
            if retried_plain:
                return _finish(proc, args, want_check)  # 원인 모를 실패는 부르는 쪽이 판단
            retried_plain = True
            log(f"한도와 무관한 실패. 한 번 더 시도합니다 (종료코드 {proc.returncode})")
            time.sleep(2)
            continue

        try:
            nxt = _rotate_after_failure(used_label, verdict, proc.stdout, proc.stderr)
        except AllAccountsExhausted as e:
            if e.notify_message:  # 전송은 잠금 밖에서
                notify(e.notify_message)
            raise
        switched_here = switched_here or (nxt != used_label)

    log("재시도 횟수를 다 썼습니다. 마지막 결과를 그대로 돌려줍니다.")
    return _finish(proc, args, want_check)


def _finish(proc, args, want_check):
    """부르는 쪽이 check=True 를 원했다면 원래 뜻대로 실패에서 예외를 올린다."""
    if want_check and proc is not None and proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, args, output=proc.stdout, stderr=proc.stderr
        )
    return proc


def _block_exhausted(state: dict) -> None:
    """마지막으로 읽은 값이 "다 씀" 인 계정을 막힘 목록에 올린다(잠금 안에서 부를 것).

    막힘 목록은 기계들이 공유하므로, 한 기계가 알아낸 소진을 다른 기계도 그대로 피한다.
    푸는 시각은 그 창의 초기화 시각이라 저절로 풀린다.
    """
    for label in st.account_labels():
        if st.is_blocked(state, label):
            continue
        until = looks_exhausted(state, label)
        if until:
            st.mark_blocked(state, label, until, "마지막으로 읽은 사용량이 소진")
            log(f"'{label}' 은 다 쓴 것으로 기록돼 있어 후보에서 뺍니다")


def _print_home() -> int:
    """지금 써야 할 계정의 폴더 경로를 출력한다. 필요하면 계정을 갈아탄다.

    래퍼(gcauto.ps1)가 codex 를 켜기 직전에 부른다. 여기서 갈아타면 사람이 gn 을
    따로 칠 일이 없다. 갈아탄 사실은 기록에 남기되 알림은 안 보낸다
    (막을 때 이미 한 번 나갔다).
    """
    state = st.prune_blocked(st.load_state())
    cur = state.get("current")
    need_switch = ((not cur) or (not codex_home.is_logged_in(cur))
                   or st.is_blocked(state, cur) or bool(looks_exhausted(state, cur)))
    if need_switch:
        with st.with_lock():
            s = st.prune_blocked(st.load_state())
            _block_exhausted(s)  # 다 쓴 걸로 기록된 계정은 고르는 후보에서 빠지게 먼저 막는다
            c = s.get("current")
            if (not c) or (not codex_home.is_logged_in(c)) or st.is_blocked(s, c):
                nxt = st.pick_next(s, exclude=None)
                if nxt is None:
                    return 1  # 쓸 계정이 없다 — 래퍼가 안내를 띄운다
                if nxt != c:
                    st.set_current(s, nxt, f"{c or '?'} → {nxt} (켜기 전 자동 전환)")
                    st.save_state(s)
                    log(f"'{c or '?'}' 는 못 쓰는 상태라 켜기 전에 '{nxt}' 로 전환")
                cur = nxt
            else:
                cur = c
    if not cur:
        return 1
    print(str(codex_home.home_of(cur)))
    return 0


def run_codex(cli_args, **kwargs):
    """codex 실행 파일 경로를 알아서 붙여주는 짧은 형태. run() 과 동작 같음."""
    return run([codex_bin(), *cli_args], **kwargs)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if argv[0] == "--status":
        print(status_text())
        return 0
    if argv[0] == "--next":
        return next_account()
    if argv[0] == "--use":
        if len(argv) < 2:
            print("사용법: python codex_pool.py --use <라벨>")
            return 1
        return use(argv[1].strip())
    if argv[0] == "--limited":
        # 지금 계정이 이미 한도에 걸려 있는지만 알려준다(종료코드 3). 미리 넘기지는 않는다.
        # 대화형 래퍼(gcauto.ps1)가 codex 가 끝난 뒤 이걸로 판단한다.
        if codex_usage is None:
            return 0
        cur = (st.load_state() or {}).get("current")
        if not cur:
            return 0
        u = codex_usage.get_usage_for(cred_path(cur), is_current=True)
        if not u:
            # 조회가 막혀도 "여유 있음"으로 통과시키면 안 된다. Mac 은 앞단 차단이라
            # 늘 여기로 오고, 그대로 두면 다 쓴 계정으로 켜서 똑같이 거절받는다.
            # 마지막으로 읽어둔 값이 "다 씀 + 아직 초기화 전"이면 그것만 믿는다.
            until = looks_exhausted(st.load_state(), cur)
            if until:
                left = codex_usage.left_phrase(until - time.time()) if codex_usage else ""
                print(f"{cur} 한도 도달(마지막으로 읽은 값 기준) {left}")
                return 3
            print(f"사용량 못 읽음 ({codex_usage.last_error or '사유 모름'})")
            return 0
        if u.get("limit_reached") or u.get("reached_type") or \
                codex_usage.which_limit(u, threshold=99.5):
            print(f"{cur} 한도 도달: {codex_usage.summarize(u)}")
            return 3
        print(f"{cur} 여유 있음: {codex_usage.summarize(u)}")
        return 0
    if argv[0] == "--home":
        # 지금 써야 할 계정의 codex 폴더 경로(래퍼가 CODEX_HOME 으로 쓴다).
        # 지금 계정이 막혀 있거나 로그인이 없으면 여기서 갈아탄다 — 사람이 명령을
        # 따로 칠 필요가 없게. 막힘은 머신들이 공유하므로 다른 머신이 한도로 막아둔
        # 계정을 이 머신이 그대로 켜서 똑같이 거절받는 헛걸음도 여기서 막힌다.
        return _print_home()
    if argv[0] == "--":
        try:
            proc = run_codex(argv[1:])
        except AllAccountsExhausted as e:
            print(str(e), file=sys.stderr)
            return 2
        sys.stdout.write(proc.stdout or "")
        sys.stderr.write(proc.stderr or "")
        return proc.returncode

    print("알 수 없는 인자입니다. --status / --next / --use <라벨> / -- <codex 인자> 중 하나를 쓰세요.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
