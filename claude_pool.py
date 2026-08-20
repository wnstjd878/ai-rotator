"""
== 운영 맥락 ==
용도: claude CLI 를 부를 때 구독 한도 거절이 오면 자동으로 다음 계정 자격으로 갈아끼우고
      같은 요청을 다시 보낸다. 봇들이 subprocess.run(claude ...) 대신 이 모듈의
      run() 을 쓰면 한도로 인한 실패가 사라진다.
흐름: (1) claude 실행 → (2) 결과 판정(limit_detect) → (3) 한도면 그 계정을 해제 시각까지
      쉬게 표시하고 다음 계정으로 자격 교체(switch_account) → (4) 같은 요청 재시도 →
      (5) 쓸 계정이 하나도 없으면 AllAccountsExhausted 를 올리고 텔레그램으로 알림.
      (6) 세션(5시간)으로 잠깐 비켜났던 앞순위 계정이 풀리면 부르기 전에 되돌린다
          (_reclaim_locked). 안 그러면 뒷순위 계정 주간이 먼저 깎인다.
실행: python claude_pool.py --status          지금 계정 상태 보기
      python claude_pool.py --next            지금 계정을 한도로 보고 다음 계정으로 넘김
      python claude_pool.py --use A           A 계정으로 지금 바로 교체
      python claude_pool.py --ensure          켜기 직전 확인: 지금 계정이 다 찼으면 미리 교체
      python claude_pool.py -- -p "안녕"       claude 를 순환 보호 아래서 실행
호출: from claude_pool import run;  proc = run([claude_bin, "--print", ...], timeout=240)
전제: creds/<라벨>.json 에 계정별 자격 저장본이 있어야 한다 (save_account.py 로 저장).
      Mac 은 Keychain 을 고쳐야 해서 화면(Aqua) 세션에서 돌아야 한다. launchd 봇은 해당됨.
      ssh 비대화형에서는 자격 교체가 실패할 수 있다.
동시성: 여러 봇이 같이 실패해도 교체는 잠금으로 한 번만 일어난다. 다른 프로세스가 이미
      계정을 바꿨으면 교체 없이 바로 재시도한다.
모델 칸: 계정 전체 한도와 별개로 모델별 주간 칸(화면 표시 'Fable')이 따로 있다.
      계정 주간이 40% 여유여도 Fable 만 0% 일 수 있어서(2026-08-12 실사고) 쓰려는 모델을
      알아내(intended_model: --model 인자 → settings.json 고정값) 그 칸까지 본다.
      그 칸만 바닥나면 **계정을 통째로 막지 않고 그 칸만** 막는다(scoped_blocked).
      같은 계정으로 Opus 는 계속 쓰고, Fable 로 켤 때만 그 계정을 건너뛴다.
의도적 미구현: 여유 있는 계정을 미리 바꾸는 선제 전환 없음(limit_detect 참고).
      다른 계정의 모델 칸을 미리 못 본다 — 저장본 출입증이 8시간이라 대개 401 이 나고,
      그 계정이 현재가 된 뒤에야 읽힌다. 그래서 갈아탄 계정도 바닥이면 한 번 더 넘어간다.
      --ensure 는 선제 전환이 아니다 — 이미 100% 다 찬 계정만 켜기 전에 건너뛴다.
      되돌리기도 선제 전환이 아니다 — 사용량을 보지 않고 막힘이 풀렸는지만 본다.
      대화형 claude 세션의 실시간 교체 없음 — 이미 떠 있는 세션은 재시작해야 새 계정이 붙는다.
마지막 점검: 2026-07-27
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
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import limit_detect  # noqa: E402
import pool_state as st  # noqa: E402
import usage_view  # 남은 사용량 표시 글자(지피티 쪽과 한 벌)  # noqa: E402
try:
    import usage_check  # 계정별 세션/주간 사용률 (없어도 순환은 됨)
except Exception:
    usage_check = None

LOG_FILE = BASE / "rotator.log"
BROKEN_CRED_COOLDOWN = 30 * 60  # 갈아끼운 저장본이 상했을 때 그 계정을 쉬게 할 시간(초)
AUTH_FAIL_WINDOW = 10 * 60      # 이 시간 안에 서로 다른 계정이 거절받으면 자격 자리를 의심한다
AUTH_REJECT_LIMIT = 3           # 강제 교체에 필요한 최소 거절 횟수(이 횟수 + 아래 시간 둘 다)
AUTH_REJECT_STUCK = 3 * 60      # 같은 계정이 이 시간을 넘겨 계속 거절받으면 판정을 믿지 말고 갈아탄다
FORCED_ROTATE_COOLDOWN = 5 * 60  # 그렇게 강제로 넘길 때 그 계정을 쉬게 할 시간(초)
BARRED_COOLDOWN = 24 * 3600     # 조직 설정 차단(barred) 계정을 쉬게 할 시간. 관리자가 다시 켤 수 있어 하루 뒤 재확인
SCOPED_LOW_LEFT = 5.0           # 모델 칸이 이만큼 이하로 남으면 바닥으로 본다(which_limit 의 95% 와 같은 선)
KEYCHAIN_FIX_COOLDOWN = 60 * 60  # 열쇠꾸러미를 스스로 지운 뒤, 다시 지우기까지 두는 시간
KEYCHAIN_STAMP = BASE / ".keychain-cleared"  # 마지막으로 스스로 지운 시각
CLAUDE_BINS = (
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    os.path.expanduser("~/.local/bin/claude"),
    os.path.expanduser("~/AppData/Roaming/npm/claude.cmd"),
)


class AllAccountsExhausted(RuntimeError):
    """쓸 수 있는 계정이 하나도 안 남았을 때."""

    def __init__(self, message, reset_at=None, notify_message=None):
        super().__init__(message)
        self.reset_at = reset_at
        self.notify_message = notify_message  # 알림을 보낼 차례면 그 문구, 아니면 None


def log(msg: str) -> None:
    """기록은 파일에만 남긴다.

    오류 통로로 내보내면 봇이 출력을 합쳐 받을 때(2>&1) 이 줄이 claude 응답 앞에 섞여
    JSON 해석이 깨진다. 하필 계정 교체 순간에만 깨져서 원인 찾기가 어렵다.
    화면으로 보고 싶으면 CLAUDE_ROTATOR_VERBOSE=1 을 준다.
    """
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if os.environ.get("CLAUDE_ROTATOR_VERBOSE"):
        print(f"[pool] {msg}", file=sys.stderr)


def claude_bin() -> str:
    p = shutil.which("claude")
    if p:
        return p
    for c in CLAUDE_BINS:
        if os.path.isfile(c):
            return c
    raise RuntimeError("claude CLI 를 못 찾았습니다 (PATH 및 알려진 경로에 없음)")


def _switch(label: str, leaving: str | None = None) -> bool:
    """switch_account.py 를 그대로 재사용해 자격을 교체한다.

    leaving 은 지금까지 쓰던 계정이다. 갈아타기 직전에 그 계정의 갱신된 자격을
    creds/<라벨>.json 으로 되저장하라는 뜻(안 하면 그 계정 자격이 죽는다 — save_back 참조).
    """
    cmd = [sys.executable, str(BASE / "switch_account.py"), label]
    if leaving:
        cmd += ["--from", leaving]
    ret = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if ret.returncode != 0:
        log(f"자격 교체 실패({label}): {(ret.stderr or ret.stdout).strip()[:200]}")
        return False
    for line in (ret.stdout or "").splitlines():
        if line.startswith("[save-back]") or line.startswith("[keychain]"):
            # 되저장·열쇠꾸러미 반영 결과는 남긴다(유실이 다시 생기면 여기서 보인다)
            log(line.strip())
    return True


def notify(message: str) -> None:
    """전 계정 소진 같은 사람이 알아야 할 일을 텔레그램으로 보낸다.

    자격은 notify.py 가 파일에서 직접 읽는다. 여기서는 값에 손대지 않는다.
    보내기에 실패해도 본 작업을 막지 않는다.
    """
    try:
        import notify as notifier

        notifier.send(message)
    except Exception as e:  # 알림 실패는 기록만
        log(f"알림 전송 실패: {type(e).__name__}")


def _ago_text(sec: float) -> str:
    """지난 시간을 '방금 / 12분 전 / 3시간 전 / 2일 전' 으로."""
    return usage_view.ago_text(sec)


def _host_label(host) -> str:
    """기계 이름을 사람이 알아보는 이름으로(본체/Mac/winserver). 못 바꾸면 원래 이름."""
    return usage_view.host_label(host)


def _usage_line(entry: dict, now: float) -> str:
    """기록해둔 사용량 한 줄. 초기화 시각이 지난 창은 남은 100% 로 본다. 없으면 빈 글."""
    return usage_view.usage_line(entry, now)


def _last_seen_usage(state: dict, shared: dict | None, label: str, now: float) -> str:
    """지금 못 읽을 때: 다른 머신이 올린 값과 이 머신의 기록 중 더 최신 것을 보여준다.

    지금 안 쓰는 계정은 저장본 출입증이 8시간이라 대개 조회가 안 된다(자격이 죽은 게 아니다).
    세 머신이 서로 다른 계정에 앉아 있어서, 공유 칠판을 보면 남의 머신이 쓰는 계정의
    잔여량까지 보인다. 자격을 새로 발급받지 않아도 된다(발급은 열쇠가 회전해 위험하다).
    글자 만드는 규칙은 지피티 쪽과 한 벌로 쓴다(usage_view).
    """
    return usage_view.last_seen(state, shared, label, now)


def _share_usage(label: str, usage: dict) -> None:
    """읽은 사용량을 공유 칠판에 올린다. 실패해도 조용히 넘어간다(표시용이라 없어도 된다)."""
    try:
        import shared_state

        if shared_state.enabled():
            shared_state.put_usage(label, usage)
    except Exception:
        pass


def _shared_usage() -> dict | None:
    """공유 칠판에 올라온 계정별 사용량. 못 읽으면 None."""
    try:
        import shared_state

        if not shared_state.enabled():
            return None
        return shared_state.get_usage_all()
    except Exception:
        return None


def _share_current(label: str | None) -> None:
    """이 머신이 지금 물려 있는 계정을 공유 칠판에 적는다. 실패해도 조용히 넘어간다."""
    if not label:
        return
    try:
        import shared_state

        if shared_state.enabled():
            shared_state.put_current(label)
    except Exception:
        pass


def _shared_current() -> dict | None:
    """{라벨: [그 계정에 물려 있는 머신...]}. 못 읽으면 None."""
    try:
        import shared_state

        if not shared_state.enabled():
            return None
        return shared_state.get_current_all()
    except Exception:
        return None


def _scoped_notice(bucket: str, rows, now: float) -> str | None:
    """쓰려는 모델 칸이 어느 계정에도 안 남았으면 한 줄로 먼저 알린다.

    없으면 None(여유가 있거나 판단할 근거가 없을 때). 이게 없으면 계정을 하나씩
    건너뛰다 마지막 계정에서야 '전 계정 한도' 를 알게 된다(2026-08-20 사용자 요청).
    **잔량을 모르는 계정은 바닥으로 세지 않는다** — 모르는 걸 소진으로 보면 멀쩡한
    계정을 버린다(2026-07-29 에 A~D 가 줄줄이 막힌 사고가 그 종류였다). 대신 따로 적는다.
    표시 전용이다. 계정을 고르거나 막는 판단은 지금처럼 실제 거절로만 한다.
    """
    known = [(label, left, reset) for label, left, reset in rows if left is not None]
    if not known or any(left > SCOPED_LOW_LEFT for _, left, _ in known):
        return None
    labels = ", ".join(label for label, _, _ in known)
    unknown = [label for label, left, _ in rows if left is None]
    if unknown:
        # 모르는 계정이 섞여 있으면 '쓸 계정이 없다' 고 단정하지 않는다. 확인한 범위만 말한다.
        parts = [f"쓰려는 모델 칸({bucket}): 잔량을 확인한 계정({labels})은 모두 바닥입니다."]
    else:
        parts = [f"쓰려는 모델 칸({bucket}): 쓸 수 있는 계정이 없습니다({labels} 모두 바닥)."]
    soon = [(reset, label) for label, _, reset in known if reset]
    if soon:
        reset, label = min(soon)
        parts.append(f"가장 빨리 풀리는 건 {label}"
                     f" ({time.strftime('%m/%d %H:%M', time.localtime(reset))}).")
    if unknown:
        parts.append("잔량 모르는 계정: " + ", ".join(unknown) + ".")
    return "  " + " ".join(parts)


def _exhausted_message(state: dict, bucket: str | None, when: str,
                       lead: str = "클로드 ") -> str:
    """쓸 계정이 없을 때의 문구. **모델 칸만 바닥난 경우를 갈라 말한다.**

    2026-08-20 사고: Fable 칸만 A·B·D 전부 바닥인데 "구독 계정이 전부 한도에 걸렸습니다"
    로 텔레그램이 나가 구독이 통째로 떨어진 것처럼 읽혔다. 실제로는 주간이 22~47% 남아
    Opus 는 멀쩡히 쓸 수 있었다. 전환 알림(switch_message)은 2026-08-14 에 갈라 말하게
    고쳤는데 이 알림만 안 고쳐져 있었다.

    판별은 실제로 고르는 함수에 맡긴다 — 그 칸을 빼고 물었을 때 쓸 계정이 나오면
    계정 자체는 살아 있는 것이다(표시용 잔량 값으로 판단하지 않는다).
    """
    if bucket:
        try:
            account_left = st.pick_next(state) is not None
        except Exception:
            account_left = False
        if account_left:
            return (f"{lead}{bucket} 칸이 계정 전부 바닥입니다(계정 주간은 여유 있음). "
                    f"다른 모델로 켜면 지금도 쓸 수 있습니다. "
                    f"{bucket} 칸이 가장 빨리 풀리는 시각: {when}")
    return f"{lead}구독 계정이 전부 한도에 걸렸습니다. 가장 빨리 풀리는 시각: {when}"


def status_text(model: str | None = None) -> str:
    state = st.prune_blocked(st.load_state())
    cur = state.get("current") or "(모름, --use 로 한 번 정해주세요)"
    now = time.time()
    saved = set(st.saved_labels())
    # 이 창이 쓸 모델의 칸(대개 settings.json 고정값). 못 알아내면 요약 줄을 안 만든다.
    bucket = (usage_check.bucket_for_model(model or intended_model())
              if usage_check else None)
    scoped_rows = []
    lines = [f"현재 계정: {cur}"]
    _share_current(state.get("current"))  # 이 머신이 어디 물려 있는지 먼저 올린다
    shared = _shared_usage()   # 다른 머신이 자기 계정에서 읽어 올린 값
    holders = _shared_current() or {}  # 어느 머신이 어느 계정에 물려 있는지
    seen = []  # 여기서 읽은 사용량·초기화 시각은 다음 계정 고르는 순서와 표시에 쓰인다
    hidden = set(st.retired_labels())  # 구독 해지 계정은 목록에서 뺀다(현재 계정이면 예외로 보여줌)
    for label in st.account_labels():
        if label in hidden and label != cur:
            continue
        marks = []
        who = holders.get(label) or []
        if label == cur:
            marks.append("현재 사용 중" + (f"({', '.join(who)})" if who else ""))
        elif who:
            marks.append(f"{', '.join(who)} 사용 중")
        if label not in saved:
            marks.append("자격 없음")
        until = (state.get("blocked") or {}).get(label)
        if until and float(until) > now:
            left = int(float(until) - now)
            marks.append(f"막힘({left // 3600}시간 {left % 3600 // 60}분 뒤 풀림)")
        # 실시간 사용률(세션/주간). 못 읽으면 다른 머신이 올린 값이나 이 기기 기록으로 대신한다.
        if usage_check and label in saved:
            u = usage_check.get_usage_for(BASE / "creds" / f"{label}.json",
                                          is_current=(label == cur))
            if u:
                marks.append(usage_check.summarize(u))
                seen.append((label, u))
                _share_usage(label, u)
            else:
                marks.append(_last_seen_usage(state, shared, label, now))
                u = usage_view.pick_entry(state, shared, label)[1]
            if bucket:
                # 화면에 적은 것과 같은 기록에서 칸 잔량을 뽑는다(둘이 어긋나면 안 된다).
                left, reset = usage_view.scoped_left(u, bucket, now)
                scoped_rows.append((label, left, reset))
        lines.append(f"  {label}: " + ", ".join(marks))
    st.remember_usage(seen)
    if bucket:
        notice = _scoped_notice(bucket, scoped_rows, now)
        if notice:
            lines.append(notice)
    return "\n".join(lines)


def use(label: str) -> int:
    """지정한 계정으로 즉시 교체하고 상태에 기록한다."""
    if label not in st.saved_labels():
        print(f"'{label}' 자격 저장본이 없습니다. 저장된 계정: {', '.join(st.saved_labels()) or '(없음)'}")
        return 1
    with st.with_lock():
        state = st.prune_blocked(st.load_state())
        if not _switch(label, leaving=state.get("current")):
            return 1
        st.clear_blocked_label(state, label)  # 손으로 고른 계정은 막힘 표시를 푼다(로컬+공유)
        # manual=True: 사람이 고른 계정은 그 계정이 막힐 때까지 되돌리기가 안 건드린다
        st.set_current(state, label, "수동 지정", manual=True)
        st.save_state(state)
    print(f"'{label}' 계정으로 교체했습니다.")
    return 0


def next_account(cooldown: float = limit_detect.DEFAULT_COOLDOWN,
                 model: str | None = None) -> int:
    """지금 계정을 한도 도달로 보고 다음 계정으로 넘긴다 (사람이 직접 쓸 때).

    대화형 claude 창에서 한도 거절을 봤을 때 쓴다. 교체 뒤 claude 를 새로 실행해야 적용된다.
    `model` 을 주고 실제로 그 모델 칸만 바닥났으면 **그 칸만** 막는다. 계정을 통째로
    막으면 그 계정에 남아 있는 다른 모델 몫까지 같이 버리기 때문이다.
    """
    bucket = usage_check.bucket_for_model(model) if usage_check else None
    with st.with_lock():
        state = st.prune_blocked(st.load_state())
        cur = state.get("current")
        if cur:
            scoped_only = False
            if bucket and usage_check:
                u = usage_check.get_usage_for(BASE / "creds" / f"{cur}.json", is_current=True)
                # 계정 전체는 멀쩡하고 그 칸만 찼을 때만 갈라 막는다.
                if (usage_check.which_limit(u, threshold=100.0, bucket=bucket) or "") \
                        .startswith("model:"):
                    st.mark_scoped_blocked(state, cur, bucket,
                                           usage_check.scoped_reset(u, bucket)
                                           or (time.time() + cooldown))
                    scoped_only = True
            if not scoped_only:
                st.mark_blocked(state, cur, time.time() + cooldown, "사람이 한도 도달로 지정")
        nxt = st.pick_next(state, exclude=cur, bucket=bucket)
        if nxt is None:
            st.save_state(state)
            reset_at = st.earliest_reset(state, bucket=bucket)
            when = time.strftime("%m월 %d일 %H:%M", time.localtime(reset_at)) if reset_at else "알 수 없음"
            print(_exhausted_message(state, bucket, when, lead=""))
            print(st.describe(state))
            return 2
        if not _switch(nxt, leaving=cur):
            st.save_state(state)
            return 1
        st.set_current(state, nxt, f"{cur or '?'} → {nxt} (사람 지정)")
        st.save_state(state)
    print(f"'{cur or '?'}' → '{nxt}' 계정으로 교체했습니다.")
    print("열려있는 claude 를 종료하고 다시 실행하면 적용됩니다.")
    return 0


def intended_model(argv=None) -> str | None:
    """켜려는 세션이 쓸 모델 이름. 못 알아내면 None(그러면 계정 전체 한도만 본다).

    두 자리에서 찾는다. 1순위는 사람이 직접 준 `--model`(그 판이 확실하다),
    2순위는 settings.json 에 고정된 값(대부분 이쪽이다 — cc 는 보통 인자 없이 친다).
    """
    args = list(argv or [])
    for i, a in enumerate(args):
        if a == "--model" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--model="):
            return a.split("=", 1)[1]
    for path in (Path.home() / ".claude" / "settings.json",
                 Path.home() / ".claude" / "settings.local.json"):
        try:
            got = json.loads(path.read_text(encoding="utf-8")).get("model")
        except Exception:
            continue
        if isinstance(got, str) and got.strip():
            return got.strip()
    return None


def ensure_usable(model: str | None = None) -> int:
    """켜기 직전 확인. 지금 계정이 이미 다 찼으면 켜기 전에 다음 계정으로 바꾼다.

    선제 전환이 아니다. 잔여가 조금이라도 있으면 그대로 둔다(거절 기반 순환은 그대로).
    사용량을 못 읽으면(네트워크 실패 등) 아무것도 안 한다 — 멀쩡한 계정을 막으면 안 되므로.
    전환할 때는 그 계정을 실제 리셋 시각까지 쉬게 표시하므로 세 머신이 같이 건너뛴다.

    `model` 을 주면 그 모델이 걸리는 칸까지 본다. 계정 전체 주간이 60% 여유인데
    Fable 만 100% 소진인 경우가 실제로 있었고(2026-08-12), 그때 계정을 통째로 막으면
    남은 Opus 몫을 버리게 되므로 **그 칸만 막고** 다음 계정으로 넘긴다.
    Opus 로 켜는데 여유가 있으면 아무것도 안 바뀐다(그 계정 그대로 쓴다).
    """
    if usage_check is None:
        return 0
    bucket = usage_check.bucket_for_model(model)
    started_from = None   # 처음 쓰던 계정 (알림·안내용)
    first_kind = ""       # 그 계정이 무슨 한도로 막혔는지
    landed = None         # 최종으로 고른 계정
    with st.with_lock():
        state = st.prune_blocked(st.load_state())
        # 앞순위 계정이 풀렸으면 먼저 되돌린다(대화형 cc 도 이 경로로 켜진다).
        # 되돌린 계정이 아직 다 찬 상태면 바로 아래 확인이 다시 넘겨준다.
        reclaimed = _reclaim_locked(state, bucket)
        # 다음 계정도 이미 소진돼 있을 수 있다. 쓸 만한 계정이 나올 때까지 이어서 건너뛴다.
        for _ in range(len(st.account_labels()) + 1):
            cur = state.get("current")
            if not cur or cur not in st.saved_labels():
                break
            u = usage_check.get_usage_for(BASE / "creds" / f"{cur}.json", is_current=True)
            if not u:
                break  # 못 읽었으면 그대로 쓴다
            st.note_weekly_reset(state, cur, u.get("weekly_reset"))  # 순서 기준으로 쓰인다
            st.note_usage(state, cur, u)  # 계정 목록 화면에 모델 칸까지 남기려고
            # 진짜 소진(100% 사용)일 때만. bucket 을 주면 그 모델 칸까지 본다.
            kind = usage_check.which_limit(u, threshold=100.0, bucket=bucket)
            if not kind:
                break
            if kind.startswith("model:"):
                # 그 모델만 바닥났다. 계정은 살려 두고 그 칸만 막는다 —
                # 통째로 막으면 이 계정으로 아직 쓸 수 있는 다른 모델까지 버린다.
                kind_txt = bucket  # 뒤에 붙는 문구가 '한도 소진' 이라 여기선 이름만
                until = usage_check.scoped_reset(u, bucket)
                st.mark_scoped_blocked(state, cur,
                                       bucket, until or (time.time() + limit_detect.DEFAULT_COOLDOWN))
            else:
                kind_txt = "주간" if kind == "weekly" else "세션(5시간)"
                until = u.get("weekly_reset") if kind == "weekly" else u.get("session_reset")
                st.mark_blocked(state, cur, until or (time.time() + limit_detect.DEFAULT_COOLDOWN),
                                f"{kind_txt} 한도 소진(켜기 전 확인)")
            nxt = st.pick_next(state, exclude=cur, bucket=bucket)
            if nxt is None:
                st.save_state(state)
                reset_at = st.earliest_reset(state, bucket=bucket)
                when = time.strftime("%m월 %d일 %H:%M", time.localtime(reset_at)) if reset_at else "알 수 없음"
                print(f"[켜기 전 확인] '{cur}' {kind_txt} 한도 소진. 갈아탈 계정이 없습니다. "
                      + _exhausted_message(state, bucket, when, lead=""))
                return 2
            if not _switch(nxt, leaving=cur):
                st.save_state(state)
                return 1
            if started_from is None:
                started_from, first_kind = cur, kind_txt
            landed = nxt
            st.set_current(state, nxt, f"{cur} → {nxt} (켜기 전 확인)")
            log(f"켜기 전 확인: '{cur}' {kind_txt} 소진 → '{nxt}' 로 교체")
        st.save_state(state)
    if not started_from:
        if reclaimed:
            print(f"[켜기 전 확인] 앞순위 '{reclaimed}' 계정이 풀려 그쪽으로 되돌렸습니다.")
        return 0
    print(f"[켜기 전 확인] '{started_from}' {first_kind} 한도 소진 → '{landed}' 계정으로 바꿔서 켭니다.")
    _notify_switch(started_from, landed, "limit", state, bucket)  # 전송은 잠금 밖에서
    return 0


def _reclaim_locked(state: dict, bucket: str | None = None):
    """(잠금 안에서) 앞순위 계정이 풀렸으면 그쪽으로 되돌린다. 바꿨으면 새 라벨, 아니면 None.

    세션(5시간) 한도로 잠깐 비켜났던 계정은 5시간 뒤 막힘이 저절로 풀리는데, 예전에는
    지금 계정이 막힐 때까지 안 돌아왔다. 그 동안 뒷순위 계정의 주간 잔여가 깎이고
    앞순위 계정의 남은 양은 안 쓰인 채 초기화되어 사라졌다(순차 소진의 목적과 반대).
    사용량 조회는 하지 않는다 — 잦으면 429 가 난다. 막힘 목록만 보고 정한다.
    되돌린 계정이 사실 아직 소진 상태여도 손해는 헛걸음 한 번이고, 그 거절이 정확한
    해제 시각으로 다시 막아준다. 그 반복은 RECLAIM_MIN_GAP 간격이 막는다.
    `bucket`(이번에 쓰려는 모델 칸)을 주면 그 칸이 막힌 계정으로는 안 되돌린다 —
    안 그러면 되돌림 → 그 칸 소진 재발견 → 재전환 + 알림이 10분마다 반복된다
    (2026-08-14 실사고, 알림 반복의 원인).
    상태 저장은 부르는 쪽이 한다(이미 잠금을 쥐고 있는 흐름에서 쓰라고 나눠둔 함수다).
    """
    cur = state.get("current")
    if not st.may_reclaim(state):
        return None
    nxt = st.better_candidate(state, cur, bucket=bucket)
    if nxt is None:
        return None
    st.note_reclaim(state)  # 교체가 실패해도 기록해 재시도 폭주를 막는다
    if not _switch(nxt, leaving=cur):
        return None
    st.set_current(state, nxt, f"{cur} → {nxt} (앞순위 계정이 풀려 되돌림)")
    log(f"'{cur}' 에서 앞순위 '{nxt}' 로 되돌립니다(순차 소진)")
    return nxt


def _reclaim_if_better(state: dict, cur: str, bucket: str | None = None):
    """지금 계정이 안 막혔어도 순서상 앞선 계정이 풀려 있으면 그쪽으로 되돌린다."""
    if not st.may_reclaim(state) or st.better_candidate(state, cur, bucket=bucket) is None:
        return cur  # 잠금을 잡기 전 값싼 확인. 평소에는 여기서 끝난다
    with st.with_lock():
        s = st.prune_blocked(st.load_state())
        nxt = _reclaim_locked(s, bucket)
        st.save_state(s)
        return nxt or s.get("current") or cur


def _switch_if_blocked(state: dict, bucket: str | None = None):
    """지금 계정이 이미 막혀 있으면 부르기 전에 갈아탄다. 막힌 계정 목록만 보고 판단한다.

    막힘은 세 머신이 공유하므로, 다른 머신이 한도로 막아둔 계정을 이 머신이 그대로 쓰다가
    똑같이 거절받는 헛걸음을 없앤다. 사용량 조회를 하지 않아 값싸고, 알림도 안 보낸다
    (막을 때 이미 한 번 나갔다).
    `bucket` 을 주면 이번 호출이 쓰는 모델 칸이 막힌 경우에도 미리 갈아탄다 —
    그 계정으로 나가 봐야 거절 한 번만 낭비된다(칸 막힘은 이 머신에서 이미 겪은 것이다).
    막히지 않았으면 그 다음으로, 앞순위 계정이 풀렸는지만 보고 되돌린다.
    """
    cur = state.get("current")
    if not cur:
        return cur
    if not st.is_blocked(state, cur) and not st.is_scoped_blocked(state, cur, bucket):
        return _reclaim_if_better(state, cur, bucket)
    with st.with_lock():
        s = st.prune_blocked(st.load_state())
        cur = s.get("current")
        if not cur or (not st.is_blocked(s, cur)
                       and not st.is_scoped_blocked(s, cur, bucket)):
            return cur  # 그 사이 다른 프로세스가 바꿨다
        nxt = st.pick_next(s, exclude=cur, bucket=bucket)
        if nxt is None or not _switch(nxt, leaving=cur):
            return cur  # 갈아탈 데가 없으면 기존 흐름(거절 → 소진 알림)이 처리한다
        st.set_current(s, nxt, f"{cur} → {nxt} (이미 막힌 계정이라 미리 교체)")
        st.save_state(s)
        log(f"'{cur}' 는 이미 막혀 있어 부르기 전에 '{nxt}' 로 교체")
        return nxt


def _cred_alive(label: str | None) -> bool:
    """그 계정 저장본이 지금 살아 있는지 서버에 물어본다.

    True 만 뜻이 있다(확실히 살아 있으니 막으면 안 된다). 조회가 실패했다고 죽은 것은
    아니다 — 출입증(access token)은 8시간짜리라 저장본이 멀쩡해도 조회가 안 될 수 있다.
    그래서 확인 못 한 경우는 False 로 돌려 기존 판정(막고 넘김)을 그대로 따르게 한다.
    """
    if not label:
        return False
    path = BASE / "creds" / f"{label}.json"
    # 1순위: 서버가 답하면 확실히 살아 있다.
    if usage_check:
        try:
            if usage_check.get_usage(path):
                return True
        except Exception:
            pass
    # 2순위: 서버가 안 답해도 **재발급 열쇠 만료일**이 남았으면 저장본은 멀쩡하다.
    # 출입증은 8시간짜리라 자주 만료된다. 그걸 죽음으로 보면 멀쩡한 계정이 줄줄이 막힌다
    # (2026-07-29 실사고: 1초 사이에 보호가 뚫려 네 계정이 다 막혔다).
    try:
        import json as _json
        import time as _time
        o = _json.loads(path.read_text(encoding="utf-8")).get("claudeAiOauth", {})
        exp_ms = int(o.get("refreshTokenExpiresAt") or 0)
        if exp_ms and exp_ms / 1000 > _time.time():
            return True
    except Exception:
        pass
    return False


def _keychain_account() -> str | None:
    """맥 열쇠꾸러미에 남아 있는 클로드 자격 항목의 **계정 이름**. 없으면 None.

    **자격 값은 읽지 않는다** — 항목이 있는지와 어느 계정 이름으로 걸려 있는지만 본다
    (값 읽기는 화면 잠금해제가 필요하고, 읽으면 그 값이 기록에 남는다).
    계정 이름을 같이 보는 이유: 지울 때 쓰는 명령에 그 이름이 들어가야 실제로 지워진다.
    이름을 못 읽으면 로그인 사용자 이름으로 떨어진다.

    순환기는 자격 파일(~/.claude/.credentials.json)을 갈아끼우는 설계인데, 화면 세션에서 도는
    claude(=launchd 봇)는 열쇠꾸러미 값을 먼저 읽는다. 항목이 남아 있으면 계정을 아무리
    갈아끼워도 봇 자리에는 그 값만 나가고, 그 값이 만료되는 순간 **모든 계정이 로그인 거절**을
    받는다. 2026-07-29 실사고: Mac 봇 전부가 세 시간 반 멈췄고, 멀쩡한 B 계정이 만료로 오판됐다.
    원격 접속 창에서는 열쇠꾸러미가 잠겨 파일 자격이 쓰이므로 손으로 부르면 멀쩡해 보인다.
    """
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        for line in (r.stdout or "").splitlines():
            if '"acct"' in line and '=' in line:
                name = line.split("=", 1)[1].strip().strip('"')
                if name:
                    return name
        return os.environ.get("USER") or "kwon"
    except Exception:
        return None


def _read_keychain_cred(account: str):
    """열쇠꾸러미 값을 이 프로세스 안으로만 읽어 온다. 못 읽으면 None.

    못 읽는 것은 고장이 아니다 — 원격 접속 창이나 화면 잠금 상태의 정상 결과다.
    값은 화면·명령 인자를 거치지 않고, **디스크에도 남기지 않는다**. /tmp 파일로 받던
    옛 방식은 그 파일이 기본 권한(0644)이라 다른 프로그램이 읽을 수 있었다
    (코덱스 검수 2026-07-30, 재현됨). 낡았는지만 보면 되므로 보관할 이유도 없다.
    """
    import json as _json

    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials",
             "-a", account, "-w"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    try:
        return _json.loads((r.stdout or "").strip())
    except Exception:
        return None


def _keychain_verdict(account: str | None) -> str:
    """열쇠꾸러미에 든 값이 지금 쓸 수 있는 값인지 본다.

    "none"       항목 자체가 없다
    "unreadable" 값을 못 읽는다(원격 접속 창·화면 잠금). **판정하지 않는다.**
    "fresh"      재발급 열쇠가 살아 있다. 그림자가 아니라 오히려 최신본이다.
    "stale"      재발급 열쇠가 만료됐다. 이것만 진짜 그림자다.

    **항목이 있다는 것만으로 옛 자격이라 단정하면 안 된다(2026-07-30 실측).** 화면 세션에서
    도는 봇이 자격을 갱신하면 그 최신 값은 열쇠꾸러미에만 저장되므로, 잘 돌고 있을 때도
    항목은 늘 존재한다. 그 상태에서 지우라고 알리면 유일하게 살아 있는 자격을 버리게 된다
    (2026-07-29 오후 실사고: 지운 뒤 여덟 시간 만에 네 계정이 한꺼번에 막혔다).
    """
    if not account or sys.platform != "darwin":
        return "none"
    obj = _read_keychain_cred(account)
    if not isinstance(obj, dict):
        return "unreadable"
    o = obj.get("claudeAiOauth") or {}
    try:
        exp = float(o.get("refreshTokenExpiresAt") or 0)
    except Exception:
        exp = 0
    return "fresh" if exp / 1000 > time.time() else "stale"


def _mirror_now() -> bool:
    """열쇠꾸러미의 최신 자격을 자격 파일·계정 저장본으로 되비춘다. 맞춘 게 있으면 True.

    두 자리가 어긋나 나는 거절은 지우기가 아니라 되비추기로 고친다(cred_mirror.py 참조).
    """
    try:
        import cred_mirror
    except Exception:
        return False
    try:
        return cred_mirror.sync() is True
    except Exception:
        return False


def _note_auth_fail(state: dict, label: str | None, now: float | None = None) -> list:
    """로그인 거절을 기록하고, 최근 창 안에서 거절받은 계정 라벨을 돌려준다."""
    now = now or time.time()
    fails = {
        k: v for k, v in (state.get("auth_fails") or {}).items()
        if isinstance(v, (int, float)) and now - v <= AUTH_FAIL_WINDOW
    }
    if label:
        fails[label] = now
    state["auth_fails"] = fails
    return sorted(fails)


def _cred_place_check(state: dict, label: str | None) -> tuple:
    """이번 거절이 '계정이 죽어서'가 아니라 '자격 읽는 자리가 깨져서'인지 본다.

    돌려주는 값: (사유 문장 또는 빈 문자열, 최근 거절받은 계정 라벨들, 열쇠꾸러미 계정 이름).
    사유가 있으면 그 계정을 재우면 안 된다 — 어느 계정으로 바꿔도 똑같이 거절받는 상태라
    멀쩡한 계정까지 줄줄이 잠긴다.
    """
    recent = _note_auth_fail(state, label)
    acct = _keychain_account()
    verdict = _keychain_verdict(acct) if acct else "none"
    if verdict == "stale":
        return "열쇠꾸러미에 옛 자격이 남아 있습니다", recent, acct
    if verdict == "fresh":
        # 지울 값이 아니다. 최신본이 열쇠꾸러미에만 있고 파일 쪽이 뒤처진 상태다.
        return "자격이 파일과 열쇠꾸러미로 어긋나 있습니다", recent, acct
    # "unreadable" 은 원격 접속 창의 정상 상태다. 여기서는 판정하지 않는다.
    if len(recent) >= 2:
        return f"짧은 시간에 계정 {len(recent)}개가 한꺼번에 거절받았습니다", recent, None
    return "", recent, None


def _warn_cred_place(label: str | None) -> str:
    """계정을 갈아타기 전 단계에서도 열쇠꾸러미 그림자를 잡아 고치거나 알린다.

    로그인 거절 문구가 'auth'(로그인 자체가 안 됨)로 분류되면 계정 교체 경로를 안 타서
    _cred_place_check 가 아예 안 돌고, 봇은 조용히 실패한 채 사람도 모른다(코덱스 검수 2026-07-29).
    여기서는 잠금을 잡지 않으므로 거절 기록은 남기지 않고 열쇠꾸러미만 본다.

    돌려주는 값: "fixed"(스스로 고쳤으니 다시 해보라) / "warned"(사람이 지워야 한다) / ""(해당 없음).
    """
    acct = _keychain_account()
    if not acct:
        return ""
    verdict = _keychain_verdict(acct)
    if verdict == "unreadable":
        # 원격 접속 창이라 값을 못 읽는 것뿐이다. 여기서 알리면 잘 도는 봇을 두고
        # 사람에게 "지우라"고 시키게 된다(2026-07-30 오탐). 조용히 넘어간다.
        return ""
    why = ("열쇠꾸러미에 옛 자격이 남아 있습니다" if verdict == "stale"
           else "자격이 파일과 열쇠꾸러미로 어긋나 있습니다")
    log(f"로그인 거절. 그런데 {why}. 계정 문제가 아니라 자격을 읽는 자리 문제로 봅니다.")
    labels = [label] if label else []
    res = _keychain_fix(acct, label)
    if res == "mirrored":
        # 되비추기로 조용히 맞췄다. 사람이 할 일이 없으니 알리지 않는다.
        return "fixed"
    if res == "in-sync":
        # 자격 자리는 멀쩡하다. 여기서 알리면 원인도 아닌 것을 사람에게 떠넘기게 된다.
        return ""
    if res == "fixed":
        if st.claim_notice(f"cred-fixed:{int(time.time() // 3600)}"):
            _notify_cred_fixed(labels)
        return "fixed"
    # "half" 면 이미 저장본 재설치 안내를 보냈다. 열쇠꾸러미를 또 지우라고 하면 안 된다.
    if res != "half" and st.claim_notice(f"cred-place:{int(time.time() // 3600)}"):
        _notify_cred_place(why, labels, acct)
    return "warned"


def _keychain_rescue(account: str, label: str | None) -> bool:
    """열쇠꾸러미 값을 따로 보관해 둔다. 보관했으면 True.

    **계정 저장본(creds/<라벨>.json)은 절대 덮지 않는다.** 열쇠꾸러미 값이 어느 계정 것인지
    알 수 없기 때문이다. 지금 라벨이 A 여도 열쇠꾸러미에는 B 로 로그인한 값이 들어 있을 수
    있고, 그걸 A 저장본에 쓰면 멀쩡한 A 를 잃는다(코덱스 검수 2026-07-29).
    그래서 `creds/_keychain-rescued-<시각>.json` 으로 따로 남기고, 쓸지 말지는 사람이 정한다.

    **지우기 전에 반드시 이걸 먼저 한다.** 클로드는 자격을 새로 받을 때마다 재발급 열쇠를
    한 번 쓰고 버리는데(회전), 화면 세션에서는 그 최신 값이 열쇠꾸러미에 저장된다.
    백업 없이 항목만 지우면 최신 재발급 열쇠가 통째로 사라져, 지금 출입증이 만료되는 순간
    **모든 계정이 되살아나지 못한다**(2026-07-29 실사고: 네 계정이 한꺼번에 그렇게 막혔다).

    값은 화면·명령 인자를 거치지 않고 파일로만 흐른다(save_account.py 와 같은 방식).
    원격 접속 창에서는 열쇠꾸러미가 잠겨 있어 읽기가 실패하는데, 그때는 False 를 돌려
    **지우기를 취소**시킨다.
    """
    import json as _json

    # 보관 자리는 pool_state 가 정한 곳을 따른다. BASE 를 그대로 쓰면 시험이 운영 폴더에
    # 파일을 만든다(2026-07-29 실사고: 시험이 A 저장본을 가짜 값으로 밀었다).
    # 계정 저장본 폴더 **바로 밑에 두면 안 된다** — 여러 코드가 그 폴더의 *.json 을
    # 계정 목록으로 훑기 때문에 보관 파일이 계정 하나로 잡힌다(코덱스 검수 2026-07-29).
    creds = Path(getattr(st, "CREDS_DIR", BASE / "creds"))
    keep = creds / "rescued"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = keep / f"keychain-{stamp}.json"
    tmp = keep / f".keychain-{stamp}.tmp"
    try:
        keep.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials",
                 "-a", account, "-w"],
                stdout=fh, stderr=subprocess.DEVNULL, timeout=20,
            )
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            log("열쇠꾸러미 값을 읽지 못해 지우기를 취소합니다(화면 잠금 상태일 수 있습니다).")
            return False
        raw = tmp.read_text(encoding="utf-8").strip()
        o = _json.loads(raw).get("claudeAiOauth", {})   # 형식만 본다. 값은 로그에 안 남긴다
        if not o.get("refreshToken"):
            tmp.unlink(missing_ok=True)
            log("열쇠꾸러미 값에 재발급 열쇠가 없어 지우기를 취소합니다.")
            return False
        os.replace(tmp, dest)
        try:
            os.chmod(dest, 0o600)
        except Exception:
            pass
        log(f"열쇠꾸러미에 있던 자격을 creds/rescued/{dest.name} 으로 따로 보관했습니다"
            f"(어느 계정 것인지는 사람이 확인). 지금 계정 저장본은 건드리지 않았습니다.")
        return True
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        log(f"열쇠꾸러미 값 보관 실패({type(e).__name__}). 지우기를 취소합니다.")
        return False


def _keychain_fix(account: str | None, label: str | None = None) -> str:
    """열쇠꾸러미에 남은 클로드 자격을 따로 보관한 뒤 지운다.

    돌려주는 값: "fixed"(지우고 자격 파일까지 다시 깔았다) / "mirrored"(지울 값이 아니어서
    되비추기로 두 자리를 맞췄다) / "half"(지우긴 했는데 다시 깔기 실패, 사람이 저장본을
    깔아야 한다) / ""(아무것도 안 했다).

    **지우기 전에 값이 실제로 낡았는지부터 본다.** 항목이 있다는 것만으로 지우면, 화면
    세션 봇이 방금 갱신해 넣은 최신 자격을 버리게 된다(2026-07-30 실측).

    **옮기기에 실패하면 지우지 않는다** — 최신 재발급 열쇠를 잃는 사고를 막는 핵심 규칙이다.
    지우는 건 열쇠꾸러미 항목 하나뿐이고, 자격 파일은 지운 뒤 저장본으로 다시 채운다.
    로그인 거절을 실제로 받은 뒤에만 부른다. 잘 돌 때는 절대 지우지 않는다.
    한 시간에 한 번만 지운다. 지운 직후 또 걸리면 원인이 다른 데 있다는 뜻이라, 그때는
    손대지 않고 사람에게 넘긴다(지우고 다시 생기고를 반복하는 것을 막는다).
    """
    if not account or sys.platform != "darwin":
        return ""
    verdict = _keychain_verdict(account)
    if verdict == "unreadable":
        # 값을 못 읽는 자리에서는 낡았는지 알 수 없다. 모르면 손대지 않는다.
        return ""
    if verdict == "fresh":
        # 지울 값이 아니다. 최신본이 열쇠꾸러미에만 있고 파일 쪽이 뒤처진 것뿐이다.
        if _mirror_now():
            log("열쇠꾸러미 값이 최신이라 지우지 않았습니다. "
                "자격 파일과 계정 저장본을 그 값으로 맞췄습니다.")
            return "mirrored"
        # 맞출 것이 없었다 = 세 자리가 이미 같은 값이다. 열쇠꾸러미 탓이 아니라는 뜻이므로
        # 사람을 부르지 않는다. 거절 원인은 다른 데 있으니 평소 계정 처리로 내려보낸다.
        # (이걸 ""(아무것도 못 함)로 두면 잘 맞아 있는 상태에서 오히려 알림이 나간다.)
        return "in-sync"
    try:
        if KEYCHAIN_STAMP.exists() and time.time() - KEYCHAIN_STAMP.stat().st_mtime < KEYCHAIN_FIX_COOLDOWN:
            return ""
    except Exception:
        pass
    # 지우기 전에 값을 따로 보관한다. 못 보관하면 지우지 않는다.
    if not _keychain_rescue(account, label):
        return ""
    try:
        r = subprocess.run(
            ["security", "delete-generic-password", "-s", "Claude Code-credentials", "-a", account],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            log("열쇠꾸러미의 옛 자격을 지우지 못했습니다. 사람이 지워야 합니다.")
            return ""
    except Exception as e:
        log(f"열쇠꾸러미 정리 실패({type(e).__name__}). 사람이 지워야 합니다.")
        return ""
    try:
        KEYCHAIN_STAMP.write_text(str(int(time.time())), encoding="utf-8")
    except Exception:
        pass
    # 저장본을 자격 파일 자리에 다시 깔아 준다(떠나는 계정은 주지 않는다. 망가진 값을
    # 저장본에 되저장하면 멀쩡한 자격을 덮어쓴다).
    # 다시 깔기에 실패하면 고쳐진 게 아니다. 성공했다고 알리면 안 된다(코덱스 검수).
    if label and not _switch(label):
        log("자격 파일을 저장본으로 다시 깔지 못했습니다. 사람 손이 필요합니다.")
        if st.claim_notice(f"cred-restore:{int(time.time() // 3600)}"):
            _notify_restore_failed(label)
        return "half"   # 지우기는 됐다. 열쇠꾸러미를 또 지우라고 안내하면 안 된다
    log("열쇠꾸러미에 남아 있던 옛 자격을 지웠습니다. 이제 계정 저장본으로 나갑니다.")
    return "fixed"


def _notify_restore_failed(label: str) -> None:
    """열쇠꾸러미는 정리했는데 저장본을 자격 자리에 다시 깔지 못했을 때."""
    notify(
        "클로드 로그인을 반쯤 고쳤습니다. 마지막 한 걸음이 사람 손을 기다립니다.\n\n"
        "무슨 일: 이 컴퓨터 열쇠꾸러미에 남아 거절을 부르던 옛 자격은 치웠습니다. "
        f"그런데 '{label}' 계정 저장본을 쓰는 자리에 다시 깔지 못했습니다. "
        "그래서 봇들이 아직 로그인 거절을 받습니다.\n\n"
        "할 일: Mac 에서 아래 한 줄을 실행하세요.\n"
        f"  python3 ~/projects/ai-rotator/switch_account.py {label}\n\n"
        "그래도 안 되면 개발 봇에 \"클로드 로그인 고쳐줘\" 라고 답장하세요."
    )


def _notify_cred_fixed(labels: list) -> None:
    """스스로 고쳤을 때 결과만 알린다. 사람이 할 일은 없다."""
    notify(
        "클로드 로그인이 잠깐 막혔다가 스스로 풀렸습니다.\n\n"
        "무슨 일: 이 컴퓨터 열쇠꾸러미에 옛 자격이 남아 있어 어느 계정으로 바꿔도 거절을 "
        f"받던 상태였습니다. 거절받은 계정: {', '.join(labels) or '확인 안 됨'}. "
        "그 옛 자격만 지우고 계정 저장본으로 다시 연결했습니다.\n\n"
        "하실 일: 없습니다. 표지 만들기·칼럼 초안 같은 봇은 그대로 돌아갑니다. "
        "지운 값은 ai-rotator/creds/rescued/keychain-*.json 에 그대로 남겨 뒀습니다.\n\n"
        "혹시 같은 알림이 자꾸 오면 개발 봇에 \"클로드 로그인 자꾸 끊겨\" 라고 답장하세요."
    )


def _notify_cred_place(why: str, labels: list, account: str | None = None) -> None:
    """자격 읽는 자리가 깨졌을 때 사람에게 할 일을 알린다(계정 문제와 구분해서).

    account 는 열쇠꾸러미 항목에 걸린 계정 이름이다. 지우는 명령에 그 이름이 그대로 들어가야
    실제로 지워진다(찾을 때 쓴 조건과 지울 때 쓴 조건이 다르면 지워도 항목이 남는다).
    **지우라는 명령은 그 값이 낡았다고 실제로 확인했을 때만 넣는다.** 확인 없이 넣으면,
    봇 자리에서 잘 돌고 있는 최신 자격을 사람 손으로 버리게 된다(2026-07-30 오탐).
    그래서 account 는 _keychain_verdict 가 "stale" 을 돌려준 경우에만 넘어온다.
    """
    import shlex

    head = (
        "클로드 로그인이 계정 문제가 아니라 이 컴퓨터에서 막혀 있습니다.\n\n"
        f"무슨 일: {why}. 그래서 어느 계정으로 바꿔도 로그인 거절만 돌아옵니다. "
        f"거절받은 계정: {', '.join(labels) or '확인 안 됨'}. "
        "이 상태면 표지 만들기·칼럼 초안·질문 답변 같은 Mac 봇이 다 멈춥니다.\n\n"
    )
    # 부르는 쪽이 어떤 사정으로 넘겼든, 보내기 직전에 낡았는지 한 번 더 확인한다.
    # 이 알림 한 통이 사람 손으로 자격을 지우게 만들기 때문에 방어를 여기 둔다.
    if account and _keychain_verdict(account) == "stale":
        # 계정 이름에 공백이 있으면 인용 없이는 -a 값이 잘려 실제로 안 지워진다(코덱스 검수).
        who = shlex.quote(account)
        body = (
            "할 일: Mac 에서 아래 한 줄을 실행하세요. 지워지는 건 이미 만료가 확인된 값이고, "
            "계정 저장본은 그대로 남습니다.\n"
            f'  security delete-generic-password -s "Claude Code-credentials" -a {who}\n\n'
        )
    else:
        # 열쇠꾸러미가 원인이라는 근거가 없다. 아무것도 지우게 시키지 않는다.
        # 이 갈래는 "여러 계정이 짧은 시간에 거절" 만으로 온 경우가 대부분인데,
        # 그중엔 자리 문제가 아니라 **그 계정 저장본이 진짜 죽은 것** 도 섞여 있다
        # (2026-08-03 Mac D. 저장본을 태워 보기 전에는 둘을 못 가른다).
        # 그래서 지울 것을 시키는 대신, 가려내는 일을 시킬 수 있게 적는다.
        body = (
            "할 일: 지금은 지울 것이 없습니다. 계정 저장본이 죽었을 수도 있는데 "
            "그건 하나씩 태워 봐야 알 수 있습니다. 개발 봇에 "
            "\"클로드 계정 태워서 확인해줘\" 라고 답장하시면 계정을 하나씩 태워 "
            "죽은 것을 찾아 다시 로그인해 드립니다.\n\n"
        )
    notify(head + body + "그래도 안 되면 개발 봇에 \"클로드 로그인 고쳐줘\" 라고 답장하세요.")


def _quarantine_cred(label: str | None) -> bool:
    """죽음이 굳어진 저장본을 creds/dead/ 로 옮겨 순환에서 뺀다(재로그인하면 자동 복귀).

    pick_next 는 저장본 있는 라벨만 고르므로 옮기는 것만으로 순환에서 빠진다.
    mark_blocked 를 쓰지 않는 이유: 막힘은 세 머신이 공유하는데 저장본 죽음은 이 머신만의
    문제다. 공유로 올리면 멀쩡한 다른 머신의 같은 라벨까지 막힌다.
    """
    if not label:
        return False
    src = Path(st.CREDS_DIR) / f"{label}.json"
    if not src.exists():
        return False
    dead = Path(st.CREDS_DIR) / "dead"
    try:
        dead.mkdir(parents=True, exist_ok=True)
        src.replace(dead / src.name)
    except OSError:
        return False
    return True


def _relogin_hint(label: str) -> str:
    """(머신 이름, 재로그인 명령) 짝. 실행 위치는 항상 본체의 ai-rotator 폴더다."""
    try:
        import shared_state as ss
        where = ss.host_label(ss._hostname())
    except Exception:
        where = "?"
    if where == "Mac":
        cmd = f"python relogin.py --remote macmini {label}"
    elif where == "winserver":
        cmd = f"python relogin.py --remote winserver {label}"
    else:
        cmd = f"python relogin.py {label}"
    return where, cmd


def _notify_cred_dead(label: str) -> None:
    """저장본이 죽어 격리했을 때, 하루 한 번만 할 일과 함께 알린다."""
    if not st.claim_notice(f"cred-dead:{label}:{int(time.time() // 86400)}"):
        return
    where, cmd = _relogin_hint(label)
    notify(
        f"클로드 '{label}' 계정 로그인이 거듭 거절돼 순환에서 뺐습니다({where} 저장본만 격리).\n\n"
        f"무슨 일: 이 컴퓨터에 저장된 '{label}' 로그인이 폐기된 옛 열쇠가 됐습니다. "
        "다른 계정들은 그대로 돌아가고, 이 알림은 같은 일로 하루 한 번만 옵니다.\n\n"
        "할 일: 본체 ai-rotator 폴더에서 아래 한 줄을 실행하고 브라우저 승인 한 번이면 "
        "자동으로 저장·복귀합니다.\n"
        f"  {cmd}"
    )


def _clear_auth_rejects(label: str | None) -> None:
    """호출이 성공하면 그 계정의 거절 횟수를 지운다.

    셀 거리가 없으면 파일을 안 건드린다 — 봇 수십 곳이 매 호출마다 상태를 쓰면
    잠금 다툼만 늘어난다. 지우다 실패해도 호출 결과에는 영향을 주지 않는다.

    단 거절이 쌓여 있는 동안에는 성공이 '봇과 같은 자리'에서 났을 때만 지운다.
    봇 자리(launchd)는 열쇠꾸러미 자격을 쓰고 원격 접속(ssh) 자리는 파일 자격을
    쓰므로, 열쇠꾸러미 값을 못 읽는 자리의 성공은 봇 자리 거절의 반증이 못 된다.
    2026-08-10 실사고: 봇 자리가 로그인 거절을 받는 동안 진단용 ssh 호출이
    성공하며 거절 기록을 지워, 3분 강제 교체가 눈앞에서 무력화되고 봇 정지가
    이어졌다. 이 검사는 거절이 쌓여 있을 때만 돌아 평소 호출엔 비용이 없다.
    """
    if not label:
        return
    try:
        if not st.auth_reject_count(st.load_state(), label):
            return
        if sys.platform == "darwin":
            # 항목 자체가 없으면(acct None) 파일이 모든 자리의 원본이라 평소대로 지운다
            # (코덱스 지적: None 을 원격 자리로 오인하면 기록이 영영 안 지워져, 뒤에
            # 스치는 거절 한 번에 강제 교체가 잘못 발동한다). 항목은 있는데 값을 못
            # 읽는 자리(ssh·화면 잠금)만 봇 자리와 자격 경로가 갈리므로 안 지운다.
            acct = _keychain_account()
            if acct and _read_keychain_cred(acct) is None:
                log(f"'{label}' 호출은 성공했지만 여기는 열쇠꾸러미를 못 읽는 원격 "
                    "자리라 거절 기록을 지우지 않습니다(봇 자리는 다른 자격을 씁니다).")
                return
        with st.with_lock():
            state = st.load_state()
            st.clear_auth_reject(state, label)
            st.save_state(state)
    except Exception:
        pass


def _rotate_after_failure(used_label: str | None, verdict: str, out: str, err: str,
                          bucket: str | None = None) -> str:
    """실패한 계정을 쉬게 하고 다음 계정으로 교체. 교체된 라벨을 반환.

    다른 프로세스가 이미 계정을 바꿨으면 교체하지 않고 그 라벨을 그대로 쓴다.
    `bucket` 은 이번 호출이 쓰던 모델 칸이다. 그 칸만 바닥난 거절이면 계정을 통째로
    막지 않고 그 칸만 막아, 같은 계정으로 다른 모델은 계속 쓰게 둔다.
    """
    with st.with_lock():
        state = st.prune_blocked(st.load_state())
        current = state.get("current")

        # 내가 쓰던 계정이 이미 다른 프로세스에 의해 교체됐으면 그대로 재시도
        if used_label is not None and current is not None and current != used_label:
            log(f"이미 '{current}' 로 교체돼 있어 그대로 재시도합니다.")
            return current

        # 원인 모를 실패로 넘어온 경우엔 계정을 막지 않는다. 한도인지 확실치 않아서다.
        if used_label and verdict != "error":
            if verdict == "limit":
                # 세션(5시간)인지 주간인지에 맞는 리셋 시각을 실시간 사용량에서 읽는다.
                # 못 읽으면 응답 문구 파싱으로 떨어진다.
                until = None
                kind = None
                if usage_check:
                    u = usage_check.get_usage(BASE / "creds" / f"{used_label}.json")
                    if u:
                        st.note_weekly_reset(state, used_label, u.get("weekly_reset"))
                    kind = usage_check.which_limit(u, bucket=bucket)
                    if kind == "weekly" and u.get("weekly_reset"):
                        until = u["weekly_reset"]
                    elif kind == "session" and u.get("session_reset"):
                        until = u["session_reset"]
                    elif kind and kind.startswith("model:"):
                        until = usage_check.scoped_reset(u, bucket)
                if until is None:
                    until = limit_detect.reset_epoch(out, err)
                left = int(until - time.time())
                if kind and kind.startswith("model:"):
                    # 그 모델만 바닥났다. 계정은 살려 둔다 — 통째로 막으면 이 계정으로
                    # 아직 쓸 수 있는 다른 모델 몫까지 같이 버린다.
                    st.mark_scoped_blocked(state, used_label, bucket, until)
                    log(f"'{used_label}' 의 {bucket} 한도만 도달. "
                        f"{left // 3600}시간 {left % 3600 // 60}분 뒤 풀림 (계정은 계속 씁니다)")
                else:
                    st.mark_blocked(state, used_label, until, limit_detect.short_reason(out, err))
                    log(f"'{used_label}' 한도 도달. {left // 3600}시간 {left % 3600 // 60}분 뒤 풀림")
            elif verdict == "barred":
                # 조직 설정이 Claude Code 구독 접근을 껐다(403). 자격은 멀쩡해서 재로그인·격리는
                # 소용없고 갈아타면 바로 풀린다. dead 경로로 보내면 저장본 생존 확인이
                # "살아 있으니 그대로 재시도"로 눌러앉는다(2026-08-18 실사고: C 계정이 이 상태인데
                # 'error' 분류로 갈아타지 않고 재시도 58번, 마케팅위키 답변 30분 전패).
                st.mark_blocked(state, used_label, time.time() + BARRED_COOLDOWN,
                                "조직이 Claude Code 구독 접근 차단(403)")
                log(f"'{used_label}' 은(는) 조직 설정으로 Claude Code 구독 접근이 꺼져 있습니다. "
                    f"{BARRED_COOLDOWN // 3600}시간 재우고 다음 계정으로 넘깁니다. "
                    f"계속 쓰려면 claude.ai 에서 그 계정의 조직 설정을 확인하세요.")
            else:  # dead(로그인 거절) 또는 갈아끼운 저장본이 상한 auth
                # 같은 계정이 짧은 시간에 거듭 거절받으면 아래 우회 갈래를 더 믿지 않는다.
                # 저장본 조회가 되고 재발급 열쇠 날짜가 남아 있어도 실제 갱신은 거부될 수 있다
                # (열쇠는 한 번 쓰면 회전 폐기된다). 그걸 "살아 있음"으로 보고 같은 계정에
                # 눌러앉으면 아무 조치 없이 몇 시간이 지난다 — 2026-08-03 실사고: 마케팅위키
                # 챗봇 답변이 1시간 24분 동안 전패했는데 순환기는 "저장본은 살아 있습니다"만
                # 반복했고, 계정을 갈아끼우자 바로 풀렸다.
                rejects, stuck_for = st.note_auth_reject(state, used_label)
                forced = rejects >= AUTH_REJECT_LIMIT and stuck_for >= AUTH_REJECT_STUCK
                strikes = 0
                if forced:
                    strikes = st.note_forced_rotate(state, used_label)
                    log(f"'{used_label}' 이(가) {int(stuck_for // 60)}분 {int(stuck_for % 60)}초째 "
                        f"로그인 거절만 받고 있습니다(거절 {rejects}번). "
                        f"저장본 판정과 무관하게 다음 계정으로 넘깁니다.")
                place_why, recent, acct = _cred_place_check(state, used_label)
                fix = _keychain_fix(acct, used_label) if (place_why and acct) else ""
                if fix == "in-sync":
                    # 세 자리가 이미 같은 값이었다 = 자격 자리 문제가 아니다.
                    # 사유를 지워 평소의 계정 처리(교체·재우기)로 내려보낸다.
                    place_why = ""
                if fix == "fixed" and st.claim_notice(f"cred-fixed:{int(time.time() // 3600)}"):
                    # 자격 자리를 실제로 고친 사실은 계정을 갈아타든 아니든 알린다.
                    _notify_cred_fixed(recent)
                if fix in ("fixed", "mirrored") and not forced:
                    # 자격 자리를 스스로 고쳤다. 계정은 그대로 두고 다시 시도하면 통과한다.
                    # "mirrored" 는 되비추기로 조용히 맞춘 경우라 사람에게 알리지 않는다.
                    log(f"'{used_label}' 계정은 그대로 두고 다시 시도합니다.")
                    st.save_state(state)
                    return used_label or current
                if place_why and fix != "half":
                    # 자격 자리가 깨졌다는 근거가 있으면 사람에게는 항상 알린다(시간당 한 건).
                    # 계정을 갈아타든 아니든 사람이 손볼 거리는 그대로 남아 있기 때문이다.
                    # "half" 면 저장본 재설치 안내가 이미 나갔다 — 같은 일로 두 번 알리지 않는다.
                    if st.claim_notice(f"cred-place:{int(time.time() // 3600)}"):
                        _notify_cred_place(place_why, recent, acct)
                if place_why and not forced:
                    if fix == "half":
                        st.save_state(state)
                        return used_label or current
                    # 계정이 죽은 게 아니라 이 컴퓨터가 자격을 읽는 자리가 깨진 경우다.
                    # 여기서 계정을 재우면 멀쩡한 계정이 줄줄이 잠기고(2026-07-29 B 가 그렇게
                    # 잠겼다), 봇들이 종량제로 넘어가 카드가 나간다. 막지 말고 사람에게 알린다.
                    log(f"'{used_label}' 로그인 거절. 그런데 {place_why}. 계정 문제가 아니라 "
                        f"자격을 읽는 자리 문제로 봅니다. 막지 않고 그대로 다시 시도합니다.")
                    st.save_state(state)
                    return used_label or current
                if verdict == "dead" and not forced and _cred_alive(used_label):
                    # 저장본이 멀쩡한데 거절이 온 경우다. 자격 파일을 갈아끼우는 순간에
                    # 호출이 겹치면 남의 자격으로 나가 인증 실패가 난다. 이때 지금 계정을
                    # 죽었다고 막으면 멀쩡한 계정이 줄줄이 막히고, 봇들이 종량제로 넘어가
                    # 카드가 나간다(2026-07-29 실제 사고). 막지 말고 같은 계정으로 재시도한다.
                    log(f"'{used_label}' 저장본은 살아 있습니다(서버 확인). "
                        f"계정을 막지 않고 그대로 다시 시도합니다.")
                    st.save_state(state)
                    return used_label
                if forced and strikes >= 2 and not place_why and _quarantine_cred(used_label):
                    # place_why 가 있으면 이 컴퓨터의 자격 자리 장애일 수 있다(여러 계정 동반
                    # 거절 등). 그때 격리하면 멀쩡한 저장본이 빠진다 — 짧은 재움으로 넘긴다
                    # (코덱스 검수 2026-08-15).
                    # 같은 계정이 하루 안에 두 번째 강제 교체 = 저장본이 진짜 죽은 것이다.
                    # 첫 번째는 되비추기가 스스로 고칠 수 있어 짧게 재우고 기회를 주지만,
                    # 그 뒤에도 또 걸리면 "5분 재움 → 1순위라 되돌아감 → 또 거절" 왕복이
                    # 매시간 알림을 쏟아낸다(2026-08-15 A 사고). 여기서 격리로 끊는다.
                    log(f"'{used_label}' 저장본을 creds/dead/ 로 격리했습니다. "
                        f"재로그인 전까지 순환에서 뺍니다.")
                    _notify_cred_dead(used_label)
                else:
                    # 잇단 거절로 강제로 넘기는 경우엔 짧게만 쉬게 한다. 원인이 계정이 아니라
                    # 이 컴퓨터의 자격 자리일 수도 있는데, 그때 네 계정을 30분씩 재우면
                    # 그동안 쓸 계정이 없어져 봇들이 종량제로 넘어간다.
                    cooldown = FORCED_ROTATE_COOLDOWN if forced else BROKEN_CRED_COOLDOWN
                    st.mark_blocked(state, used_label, time.time() + cooldown,
                                    limit_detect.short_reason(out, err))
                    _why = ("로그인이 만료됐습니다" if verdict == "dead" else "저장본이 상했습니다")
                    log(f"'{used_label}' {_why}(로그인 거절). "
                        f"{cooldown // 60}분 쉬게 하고 다음 계정으로 넘깁니다. "
                        f"그 계정으로 다시 로그인해 save_account.py 로 저장하세요.")

        nxt = st.pick_next(state, exclude=used_label, bucket=bucket)
        if nxt is None:
            # 해제 시각도 그 칸까지 봐야 맞다. 계정 단위 막힘만 보면 5시간 한도가 풀리는
            # 시각이 찍혀 실제보다 몇 시간 이른 시각을 알린다(2026-08-20).
            reset_at = st.earliest_reset(state, bucket=bucket)
            when = time.strftime("%m월 %d일 %H:%M", time.localtime(reset_at)) if reset_at else "알 수 없음"
            msg = _exhausted_message(state, bucket, when)
            log(msg)
            # 소진 구간마다 한 건만 알린다. 봇 55곳이 각자 부르면 알림이 쏟아진다.
            should_notify = st.mark_notified(state, reset_at)
            st.save_state(state)
            # 실제 전송은 잠금 밖에서 한다. 통신이 잠금을 붙잡으면 뒤에 온 봇들이 대기하다 터진다.
            raise AllAccountsExhausted(msg, reset_at=reset_at, notify_message=msg if should_notify else None)

        if not _switch(nxt, leaving=current):
            st.save_state(state)
            raise AllAccountsExhausted(f"'{nxt}' 로 자격 교체에 실패했습니다.")

        st.set_current(state, nxt, f"{used_label or '?'} → {nxt} ({verdict})")
        st.save_state(state)
        log(f"'{used_label or '?'}' → '{nxt}' 계정 교체 완료")
        _notify_switch(used_label, nxt, verdict, state, bucket)
        return nxt


def switch_message(used_label, nxt, verdict, state, bucket=None) -> str:
    """계정 전환 알림 문구. 두괄식 — 첫 줄이 '어디서 어디로, 왜'.

    사용량 경고(usage_alert)와 같은 표기를 쓴다. 시각·남은량 표기는 usage_check 한 벌에서 온다.
    `bucket` 을 주면 그 모델 칸만 바닥난 경우를 갈라 말한다 — 안 그러면 계정 전체가
    62% 남았는데도 "A 한도 도달" 로 나가 사람을 헷갈리게 한다(2026-08-14 사용자 지적).
    """
    # 어느 한도(세션 5시간 / 주간 / 모델 칸)에 걸렸는지 실시간으로 읽어 사유에 붙인다
    reason = "한도 도달"
    used_until = None
    if verdict == "limit" and usage_check:
        u_used = usage_check.get_usage(BASE / "creds" / f"{used_label}.json")
        kind = usage_check.which_limit(u_used, bucket=bucket)
        if kind == "session":
            reason = "세션(5시간) 한도 도달"
            used_until = (u_used or {}).get("session_reset")
        elif kind == "weekly":
            reason = "주간 한도 도달"
            used_until = (u_used or {}).get("weekly_reset")
        elif kind and kind.startswith("model:"):
            reason = f"{bucket} 한도만 도달, 계정 주간은 여유"
            used_until = usage_check.scoped_reset(u_used, bucket)
    elif verdict == "limit":
        reason = "주간/세션 한도 도달"
    else:
        reason = "로그인 저장본 문제"

    lines = [f"[클로드] 계정 {used_label} → {nxt} 전환됨 ({used_label} {reason})", ""]

    if usage_check:  # 새 계정이 얼마나 남았는지 (전환 직후라 이게 지금 쓰는 계정)
        u_next = usage_check.get_usage_for(BASE / "creds" / f"{nxt}.json", is_current=True)
        rem = None if not u_next or u_next.get("weekly_pct") is None \
            else max(0.0, 100 - float(u_next["weekly_pct"]))
        if rem is not None:
            lines.append(f"{nxt} 주간 {usage_check.pct_text(rem)} 남음")

    blocked = state.get("blocked") or {}
    if used_until is None:
        used_until = blocked.get(used_label)
    if used_until and usage_check:
        if verdict == "limit":
            lines.append(f"{used_label} {usage_check.fmt_reset_full(used_until)}")
        else:  # 한도가 아니라 잠깐 쉬게 한 것이라 '초기화' 라고 하면 안 된다
            left = usage_check.left_phrase(float(used_until) - time.time())
            lines.append(f"{used_label} {left} 다시 시도")

    # 남은 계정: 새로 쓰는 것과 이미 막힌 것을 빼고, 주간 남은량을 같이 보여준다.
    # 잔여 0% 는 막힘 표시가 아직 없어도 못 쓰는 계정이라 따로 가른다(사용량 경고와 같은 규칙).
    ok, dead = [], []
    for l in st.account_labels():
        if l in (used_label, nxt) or l in blocked:
            continue
        rem = usage_check.weekly_left(BASE / "creds" / f"{l}.json") if usage_check else None
        if rem is None:
            ok.append(f"계정 {l}")  # 못 읽었으면 못 쓴다고 단정하지 않는다
        elif rem > 0:
            ok.append(f"계정 {l} {usage_check.pct_text(rem)}")
        else:
            dead.append(f"계정 {l} 0%")
    if ok:
        lines += ["", "전환 가능"] + ok
    if dead:
        lines += ["", "사용 불가"] + dead
    if not ok and not dead:
        lines += ["", "남은 계정 없음"]
    return "\n".join(lines)


def _notify_switch(used_label, nxt, verdict, state, bucket=None):
    """계정이 바뀔 때 사용자에게 텔레그램으로 알린다. 세 머신 통틀어 전환당 한 번만.

    실제로 교체를 수행한 머신만 이 지점에 오고, claim_notice 로 한 번 더 걸러 중복을 막는다.
    """
    if not used_label:
        return  # 시작점 지정 등, 전환이 아니면 알리지 않는다
    if verdict in ("dead", "auth"):
        # 죽은 저장본이 낀 전환은 같은 내용이 왕복마다 반복된다(2026-08-15 매시간 도배).
        # 같은 (어디서→어디로, 사유)는 하루 한 번만 알린다. 격리 카드가 본 신호다.
        key = f"switch_{used_label}_{nxt}_{verdict}_{int(time.time() // 86400)}"
    else:
        key = f"switch_{used_label}_{nxt}_{int(time.time() // 600)}"  # 10분 창당 1회
    if not st.claim_notice(key):
        return
    notify(switch_message(used_label, nxt, verdict, state, bucket))


def run(args, *, input=None, timeout=None, env=None, max_attempts=None, **kwargs):
    """claude CLI 를 순환 보호 아래서 실행한다. subprocess.run 과 같은 결과를 돌려준다.

    한도 거절이면 계정을 바꿔 같은 요청을 다시 보낸다.
    로그인 거절은 두 가지로 갈린다. 처음부터 났으면 이 환경에서 로그인이 안 된 것이라
    계정을 안 바꾸고 멈추고, 계정을 갈아끼운 뒤에 났으면 그 저장본이 상한 것이라
    쉬게 하고 다음 계정으로 넘어간다.
    그 밖의 실패는 한 번만 그대로 재시도하고, 결과를 그대로 돌려준다.
    쓸 계정이 없으면 AllAccountsExhausted 를 올린다.
    """
    # 이번 호출이 어느 모델 칸을 쓰는지. 봇이 --model 을 안 붙이면 None 이고,
    # 그때는 예전처럼 계정 단위 한도만 본다(동작이 안 바뀐다).
    bucket = usage_check.bucket_for_model(intended_model(args)) if usage_check else None

    labels = st.saved_labels()
    if max_attempts is None:
        max_attempts = max(2, len(labels) + 1)
    max_attempts = max(1, int(max_attempts))  # 0 이하가 들어와도 최소 한 번은 실행

    # 결과를 읽어야 한도 거절을 알아볼 수 있다. 호출부가 따로 정했으면 그 뜻을 따른다.
    # stdout/stderr 를 직접 지정한 호출에 capture_output 을 같이 넣으면 파이썬이 바로 거부한다.
    if "stdout" not in kwargs and "stderr" not in kwargs:
        kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    # 글자 모드로 주고받을 땐 문자표를 utf-8 로 못박는다. 윈도우 기본(cp949)으로 두면
    # 한글·이모지가 든 프롬프트나 답변에서 통째로 실패한다(2026-07-28 winserver 실측:
    # viral-radar 이름 짓기가 UnicodeDecodeError 로 조용히 빈손이 됐다).
    if kwargs.get("text") and "encoding" not in kwargs:
        kwargs["encoding"] = "utf-8"
        kwargs.setdefault("errors", "replace")
    if not kwargs.get("capture_output") and "stdout" not in kwargs:
        log("출력을 안 잡는 호출이라 한도 거절을 알아볼 수 없습니다. 계정 교체가 안 됩니다.")

    # check=True 를 그대로 넘기면 한도를 판정하기 전에 예외가 터져 교체가 안 일어난다.
    # 안에서는 꺼두고, 마지막에 실패로 끝났을 때만 원래 뜻대로 예외를 올린다.
    want_check = bool(kwargs.pop("check", False))

    retried_plain = False
    switched_here = False  # 이 호출 안에서 계정을 갈아끼웠는지
    proc = None
    for attempt in range(max_attempts):
        state = st.prune_blocked(st.load_state())
        used_label = _switch_if_blocked(state, bucket)

        try:
            proc = subprocess.run(args, input=input, timeout=timeout, env=env, **kwargs)
        except Exception:
            raise
        verdict = limit_detect.classify(proc.returncode, proc.stdout, proc.stderr)

        if verdict == "ok":
            _clear_auth_rejects(used_label)
            return _finish(proc, args, want_check)

        if verdict == "auth" and not switched_here:
            # 계정을 바꿔도 안 풀린다. ssh 비대화형처럼 자격을 아예 못 읽는 환경에서 나오는데,
            # 이때 계정을 돌리면 멀쩡한 계정들이 줄줄이 막힌 것으로 잘못 기록된다.
            # 다만 봇 자리(launchd)에서 이 문구가 나오면 열쇠꾸러미에 남은 옛 자격이 원인일 수
            # 있다. 그 경우 사람이 지워줘야 풀리므로 여기서도 한 번 알린다.
            place = _warn_cred_place(used_label)
            if place == "fixed":
                continue  # 옛 자격을 지웠으니 같은 계정으로 한 번 더 해본다
            if not place:
                log("로그인이 안 된 상태입니다. 계정을 바꾸지 않습니다. "
                    "화면 세션에서 claude /login 을 했는지 확인하세요.")
            return _finish(proc, args, want_check)

        if verdict == "error":
            if retried_plain:
                return _finish(proc, args, want_check)  # 원인 모를 실패는 호출부가 판단하도록
            retried_plain = True
            log(f"한도와 무관한 실패. 한 번 더 시도합니다 (종료코드 {proc.returncode})")
            time.sleep(2)
            continue

        # limit·barred, 또는 갈아끼운 저장본이 상한 auth → 계정 교체 후 재시도
        try:
            nxt = _rotate_after_failure(used_label, verdict, proc.stdout, proc.stderr,
                                        bucket=bucket)
        except AllAccountsExhausted as e:
            if e.notify_message:  # 전송은 잠금 밖에서
                notify(e.notify_message)
            raise
        switched_here = switched_here or (nxt != used_label)

    log("재시도 횟수를 다 썼습니다. 마지막 결과를 그대로 돌려줍니다.")
    return _finish(proc, args, want_check)


def _finish(proc, args, want_check):
    """호출부가 check=True 를 원했다면 원래 뜻대로 실패에서 예외를 올린다."""
    if want_check and proc is not None and proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, args, output=proc.stdout, stderr=proc.stderr
        )
    return proc


def run_claude(cli_args, **kwargs):
    """claude 실행 파일 경로를 알아서 붙여주는 짧은 형태. run() 과 동작 같음."""
    return run([claude_bin(), *cli_args], **kwargs)


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if argv[0] == "--status":
        # 뒤에 붙은 인자(`--model ...`)까지 본다. 안 주면 settings.json 고정값을 쓴다.
        print(status_text(intended_model(argv[1:])))
        return 0
    if argv[0] == "--next":
        return next_account(model=intended_model(argv[1:]))
    if argv[0] == "--ensure":
        # 뒤에 붙는 건 켜려는 claude 의 인자를 그대로 넘겨받은 것이다.
        # 모르는 인자는 무시하고 --model 만 본다.
        return ensure_usable(intended_model(argv[1:]))
    if argv[0] == "--use":
        if len(argv) < 2:
            print("사용법: python claude_pool.py --use <라벨>")
            return 1
        return use(argv[1].strip())
    if argv[0] == "--":
        try:
            proc = run_claude(argv[1:])
        except AllAccountsExhausted as e:
            print(str(e), file=sys.stderr)
            return 2
        sys.stdout.write(proc.stdout or "")
        sys.stderr.write(proc.stderr or "")
        return proc.returncode

    print("알 수 없는 인자입니다. --status / --use <라벨> / -- <claude 인자> 중 하나를 쓰세요.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
