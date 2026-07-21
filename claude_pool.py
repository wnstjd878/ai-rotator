"""
== 운영 맥락 ==
용도: claude CLI 를 부를 때 구독 한도 거절이 오면 자동으로 다음 계정 자격으로 갈아끼우고
      같은 요청을 다시 보낸다. 봇들이 subprocess.run(claude ...) 대신 이 모듈의
      run() 을 쓰면 한도로 인한 실패가 사라진다.
흐름: (1) claude 실행 → (2) 결과 판정(limit_detect) → (3) 한도면 그 계정을 해제 시각까지
      쉬게 표시하고 다음 계정으로 자격 교체(switch_account) → (4) 같은 요청 재시도 →
      (5) 쓸 계정이 하나도 없으면 AllAccountsExhausted 를 올리고 텔레그램으로 알림.
실행: python claude_pool.py --status          지금 계정 상태 보기
      python claude_pool.py --next            지금 계정을 한도로 보고 다음 계정으로 넘김
      python claude_pool.py --use A           A 계정으로 지금 바로 교체
      python claude_pool.py -- -p "안녕"       claude 를 순환 보호 아래서 실행
호출: from claude_pool import run;  proc = run([claude_bin, "--print", ...], timeout=240)
전제: creds/<라벨>.json 에 계정별 자격 저장본이 있어야 한다 (save_account.py 로 저장).
      Mac 은 Keychain 을 고쳐야 해서 화면(Aqua) 세션에서 돌아야 한다. launchd 봇은 해당됨.
      ssh 비대화형에서는 자격 교체가 실패할 수 있다.
동시성: 여러 봇이 같이 실패해도 교체는 잠금으로 한 번만 일어난다. 다른 프로세스가 이미
      계정을 바꿨으면 교체 없이 바로 재시도한다.
의도적 미구현: 사용량을 미리 재는 선제 전환 없음(limit_detect 참고). 대화형 claude 세션의
      실시간 교체 없음 — 이미 떠 있는 세션은 재시작해야 새 계정이 붙는다.
마지막 점검: 2026-07-22
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

import limit_detect  # noqa: E402
import pool_state as st  # noqa: E402

LOG_FILE = BASE / "rotator.log"
BROKEN_CRED_COOLDOWN = 30 * 60  # 갈아끼운 저장본이 상했을 때 그 계정을 쉬게 할 시간(초)
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


def _switch(label: str) -> bool:
    """switch_account.py 를 그대로 재사용해 자격을 교체한다."""
    ret = subprocess.run(
        [sys.executable, str(BASE / "switch_account.py"), label],
        capture_output=True, text=True,
    )
    if ret.returncode != 0:
        log(f"자격 교체 실패({label}): {(ret.stderr or ret.stdout).strip()[:200]}")
        return False
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


def status_text() -> str:
    state = st.prune_blocked(st.load_state())
    cur = state.get("current") or "(모름 — --use 로 한 번 정해주세요)"
    return f"현재 계정: {cur}\n" + st.describe(state)


def use(label: str) -> int:
    """지정한 계정으로 즉시 교체하고 상태에 기록한다."""
    if label not in st.saved_labels():
        print(f"'{label}' 자격 저장본이 없습니다. 저장된 계정: {', '.join(st.saved_labels()) or '(없음)'}")
        return 1
    with st.with_lock():
        if not _switch(label):
            return 1
        state = st.prune_blocked(st.load_state())
        blocked = dict(state.get("blocked") or {})
        blocked.pop(label, None)  # 손으로 고른 계정은 막힘 표시를 푼다
        state["blocked"] = blocked
        st.set_current(state, label, "수동 지정")
        st.save_state(state)
    print(f"'{label}' 계정으로 교체했습니다.")
    return 0


def next_account(cooldown: float = limit_detect.DEFAULT_COOLDOWN) -> int:
    """지금 계정을 한도 도달로 보고 다음 계정으로 넘긴다 (사람이 직접 쓸 때).

    대화형 claude 창에서 한도 거절을 봤을 때 쓴다. 교체 뒤 claude 를 새로 실행해야 적용된다.
    """
    with st.with_lock():
        state = st.prune_blocked(st.load_state())
        cur = state.get("current")
        if cur:
            st.mark_blocked(state, cur, time.time() + cooldown, "사람이 한도 도달로 지정")
        nxt = st.pick_next(state, exclude=cur)
        if nxt is None:
            st.save_state(state)
            reset_at = st.earliest_reset(state)
            when = time.strftime("%m월 %d일 %H:%M", time.localtime(reset_at)) if reset_at else "알 수 없음"
            print(f"쓸 수 있는 계정이 없습니다. 가장 빨리 풀리는 시각: {when}")
            print(st.describe(state))
            return 2
        if not _switch(nxt):
            st.save_state(state)
            return 1
        st.set_current(state, nxt, f"{cur or '?'} → {nxt} (사람 지정)")
        st.save_state(state)
    print(f"'{cur or '?'}' → '{nxt}' 계정으로 교체했습니다.")
    print("열려있는 claude 를 종료하고 다시 실행하면 적용됩니다.")
    return 0


def _rotate_after_failure(used_label: str | None, verdict: str, out: str, err: str) -> str:
    """실패한 계정을 쉬게 하고 다음 계정으로 교체. 교체된 라벨을 반환.

    다른 프로세스가 이미 계정을 바꿨으면 교체하지 않고 그 라벨을 그대로 쓴다.
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
                until = limit_detect.reset_epoch(out, err)
                st.mark_blocked(state, used_label, until, limit_detect.short_reason(out, err))
                left = int(until - time.time())
                log(f"'{used_label}' 한도 도달. {left // 3600}시간 {left % 3600 // 60}분 뒤 풀림")
            else:  # auth: 갈아끼운 저장본이 상한 경우에만 여기로 온다
                st.mark_blocked(state, used_label, time.time() + BROKEN_CRED_COOLDOWN,
                                limit_detect.short_reason(out, err))
                log(f"'{used_label}' 저장본이 상했습니다(로그인 거절). "
                    f"{BROKEN_CRED_COOLDOWN // 60}분 쉬게 하고 다음 계정으로 넘깁니다. "
                    f"그 계정으로 다시 로그인해 save_account.py 로 저장하세요.")

        nxt = st.pick_next(state, exclude=used_label)
        if nxt is None:
            reset_at = st.earliest_reset(state)
            when = time.strftime("%m월 %d일 %H:%M", time.localtime(reset_at)) if reset_at else "알 수 없음"
            msg = f"클로드 구독 계정이 전부 한도에 걸렸습니다. 가장 빨리 풀리는 시각: {when}"
            log(msg)
            # 소진 구간마다 한 건만 알린다. 봇 55곳이 각자 부르면 알림이 쏟아진다.
            should_notify = st.mark_notified(state, reset_at)
            st.save_state(state)
            # 실제 전송은 잠금 밖에서 한다. 통신이 잠금을 붙잡으면 뒤에 온 봇들이 대기하다 터진다.
            raise AllAccountsExhausted(msg, reset_at=reset_at, notify_message=msg if should_notify else None)

        if not _switch(nxt):
            st.save_state(state)
            raise AllAccountsExhausted(f"'{nxt}' 로 자격 교체에 실패했습니다.")

        st.set_current(state, nxt, f"{used_label or '?'} → {nxt} ({verdict})")
        st.save_state(state)
        log(f"'{used_label or '?'}' → '{nxt}' 계정 교체 완료")
        return nxt


def run(args, *, input=None, timeout=None, env=None, max_attempts=None, **kwargs):
    """claude CLI 를 순환 보호 아래서 실행한다. subprocess.run 과 같은 결과를 돌려준다.

    한도 거절이면 계정을 바꿔 같은 요청을 다시 보낸다.
    로그인 거절은 두 가지로 갈린다. 처음부터 났으면 이 환경에서 로그인이 안 된 것이라
    계정을 안 바꾸고 멈추고, 계정을 갈아끼운 뒤에 났으면 그 저장본이 상한 것이라
    쉬게 하고 다음 계정으로 넘어간다.
    그 밖의 실패는 한 번만 그대로 재시도하고, 결과를 그대로 돌려준다.
    쓸 계정이 없으면 AllAccountsExhausted 를 올린다.
    """
    labels = st.saved_labels()
    if max_attempts is None:
        max_attempts = max(2, len(labels) + 1)
    max_attempts = max(1, int(max_attempts))  # 0 이하가 들어와도 최소 한 번은 실행

    # 결과를 읽어야 한도 거절을 알아볼 수 있다. 호출부가 따로 정했으면 그 뜻을 따른다.
    # stdout/stderr 를 직접 지정한 호출에 capture_output 을 같이 넣으면 파이썬이 바로 거부한다.
    if "stdout" not in kwargs and "stderr" not in kwargs:
        kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
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
        used_label = state.get("current")

        try:
            proc = subprocess.run(args, input=input, timeout=timeout, env=env, **kwargs)
        except Exception:
            raise
        verdict = limit_detect.classify(proc.returncode, proc.stdout, proc.stderr)

        if verdict == "ok":
            return _finish(proc, args, want_check)

        if verdict == "auth" and not switched_here:
            # 계정을 바꿔도 안 풀린다. ssh 비대화형처럼 자격을 아예 못 읽는 환경에서 나오는데,
            # 이때 계정을 돌리면 멀쩡한 계정들이 줄줄이 막힌 것으로 잘못 기록된다.
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

        # limit, 또는 갈아끼운 저장본이 상한 auth → 계정 교체 후 재시도
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
        print(status_text())
        return 0
    if argv[0] == "--next":
        return next_account()
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
