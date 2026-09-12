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
      python claude_pool.py --sweep           낡은 계정 막힘 걷어내기(주기 작업용, 30분 간격 제한)
      python claude_pool.py --tend            떠 있는 창의 칸이 바닥났으면 갈아끼우기(주기 작업용)
      python claude_pool.py --claim <번호>     그 프로세스가 쓰는 창이 이 모델 칸을 잡았다고 표시
      python claude_pool.py --unclaim <번호>   창이 닫혔다고 표시(프로세스가 죽으면 자동 정리)
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
대화 중 교체(2026-09-06 추가): claude 는 요청마다 자격 파일을 다시 읽으므로 떠 있는 창도
      파일만 갈아끼우면 다음 메시지부터 새 계정이 붙는다(재시작 불필요). Windows 본체 작업
      스케줄러 `ai-rotator-tend-windows` 가 3분마다 `--tend` 로 떠 있는 창의 칸을 확인해
      바닥났으면 갈아끼운다(Mac 은 launchd `com.user.claude-tend`, 180초). ccauto 도 창이 끝난 뒤 `--ensure` 종료코드(3=갈아탐)를 보고
      Fable 칸 소진을 알아본다(haiku 찔러보기는 그 칸을 못 봤다).
떠 있는 창 지키기(2026-08-22 추가): 자격은 이 컴퓨터에 한 벌뿐이라 봇이 계정을 갈아끼우면
      떠 있는 창까지 끌려간다. 그래서 사람 창은 켜질 때 자기가 쓰는 모델 칸을 표시하고
      (claude_shim 이 자동으로, ccauto 도 한 번 더), 계정을 고르는 자리는 그 칸까지
      만족하는 계정을 먼저 찾는다(_wanted_buckets / _pick_account).
      되돌리기는 창을 절대 밀어내지 않고, 갈아타야만 하는 교체는 다른 수가 없을 때만 민다.
      표시는 프로세스가 죽으면 저절로 걷힌다(pool_state.prune_claims).
마지막 점검: 2026-09-06
"""

from __future__ import annotations

import contextlib
import io
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


def _hidden_child_flags() -> int:
    """창 없는 파이썬(pythonw, 예약 작업 `--tend`) 아래서만 자식 콘솔 창을 숨긴다.

    부모에 콘솔이 없으면 claude·ssh 같은 콘솔 프로그램이 새 콘솔을 만들고, 기본 터미널인
    Windows Terminal 이 그걸 창으로 띄워 포커스를 가져간다(2026-09-07 사용자 지적).
    콘솔이 있는 자리(사람 터미널·가로채기 claude_shim)에서는 0 을 돌려줘 동작을 안 바꾼다.
    """
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        # 콘솔이 붙어 있으면 GetConsoleCP 가 0 이 아니다. (GetConsoleWindow 는 Claude Code 의
        # 숨긴 콘솔·ConPTY 아래서 0 을 돌려줘 오판하므로 쓰지 않는다 — 2026-09-07 실측.)
        if ctypes.windll.kernel32.GetConsoleCP():
            return 0
    except Exception:
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


import limit_detect  # noqa: E402
import pool_state as st  # noqa: E402
import slot_pool  # 모델별 자리(창마다 다른 계정), Windows 만 켜짐  # noqa: E402
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
CLAIM_LOCK_TIMEOUT = 5          # 창 표시를 적을 때 잠금을 기다리는 상한(초). 창 켜기를 늦추지 않는다
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
        creationflags=_hidden_child_flags(),
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
            # 후보 기준은 실제로 계정을 고르는 쪽과 같은 한 벌을 쓴다(구독 해지 제외).
            free = [l for l in st.usable_labels() if not st.is_blocked(state, l)]
            # 다른 칸도 막혀 있으면 그 사실을 덧붙인다. 다만 '다른 모델은 안 된다' 로
            # 말하지는 않는다 — 기록에 없는 칸(Sonnet 등)은 안 막힌 것이고 그 모델로는
            # 지금도 쓸 수 있다(2026-08-20 코덱스 재검수 지적, 재현됨).
            # **모든 후보에서 막힌 칸만 적는다** — 한 계정만 막힌 칸은 다른 계정으로
            # 쓸 수 있어서, 합집합으로 모으면 멀쩡한 모델을 못 쓴다고 알린다
            # (2026-08-21 코덱스 3차 지적, 재현됨).
            common = (set.intersection(
                *(st.scoped_blocked_buckets(state, l) for l in free)) if free else set())
            others = sorted(common - {bucket})
        except Exception:
            free, others = [], []
        if free:
            tail = "다른 모델로 켜면 지금도 쓸 수 있습니다."
            if others:
                tail += " 다만 " + ", ".join(others) + " 칸도 바닥이라 그 모델은 빼야 합니다."
            return (f"{lead}{bucket} 칸이 이 컴퓨터가 쓸 수 있는 계정 전부에서 바닥입니다(계정 주간은 여유 있음, 다른 컴퓨터의 계정은 별개). "
                    f"{tail} "
                    f"{bucket} 칸이 가장 빨리 풀리는 시각: {when}")
    return f"{lead}구독 계정이 전부 한도에 걸렸습니다. 가장 빨리 풀리는 시각: {when}"


def status_text(model: str | None = None) -> str:
    """계정별 남은 사용량 화면.

    2026-09-04 에 모양을 바꿨다. 그 전에는 계정 한 줄에 상태·주간·세션·모델 칸·풀리는
    시각을 쉼표로 이어 붙여, 한 줄이 200자를 넘고 어느 숫자가 어느 칸인지 눈으로 못
    골랐다(사용자 지적). 지금은 계정마다 머리글 한 줄 + 칸마다 막대 한 줄이다.
    그리는 일은 usage_view 가 한 벌로 맡는다(지피티 쪽 화면과 규칙을 같이 쓰려고).
    """
    state = st.prune_blocked(st.load_state())
    cur = state.get("current")
    now = time.time()
    saved = set(st.saved_labels())
    # 이 창이 쓸 모델의 칸(대개 settings.json 고정값). 못 알아내면 요약 줄을 안 만든다.
    bucket = (usage_check.bucket_for_model(model or intended_model())
              if usage_check else None)
    color = usage_view.supports_color()
    scoped_rows = []
    _share_current(state.get("current"))  # 이 머신이 어디 물려 있는지 먼저 올린다
    shared = _shared_usage()   # 다른 머신이 자기 계정에서 읽어 올린 값
    holders = _shared_current() or {}  # 어느 머신이 어느 계정에 물려 있는지
    seen = []  # 여기서 읽은 사용량·초기화 시각은 다음 계정 고르는 순서와 표시에 쓰인다
    hidden = set(st.retired_labels())  # 구독 해지 계정은 목록에서 뺀다(현재 계정이면 예외로 보여줌)
    lines = []
    # 지금 쓰는 계정을 맨 위로 올린다(사용자 요구 2026-09-04). 나머지는 원래 차례 그대로 —
    # 자리가 매번 바뀌면 어느 계정이 어디 있는지 눈이 못 외운다.
    order = [l for l in st.account_labels() if not (l in hidden and l != cur)]
    if cur in order:
        order = [cur] + [l for l in order if l != cur]
    # 1) 계정마다 사용량부터 모은다. 화면 글자는 낡은 막힘을 푼 **뒤에** 만든다.
    rows = []
    for label in order:
        entry, note = None, ""
        if usage_check and label in saved:
            u = usage_check.get_usage_for(BASE / "creds" / f"{label}.json",
                                          is_current=(label == cur))
            if u:
                entry = u
                seen.append((label, u))
                _share_usage(label, u)
            else:
                entry, note = usage_view.entry_and_note(state, shared, label, now)
            if bucket:
                # 화면에 그린 것과 같은 기록에서 칸 잔량을 뽑는다(둘이 어긋나면 안 된다).
                left, reset = usage_view.scoped_left(entry, bucket, now)
                scoped_rows.append((label, left, reset))
        rows.append((label, entry, note))
    # 2) 읽은 값을 기록하면서 실제로는 풀린 계정 막힘을 푼다(조회를 더 안 늘린다).
    freed = _remember_and_revive(seen)
    # 3) 글자를 만든다. 방금 푼 계정에는 막힘을 안 적는다.
    for label, entry, note in rows:
        marks = []
        who = holders.get(label) or []
        if label == cur:
            marks.append("지금 사용 중" + (f" ({', '.join(who)})" if who else ""))
        elif who:
            marks.append(f"{', '.join(who)} 사용 중")
        else:
            marks.append("대기")
        if label not in saved:
            marks.append("자격 없음")
        until = (state.get("blocked") or {}).get(label)
        if until and float(until) > now and label not in freed:
            left = int(float(until) - now)
            marks.append(f"막힘, {left // 3600}시간 {left % 3600 // 60}분 뒤 풀림")
        parts = usage_view.usage_parts(entry, now)[0] if entry else []
        if lines:
            lines.append("")
        lines += usage_view.account_block(label, marks, parts, now,
                                          current=(label == cur), color=color, note=note)
    if not lines:
        lines.append("  계정이 없습니다. --use <라벨> 로 한 번 정해 주세요.")
    tail = []
    if freed:
        tail.append(f"{', '.join(sorted(freed))} 는 실제로 한도가 풀려 있어 막힘을 해제했습니다"
                    f" (식힘 시간이 실제보다 길게 잡혀 있었습니다).")
    if bucket:
        notice = _scoped_notice(bucket, scoped_rows, now)
        if notice:
            tail.append(notice.strip())
    held = st.claimed_buckets(state)
    if held:
        # 왜 앞순위 계정으로 안 돌아가는지 사람이 알 수 있게 적는다.
        tail.append(f"이 컴퓨터에 떠 있는 창이 쓰는 칸: {', '.join(held)}"
                    f" (그 칸이 막힌 계정으로는 안 넘어갑니다)")
    if not cur:
        tail.append("쓸 계정이 안 정해져 있습니다. --use <라벨> 로 한 번 정해 주세요.")
    if tail:
        lines.append("")
        lines += ["  " + t for t in tail]
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


def _limit_reset(label: str, usage: dict | None = None, *, bucket=None,
                 is_current: bool = False, threshold: float = 95.0):
    """그 계정이 **실제로** 풀리는 시각과 어느 한도인지. 못 읽으면 (None, None).

    막을 때 고정 시간(기본 5시간)을 붙이면 두 방향으로 다 틀린다. 세션 한도는 창이
    열린 시점부터 5시간이라 막히는 순간에는 대개 거의 다 지나 있다 — 2026-09-04 실측:
    실제로는 12분 뒤 풀리는데 5시간을 잡아 주간 67% 남은 A 가 4시간 반을 놀았다.
    반대로 주간이 찬 경우엔 5시간이 너무 짧아 깨어나자마자 또 거절받는다.
    어느 쪽이든 서버가 준 시각이 정답이다.

    `usage` 를 주면 그 값을 쓴다(부르는 쪽이 이미 읽었으면 조회를 안 늘린다).
    """
    if usage_check is None:
        return None, None
    u = usage if usage is not None else usage_check.get_usage_for(
        BASE / "creds" / f"{label}.json", is_current=is_current)
    if not u:
        return None, None
    kind = usage_check.which_limit(u, threshold=threshold, bucket=bucket)
    if kind == "weekly":
        return u.get("weekly_reset"), kind
    if kind == "session":
        return u.get("session_reset"), kind
    if kind and kind.startswith("model:"):
        return usage_check.scoped_reset(u, bucket), kind
    return None, kind


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
            u = None
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
                # 재우는 시간은 서버가 준 실제 풀림 시각으로 잡는다. 고정 5시간은 세션
                # 한도에는 너무 길고(멀쩡한 계정을 몇 시간 놀린다) 주간 한도에는 너무
                # 짧다(깨어나자마자 또 거절받는다). 못 읽을 때만 고정값으로 떨어진다.
                until, _kind = _limit_reset(cur, usage=u, is_current=True)
                try:
                    ok = until is not None and float(until) > time.time()
                except (TypeError, ValueError):
                    ok = False
                st.mark_blocked(state, cur, float(until) if ok else time.time() + cooldown,
                                "사람이 한도 도달로 지정")
        nxt = _pick_or_revive(state, exclude=cur, bucket=bucket)
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


ENSURE_SWITCHED = 3   # ensure_usable 종료코드: 갈아탔다(0 은 그대로, 2 는 갈아탈 데 없음)

# 떠 있는 창을 미리 넘기는 문턱(%). 창은 한도 거절을 받으면 하던 작업이 멈추고 사람 입력을
# 기다리므로, 거절받기 전에 넘긴다(2026-09-10 사용자 결정). 봇 호출·켜기 전 확인은 그대로
# 100%(거절 기반)다. 낡은 막힘을 푸는 쪽도 이 문턱을 같이 써야 한다 — 97% 에서 막은 계정을
# "100% 아니니 열렸다" 로 풀면 되돌림이 도로 끌고 와 3분마다 왕복한다.
PREEMPT_PCT = 97.0
# 창 지키기가 이미 막힌 계정에 앉아 있을 때 다른 계정의 낡은 막힘을 확인하는 최소 간격(초).
# 매 회차 하면 3분마다 전 계정 조회, 아예 안 하면 낡은 칸 막힘이 안 풀려 창이 멈춘다(재검수 2026-09-10).
TEND_REVIVE_GAP = 1800


def ensure_usable(model: str | None = None, *, bucket: str | None = None,
                  keep_windows: bool = False, threshold: float = 100.0) -> int:
    """켜기 직전 확인. 지금 계정이 이미 다 찼으면 켜기 전에 다음 계정으로 바꾼다.

    기본은 선제 전환이 아니다. 잔여가 조금이라도 있으면 그대로 둔다(거절 기반 순환은 그대로).
    `threshold` 를 낮춰 주면 그 % 에서 미리 넘긴다 — 떠 있는 창 지키기(--tend)만 PREEMPT_PCT 를 준다.
    사용량을 못 읽으면(네트워크 실패 등) 아무것도 안 한다 — 멀쩡한 계정을 막으면 안 되므로.
    전환할 때는 그 계정을 실제 리셋 시각까지 쉬게 표시하므로 세 머신이 같이 건너뛴다.

    `model` 을 주면 그 모델이 걸리는 칸까지 본다. 계정 전체 주간이 60% 여유인데
    Fable 만 100% 소진인 경우가 실제로 있었고(2026-08-12), 그때 계정을 통째로 막으면
    남은 Opus 몫을 버리게 되므로 **그 칸만 막고** 다음 계정으로 넘긴다.
    Opus 로 켜는데 여유가 있으면 아무것도 안 바뀐다(그 계정 그대로 쓴다).
    `bucket` 을 직접 주면 모델 이름 대신 그 칸을 본다(떠 있는 창의 칸을 그대로 넘길 때).
    `keep_windows` 면 떠 있는 창의 칸을 다 만족하는 계정이 없을 때 갈아끼우지 않는다(주기
    작업용). 안 그러면 창끼리 서로 밀어내며 회차마다 왕복한다.

    종료코드: 0 그대로 / ENSURE_SWITCHED(3) 갈아탔음 / 2 갈아탈 계정 없음 / 1 교체 실패.
    감싸는 코드(ccauto)가 창이 끝난 뒤 이 값으로 "한도라서 갈아탔다" 를 알아본다 —
    haiku 로 찔러보는 확인은 Fable 칸만 바닥난 경우를 못 잡는다(2026-09-06).
    """
    if usage_check is None:
        return 0
    if bucket is None:
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
                # 못 읽었으면 원칙은 그대로 쓴다(멀쩡한 계정을 막으면 안 되므로). 단 **기록상 이미
                # 막힌 계정**이면 그대로 두는 게 아니라 기록으로 갈아탄다. 2026-09-12 실사고: B 가
                # 세션 소진으로 기록돼 있는데 사용량 조회가 429 로 4시간 넘게 실패해 cc 가 "못 읽어
                # 그대로 둔다" 로 B 를 켰고, 새 창이 첫 요청부터 한도 거절을 받았다.
                kind = ("model:" + bucket) if (bucket and st.is_scoped_blocked(state, cur, bucket)) else (
                    "blocked" if st.is_blocked(state, cur) else None)
                why = (f"사유={getattr(usage_check, 'last_error', None)}, "
                       f"응답={getattr(usage_check, 'last_status', None)}")
                if not kind:
                    # 흔적은 남긴다 — 주기 작업은 화면이 없어서 이 줄이 없으면 "조회가 계속 실패해
                    # 기능이 통째로 멈춤" 과 "할 일 없음" 을 구분할 수 없다(2026-09-06 검수).
                    # 값은 실패 종류·응답 번호뿐, 자격은 안 담긴다.
                    log(f"켜기 전 확인: '{cur}' 사용량을 못 읽어 그대로 둔다({why})")
                    break
                log(f"켜기 전 확인: '{cur}' 사용량을 못 읽었지만 기록상 막혀 있어 기록으로 갈아탄다({why})")
                kind_txt = bucket if kind.startswith("model:") else "계정"
                nxt = _pick_account(state, exclude=cur, bucket=bucket, strict=keep_windows)
                if nxt is None and time.time() - float(state.get("tend_revive_at") or 0) >= TEND_REVIVE_GAP:
                    state["tend_revive_at"] = time.time()
                    nxt = _pick_or_revive(state, exclude=cur, bucket=bucket, strict=keep_windows,
                                          below=threshold)
                if nxt is None:
                    st.save_state(state)
                    return 2
            else:
                st.note_weekly_reset(state, cur, u.get("weekly_reset"))  # 순서 기준으로 쓰인다
                st.note_usage(state, cur, u)  # 계정 목록 화면에 모델 칸까지 남기려고
                # 기본은 진짜 소진(100% 사용)일 때만. bucket 을 주면 그 모델 칸까지 본다.
                kind = usage_check.which_limit(u, threshold=threshold, bucket=bucket)
                if not kind:
                    break
                kind_txt = _kind_txt(kind, bucket)
                nxt = _next_for_exhausted(state, cur, u, kind, bucket=bucket,
                                          keep_windows=keep_windows, threshold=threshold)
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
    return ENSURE_SWITCHED


def _kind_txt(kind: str, bucket) -> str:
    """which_limit 결과를 사람이 읽는 이름으로. 칸만 찼으면 칸 이름만(뒤에 '한도 소진' 이 붙는다)."""
    if kind.startswith("model:"):
        return str(bucket)
    return "주간" if kind == "weekly" else "세션(5시간)"


def _next_for_exhausted(state: dict, cur: str, u: dict, kind: str, *, bucket, keep_windows: bool,
                        threshold: float):
    """지금 계정이 소진됐을 때(막음 기록 + 갈 곳 고르기). 갈 계정 라벨, 없으면 None.

    ensure_usable 의 잠금 안에서만 부른다. 상태 저장은 부르는 쪽이 한다.
    """
    scoped = kind.startswith("model:")
    kind_txt = _kind_txt(kind, bucket)
    # 창 지키기(문턱 97)가 지난 회차에 이미 막아 둔 계정이면 막음 기록·전 계정 조회를
    # 되풀이하지 않고 갈 곳만 조회 없이 다시 본다. 97~100% 구간에서 갈 곳이 없으면
    # 3분마다 같은 일을 되풀이했다(검수 반영 2026-09-10).
    already = threshold < 100.0 and (st.is_scoped_blocked(state, cur, bucket) if scoped
                                      else st.is_blocked(state, cur))
    if already:
        nxt = _pick_account(state, exclude=cur, bucket=bucket, strict=keep_windows)
        if nxt is None and time.time() - float(state.get("tend_revive_at") or 0) >= TEND_REVIVE_GAP:
            # 이 PC 에서 칸 막힘을 예정보다 먼저 푸는 곳은 여기뿐이라 아예 빼면 안 된다
            state["tend_revive_at"] = time.time()
            nxt = _pick_or_revive(state, exclude=cur, bucket=bucket, strict=keep_windows,
                                  below=threshold)
        return nxt
    if scoped:
        # 그 모델만 바닥났다. 계정은 살려 두고 그 칸만 막는다 —
        # 통째로 막으면 이 계정으로 아직 쓸 수 있는 다른 모델까지 버린다.
        until = usage_check.scoped_reset(u, bucket)
        st.mark_scoped_blocked(state, cur,
                               bucket, until or (time.time() + limit_detect.DEFAULT_COOLDOWN))
    else:
        until = u.get("weekly_reset") if kind == "weekly" else u.get("session_reset")
        st.mark_blocked(state, cur, until or (time.time() + limit_detect.DEFAULT_COOLDOWN),
                        f"{kind_txt} 한도 소진("
                        + ("창 지키기, 미리 넘김" if threshold < 100.0 else "켜기 전 확인") + ")")
    # 되살리기 기준은 이번 문턱을 따른다: cc 켜기 전(100)은 97~99% 계정도 살려 쓴다
    if threshold < 100.0:
        state["tend_revive_at"] = time.time()   # 방금 확인했으니 30분은 다시 안 본다
    return _pick_or_revive(state, exclude=cur, bucket=bucket, strict=keep_windows, below=threshold)


def tend_windows() -> int:
    """떠 있는 사람 창 지키기(주기 작업용). 창이 잡은 칸이 지금 계정에서 바닥났으면 갈아끼운다.

    자격은 이 컴퓨터에 한 벌뿐이고 claude 는 매 요청마다 자격 파일을 다시 읽으므로,
    파일을 갈아끼우면 떠 있는 창도 **다음 메시지부터** 새 계정으로 간다(2026-08-22 사고에서
    봇의 교체가 떠 있는 Fable 창을 끌고 간 것이 그 증거). 그동안은 창이 켜질 때(--ensure)와
    끝난 뒤(ccauto)만 봐서, 대화 중에 Fable 이 바닥나면 사람이 cc 를 다시 쳐야 했다.

    - 창 표시(session_claims)가 하나도 없으면 아무 조회도 안 한다(봇만 도는 머신에서 공짜).
    - 창들이 잡은 칸마다 --ensure 와 같은 판정을 하되 PREEMPT_PCT(97%)에서 미리 넘긴다.
      100% 에서 넘기면 이미 거절을 받은 뒤라 창의 작업이 멈춘다(2026-09-10 사용자 결정).
      갈아탈 계정은 _wanted_buckets 로 **떠 있는 창들의 칸을 다 만족하는** 곳을 고른다.
    - 사용량 조회는 칸 종류 수만큼(대개 1회). 3분 주기면 상태줄 조회보다 적다.
    - 갈아탔으면 --ensure 가 텔레그램 알림까지 보낸다(따로 안 보낸다).
    종료코드: ENSURE_SWITCHED(3) 갈아탔음 / 그 밖엔 0(창 없음·그대로·못 읽음 모두).
    """
    if usage_check is None:
        return 0
    # 자리(slot) 창은 제 폴더의 자격만 갈아끼운다. 다른 자리·봇은 안 건드린다.
    try:
        for b in slot_pool.tend(PREEMPT_PCT):
            log(f"창 지키기: {b} 자리의 계정을 갈아끼웠다")
    except Exception as e:
        log(f"창 지키기: 자리 확인 중 오류({type(e).__name__})")
    try:
        buckets = st.claimed_buckets(st.load_state())
    except Exception:
        return 0
    rc = _tend_claimed(buckets) if buckets else 0
    if not buckets:
        print("[창 지키기] 떠 있는 창 없음")
    # 미리 넘겨도 한 번에 3% 넘게 쓰면 거절받고 멈춘 창이 생긴다. 그 창에 이어가라고 보낸다.
    # 창 표시가 없는 창(claude 를 직접 친 창)도 대상이라 창 표시 유무와 따로 돈다.
    _nudge_stalled_windows()
    return rc


def _usable_now(model) -> bool:
    """지금 계정으로 그 모델을 쓸 수 있나(미리 넘기는 문턱 아래인가). 못 읽으면 False."""
    if usage_check is None:
        return False
    # 그 모델의 자리가 떠 있으면 자리 계정으로 판단한다(그 창들은 공용 자격을 안 쓴다)
    got = slot_pool.usable_now(usage_check.bucket_for_model(model), PREEMPT_PCT)
    if got is not None:
        return got
    try:
        cur = st.load_state().get("current")
    except Exception:
        return False
    if not cur:
        return False
    u = usage_check.get_usage_for(BASE / "creds" / f"{cur}.json", is_current=True)
    if not u:
        return False
    return usage_check.which_limit(u, threshold=PREEMPT_PCT,
                                   bucket=usage_check.bucket_for_model(model)) is None


def _nudge_stalled_windows() -> list:
    """한도로 멈춘 창에 이어가라는 줄을 넣는다. 실패해도 창 지키기는 멈추지 않는다."""
    if os.environ.get("AIROTATOR_NO_NUDGE"):
        return []   # 시험 중엔 실제 창을 건드리지 않는다(검수 반영: 시험이 실제 창 목록을 읽었다)
    try:
        import session_nudge
        return session_nudge.nudge_stalled(_usable_now, log=log)
    except Exception as e:
        log(f"이어가기: 확인 중 오류({type(e).__name__})")
        return []


def _tend_claimed(buckets) -> int:
    switched, stuck = False, []
    for name in buckets:
        # keep_windows: 떠 있는 창들을 다 만족하는 계정이 없으면 갈아끼우지 않는다.
        # 주기 작업이 창을 밀면 다음 회차가 되밀어 왕복한다(검수 재현 2026-09-06).
        rc = ensure_usable(bucket=name, keep_windows=True, threshold=PREEMPT_PCT)
        if rc == ENSURE_SWITCHED:
            switched = True
            log(f"창 지키기: 떠 있는 창의 {name} 칸이 바닥나 계정을 갈아끼웠다")
        elif rc == 2:
            stuck.append(name)
            log(f"창 지키기: 떠 있는 창의 {name} 칸이 바닥났지만 갈아탈 계정이 없다")
    if stuck:
        print(f"[창 지키기] 떠 있는 창의 칸({', '.join(stuck)})이 바닥났지만 갈아탈 계정이 없음")
    elif not switched:
        print(f"[창 지키기] 떠 있는 창의 칸({', '.join(buckets)}) 그대로 쓸 수 있음")
    return ENSURE_SWITCHED if switched else 0


def claim_session(pid, model: str | None = None, argv=None) -> str | None:
    """사람이 쓰는 창이 이 모델 칸을 잡았다고 적어둔다. 잡은 칸 이름을 돌려준다.

    창이 살아 있는 동안 봇 호출이 그 칸 막힌 계정으로 갈아끼우지 못하게 하는 표시다.
    모델을 못 알아내면 아무것도 안 적는다 — 그때는 계정 단위 판단이라 표시해도 달라질 게 없다.
    잠금은 짧게만 기다린다. 창 켜기를 늦추느니 표시를 포기하는 편이 낫다.
    같은 프로세스 번호로 다시 부르면 칸만 갱신한다(창 안에서 모델을 바꾼 경우).
    """
    if usage_check is None:
        return None
    try:
        want = model if model is not None else intended_model(argv)
        bucket = usage_check.bucket_for_model(want)
    except Exception:
        return None
    if not bucket:
        return None
    slot = slot_pool.slot_of_env()   # cc 가 자리 폴더를 물려줬으면 그 자리(없으면 None = 공용 자격)
    try:
        with st.with_lock(timeout=CLAIM_LOCK_TIMEOUT):
            state = st.load_state()
            st.prune_claims(state)
            label = slot_pool.slot_current(state, slot) if slot else state.get("current")
            st.note_session_claim(state, pid, bucket, label, model=want, slot=slot)
            st.save_state(state)
    except Exception:
        return None
    return bucket


def release_session(pid) -> bool:
    """창이 닫혔다고 표시를 지운다. 못 지워도 프로세스가 죽으면 저절로 걷힌다."""
    try:
        with st.with_lock(timeout=CLAIM_LOCK_TIMEOUT):
            state = st.load_state()
            st.drop_session_claim(state, pid)
            st.save_state(state)
        return True
    except Exception:
        return False


def _wanted_buckets(state: dict, bucket=None) -> tuple:
    """이번 호출이 쓰는 모델 칸 + 지금 떠 있는 창들이 잡고 있는 칸.

    자격은 이 컴퓨터에 한 벌뿐이라, 계정을 갈아끼우면 떠 있는 창까지 같이 끌려간다.
    그래서 계정을 고를 때는 **이번 호출과 떠 있는 창들이 다 쓸 수 있는** 계정을 찾는다.
    2026-08-22 실사고: Fable 로 켠 창이 B 를 쓰고 있는데, Opus 로 부르는 봇 호출이
    "앞순위 A 가 안 막혔다"며 10분마다 A 로 되돌려 창이 계속 Fable 한도 문구를 받았다.
    """
    names = list(st.bucket_names(bucket))
    for name in st.claimed_buckets(state):
        if name not in names:
            names.append(name)
    return tuple(names)


def _dead_everywhere(state: dict, buckets) -> set:
    """그 칸들 중 **살아 있는 계정 전부**(지금 계정 포함)에서 막힌 칸. 기록만 본다(조회 없음)."""
    now = time.time()
    labels = [l for l in st.usable_labels() if not st.is_blocked(state, label=l, now=now)]
    out = set()
    for name in st.bucket_names(buckets):
        if all(st.is_scoped_blocked(state, l, name, now) for l in labels):
            out.add(name)
    return out


def _pick_account(state: dict, exclude: str | None, bucket=None, strict: bool = False):
    """갈아탈 계정 하나. 떠 있는 창의 칸까지 만족하는 계정을 먼저 찾는다.

    다 만족하는 계정이 없으면 이번 호출 칸만 보고 고른다 — 봇을 멈추는 것보다 창이
    한 번 밀리는 편이 낫다(밀린 창은 다시 켜면 제자리를 찾는다). 그때는 로그를 남긴다.
    `strict` 면 그 폴백을 안 한다(None). 주기 작업(--tend)이 쓴다 — 되풀이되는 자리에서
    창을 밀면 다음 회차가 되밀어 3분마다 왕복한다(2026-09-06 검수 재현: Fable 창 + Opus 창,
    두 칸을 다 만족하는 계정이 없을 때 B→A→B→A 무한 반복).
    """
    wanted = _wanted_buckets(state, bucket)
    nxt = st.pick_next(state, exclude=exclude, bucket=wanted)
    if nxt is not None or set(wanted) == set(st.bucket_names(bucket)):
        return nxt
    # 떠 있는 창의 칸 중 **어느 계정에서도 못 쓰는 칸**은 조건에서 뺀다. 2026-09-12 실사고:
    # Fable 이 네 계정 모두 바닥난 채로 Opus 창 여러 개가 떠 있었는데, "Fable 창까지 만족하는
    # 계정이 없다" 며 4시간 넘게 아무것도 안 갈아끼웠다. Opus 는 C·D 에 100% 남아 있었다.
    # 그 칸은 어디로 가도 못 쓰니 빼도 창끼리 밀어내는 왕복이 안 생긴다(각 칸을 서로 다른
    # 계정만 만족하는 경우는 그대로 strict 다). 지금 계정만 그 칸을 쓸 수 있으면 빼지 않는다 —
    # 일하는 창을 밀면서까지 다른 창을 살리지는 않는다.
    dead = _dead_everywhere(state, wanted)
    if dead and not (dead & set(st.bucket_names(bucket))):
        trimmed = tuple(n for n in wanted if n not in dead)
        nxt = st.pick_next(state, exclude=exclude, bucket=trimmed)
        if nxt is not None:
            log(f"떠 있는 창의 칸 중 {', '.join(sorted(dead))} 은 어느 계정에서도 못 써서 빼고 "
                f"'{nxt}' 로 갑니다(나머지 칸 {', '.join(trimmed) or '없음'} 은 만족).")
            return nxt
    if strict:
        claimed = ", ".join(st.claimed_buckets(state)) or "없음"
        log(f"떠 있는 창의 칸({claimed})까지 만족하는 계정이 없어 갈아끼우지 않습니다(주기 작업).")
        return None
    nxt = st.pick_next(state, exclude=exclude, bucket=bucket)
    if nxt is not None:
        claimed = ", ".join(st.claimed_buckets(state)) or "없음"
        log(f"떠 있는 창의 칸({claimed})까지 만족하는 계정이 없어 '{nxt}' 로 갑니다. "
            f"그 창은 다시 켜야 할 수 있습니다.")
    return nxt


def _revive_scoped(state: dict, bucket=None, below: float = PREEMPT_PCT) -> list:
    """막힘으로 적어둔 모델 칸 중 **실제로는 다시 열린 칸**을 풀어 준다. 푼 [(계정, 칸)].

    막힘 해제 시각은 막던 순간에 조회한 값이라, 그 뒤 실제 한도가 그보다 먼저 풀려도
    기록은 그대로 남는다. 2026-09-03 실사고: A 의 Fable 칸이 다 비어 있는데도 8월 31일에
    적힌 '09/05 21:00 풀림' 이 남아 A·B·D 세 계정이 전부 막힌 것처럼 보였고, cc 가
    갈아탈 계정을 못 찾아 소진된 계정에 그대로 눌러앉았다.

    **갈아탈 계정이 없다고 결론내기 직전에만 부른다.** 평소 경로에서는 조회를 한 번도
    늘리지 않는다(잦은 조회는 429 를 부른다).
    사용량을 못 읽은 계정은 손대지 않는다 — 못 읽는 것과 열린 것은 다르고, 멀쩡한
    막힘을 지우면 소진된 계정으로 다시 갈아타 거절만 받는다.
    """
    if usage_check is None:
        return []
    names = st.bucket_names(bucket)
    if not names:
        return []   # 칸을 안 보는 호출이면 칸 막힘이 후보를 줄인 것도 아니다
    now = time.time()
    cur = state.get("current")
    freed = []
    for label in st.usable_labels():
        blocked = st.scoped_blocked_buckets(state, label, now) & set(names)
        if not blocked:
            continue
        u = usage_check.get_usage_for(BASE / "creds" / f"{label}.json",
                                      is_current=(label == cur))
        if not u:
            continue
        st.note_usage(state, label, u)
        st.note_weekly_reset(state, label, u.get("weekly_reset"))
        for name in sorted(blocked):
            pct = usage_check.scoped_pct(u, name)
            if pct is None or pct >= below:
                continue    # 못 읽었거나 찼다(미리 넘긴 문턱 위도 찬 것으로 본다)
            if st.clear_scoped_bucket(state, label, name):
                freed.append((label, name))
                log(f"'{label}' 의 {name} 칸이 실제로는 열려 있어 막힘을 풉니다 "
                    f"(남은 {max(0, 100 - pct):.0f}%).")
    return freed


def _revive_account_blocks(state: dict, usages: dict | None = None,
                           probe: bool = True, below: float = PREEMPT_PCT) -> list:
    """계정 전체 막힘 중 **실제로는 이미 풀린 것**을 푼다. 푼 계정 목록.

    막을 때 붙이는 식힘 시간은 고정값(기본 5시간)이라 실제보다 훨씬 길게 잡힌다.
    세션 한도는 창이 열린 시점부터 5시간이라, 막히는 순간에는 이미 대부분 지나 있다.
    2026-09-04 실측: A 를 14:27 에 세션 한도로 막고 19:27 까지 잡았는데 실제로는
    14:39 에 풀렸다. 그 사이 A 는 주간이 67% 남았는데도 후보에서 빠져 있었다.

    푸는 쪽이라 안전하다 — 잘못 풀어도 그 계정으로 갈아탔을 때 거절받으면 다시 막힌다.
    반대로 잘못 막으면 멀쩡한 계정을 몇 시간 놀린다(2026-07-29 사고).
    **모르는 것은 열린 것으로 보지 않는다** — 사용량을 못 읽었거나 값이 안 온 계정은
    손대지 않는다.
    `usages` 를 주면 그 값을 쓴다(화면이 이미 읽어둔 값 재활용). `probe=False` 면
    거기 없는 계정은 그냥 건너뛴다 — 잠금을 쥔 채 서버를 기다리지 않으려는 것이다.
    """
    if usage_check is None:
        return []
    now = time.time()
    cur = state.get("current")
    freed = []
    for label, until in list((state.get("blocked") or {}).items()):
        try:
            if float(until) <= now:
                continue
        except (TypeError, ValueError):
            continue
        u = (usages or {}).get(label)
        if u is None:
            if not probe:
                continue
            u = usage_check.get_usage_for(BASE / "creds" / f"{label}.json",
                                          is_current=(label == cur))
        if not u:
            continue
        wk, se = u.get("weekly_pct"), u.get("session_pct")
        if wk is None or se is None:
            continue
        if float(wk) >= below or float(se) >= below:
            continue    # 찼거나, 창 지키기가 미리 넘긴 문턱 위다(풀면 되돌림이 도로 끌고 온다)
        if st.clear_account_block(state, label, now):
            freed.append(label)
            log(f"'{label}' 계정이 실제로는 열려 있어 막힘을 풉니다"
                f" (세션 남은 {max(0.0, 100 - float(se)):.0f}%,"
                f" 주간 남은 {max(0.0, 100 - float(wk)):.0f}%).")
    return freed


def _remember_and_revive(seen) -> set:
    """화면이 읽은 사용량을 기록하면서 낡은 계정 막힘도 같이 푼다. 푼 계정 집합.

    한 번의 잠금 안에서 끝낸다(st.remember_usage 와 겹쳐 부르면 잠금이 중첩된다).
    화면은 어차피 모든 계정 사용량을 읽으므로 여기서 푸는 데 조회가 더 안 든다.
    """
    pairs = [(l, u) for l, u in seen if u]
    if not pairs:
        return set()
    freed = []
    with st.with_lock():
        s = st.prune_blocked(st.load_state())
        for l, u in pairs:
            st.note_usage(s, l, u)
            st.note_weekly_reset(s, l, u.get("weekly_reset"))
        freed = _revive_account_blocks(s, dict(pairs), probe=False)
        st.save_state(s)
    return set(freed)


def sweep_stale_blocks(min_interval: float = 1800) -> list:
    """막힌 계정 중 **실제로는 이미 풀린 것**을 걷어낸다. 주기 작업이 부르는 자리.

    사람이 화면(--status)을 안 봐도 낡은 막힘이 걷히게 한다. 봇만 도는 머신은 아무도
    화면을 안 켜서, 식힘 시간이 실제보다 길게 잡힌 계정이 몇 시간씩 논다.
    막힌 계정만 조회한다(대개 0~2개). `min_interval` 안에 이미 봤으면 건너뛴다 —
    10분마다 부르는 자리에 걸려도 조회가 그만큼 늘지 않는다(잦은 조회는 429 를 부른다).
    """
    now = time.time()
    with st.with_lock():
        state = st.prune_blocked(st.load_state())
        if not (state.get("blocked") or {}):
            st.save_state(state)     # 시간이 지나 저절로 걷힌 것만 반영하고 끝
            return []
        try:
            last = float(state.get("revive_checked_at") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if now - last < min_interval:
            return []
        freed = _revive_account_blocks(state)
        state["revive_checked_at"] = now
        st.save_state(state)
    return freed


def _pick_or_revive(state: dict, exclude: str | None, bucket=None, strict: bool = False,
                    below: float = PREEMPT_PCT):
    """갈아탈 계정 하나. 없으면 낡은 칸 막힘을 실제 사용량으로 한 번 검증하고 다시 고른다.

    `below`: 이 % 미만이면 열린 것으로 보고 되살린다. 창 지키기(97)는 97, cc 켜기 전(100)은 100 —
    cc 는 97~99% 계정도 쓸 수 있는데 97 로 보면 "갈아탈 계정 없음" 이 된다(검수 반영 2026-09-10).
    """
    nxt = _pick_account(state, exclude=exclude, bucket=bucket, strict=strict)
    if nxt is not None:
        return nxt
    # 계정 전체 막힘부터 본다. 식힘 시간이 실제보다 길게 잡혀 있는 경우가 흔하다.
    if _revive_account_blocks(state, below=below):
        nxt = _pick_account(state, exclude=exclude, bucket=bucket, strict=strict)
    if nxt is not None:
        return nxt
    if _revive_scoped(state, _wanted_buckets(state, bucket), below=below):
        nxt = _pick_account(state, exclude=exclude, bucket=bucket, strict=strict)
    return nxt


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
    # 되돌리기는 **떠 있는 창까지 만족할 때만** 한다. 순차 소진은 아껴 쓰자는 것이고,
    # 사람이 쓰는 창을 못 쓰게 만들면서까지 지킬 이유는 없다(폴백 없음).
    nxt = st.better_candidate(state, cur, bucket=_wanted_buckets(state, bucket))
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
    if (not st.may_reclaim(state)
            or st.better_candidate(state, cur, bucket=_wanted_buckets(state, bucket)) is None):
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
        nxt = _pick_account(s, exclude=cur, bucket=bucket)
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


def _settle_pending_switch() -> tuple:
    """열쇠꾸러미까지 못 맞춘 계정 전환 예약이 남아 있으면 지금 마무리한다. 마무리했으면 True.

    맥에서 원격 접속 창이나 화면이 잠긴 자리에서 계정을 바꾸면 **자격 파일만 바뀌고
    열쇠꾸러미는 그대로 남는다**(switch_account 가 예약만 걸어 둔다). 그 사이 화면 세션에서
    도는 봇은 열쇠꾸러미의 **옛 계정**으로 호출한다. 그때 오는 한도 거절은 지금 계정 것이 아니다.
    2026-08-22 실사고: A(Fable 0%)의 거절이 B 앞으로 찍혀 B 가 5시간 통째로 막혔다.
    B 는 Fable 이 93% 남은 멀쩡한 계정이었고, B 자격으로 직접 불러 보니 정상이었다.

    **True/False 가 곧 "이 거절을 누가 받았나" 의 답이다.**
    - True(마무리됨) = 여기는 열쇠꾸러미를 만질 수 있는 화면 세션이고, 조금 전까지 어긋나
      있었다 = 이 호출은 옛 계정으로 나갔다 → 지금 계정을 막으면 안 된다.
    - False = 예약이 아예 없었거나(정상), 열쇠꾸러미를 못 만지는 자리다. 못 만지는 자리면
      이 호출도 자격 파일을 썼으므로 지금 계정이 맞다 → 평소대로 판단한다.

    돌려주는 값: (마무리했는지, 마무리한 계정 라벨). 라벨을 같이 주는 이유는 그게 지금
    **실제로 꽂힌 자격**이기 때문이다. 예약은 순환기를 안 거친 손 작업(switch_account 직접
    실행)으로도 생기므로 상태의 계정과 다를 수 있고, 그때 상태를 안 맞추면 다음 거절이
    또 엉뚱한 계정에 찍힌다(2026-08-22 코덱스 검수, 재현함).

    되비추기가 찍는 줄은 **표준 출력 통로로 나간다**. 봇은 이 호출의 출력을 그대로 받아
    JSON 으로 읽으므로, 여기서 새어 나가면 답변 앞에 로그가 붙어 파싱이 깨진다
    (같은 검수, 재현함). 받아서 이 파일 기록으로 옮긴다.
    """
    try:
        import cred_mirror
    except Exception:
        return False, None
    label = None
    try:
        label = (st.CREDS_DIR / ".pending-switch").read_text(encoding="utf-8").strip() or None
    except Exception:
        label = None
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            done = cred_mirror.apply_pending() == "done"
    except Exception:
        done = False
    leaked = buf.getvalue().strip()
    if leaked:
        for line in leaked.splitlines():
            log(line.strip())
    return (True, label) if done else (False, None)


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
            settled, settled_label = _settle_pending_switch() if verdict == "limit" else (False, None)
            if settled:
                # 자격 파일과 열쇠꾸러미가 어긋나 있었고 방금 맞췄다. 이 거절은 맞추기 전
                # 자격(열쇠꾸러미에 남아 있던 옛 계정)이 받은 것이라 지금 계정 탓이 아니다.
                # 막으면 멀쩡한 계정이 몇 시간 잠긴다(2026-08-22 B 계정 실사고).
                log(f"자격 파일과 열쇠꾸러미가 어긋나 있었습니다. 방금 맞췄고 "
                    f"'{used_label}' 은 막지 않습니다. 같은 계정으로 다시 시도합니다.")
                # 방금 정착시킨 계정을 다음 시도가 곧바로 앞순위로 되돌리면 재시도가 헛돈다
                # (2026-08-22 코덱스 검수, 재현: 두 번 다 옛 계정으로 나가 실패로 끝났다).
                # 되돌리기 간격을 지금부터 새로 센다.
                st.note_reclaim(state)
                if settled_label and settled_label != current:
                    # 지금 실제로 꽂힌 자격은 예약에 적혀 있던 계정이다. 상태를 안 맞추면
                    # 다음 거절이 또 엉뚱한 계정에 찍힌다(예약은 손 작업으로도 생긴다).
                    st.set_current(state, settled_label,
                                   f"{current or '?'} → {settled_label} (예약된 전환 마무리)")
                    log(f"실제로 꽂힌 자격은 '{settled_label}' 이라 상태도 그쪽으로 맞췄습니다.")
                st.save_state(state)
                return settled_label or used_label or current
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

        nxt = _pick_account(state, exclude=used_label, bucket=bucket)
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
            _hidden = _hidden_child_flags()
            if _hidden:
                kwargs.setdefault("creationflags", _hidden)
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
    if argv[0] == "--sweep":
        # 낡은 계정 막힘만 걷어낸다(주기 작업용). 조용히 끝나는 게 정상이다.
        freed = sweep_stale_blocks()
        print("막힘 해제: " + ", ".join(freed) if freed else "걷어낼 낡은 막힘 없음")
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
    if argv[0] == "--tend":
        # 떠 있는 사람 창의 칸이 바닥났으면 갈아끼운다(주기 작업용, 창 없으면 조회 0).
        return tend_windows()
    if argv[0] in ("--slot-prepare", "--slot-next"):
        # cc 가 켜기 직전(또는 한도로 끝난 뒤 되켤 때) 부른다. 표준출력에는 자리 폴더 경로만 찍는다
        # (cc 가 그 줄을 CLAUDE_CONFIG_DIR 로 쓴다). 저장본이 없는 칸이면 아무것도 안 찍는다 = 예전 방식.
        bucket = usage_check.bucket_for_model(intended_model(argv[1:])) if usage_check else None
        if argv[0] == "--slot-next":
            if not (bucket and slot_pool.enabled() and slot_pool.stored_labels(bucket)):
                return 0
            rc, cur = slot_pool.ensure(bucket, threshold=100.0, force_next=True)
            if cur:
                print(str(slot_pool.slot_dir(bucket)))
            return rc
        rc, path = slot_pool.prepare(bucket)
        if path:
            print(path)
        return rc
    if argv[0] == "--slot-status":
        lines = slot_pool.status_lines(st.load_state())
        print("\n".join(lines) if lines else "자리(slot) 없음 — 저장본이 있는 칸이 없습니다.")
        return 0
    if argv[0] == "--use":
        if len(argv) < 2:
            print("사용법: python claude_pool.py --use <라벨>")
            return 1
        return use(argv[1].strip())
    if argv[0] == "--claim":
        # 뒤에 붙는 건 켜려는 claude 의 인자다. --model 이 없으면 settings.json 고정값을 본다.
        if len(argv) < 2:
            print("사용법: python claude_pool.py --claim <프로세스번호> [claude 인자...]")
            return 1
        got = claim_session(argv[1], argv=argv[2:])
        if got:
            # 단정하지 않는다 — 갈아탈 계정이 하나도 없으면 이번 호출을 살리려고
            # 이 칸을 포기하는 갈래가 있다(_pick_account 의 폴백).
            print(f"[창 표시] 이 창이 {got} 칸을 씁니다. 봇 호출은 되도록 이 칸이 막힌 "
                  f"계정으로 안 넘어갑니다(다른 계정이 하나도 없으면 밀릴 수 있습니다).")
        return 0
    if argv[0] == "--unclaim":
        if len(argv) < 2:
            print("사용법: python claude_pool.py --unclaim <프로세스번호>")
            return 1
        release_session(argv[1])
        return 0
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
