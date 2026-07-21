"""
== 운영 맥락 ==
용도: claude 실행 파일 자리에 대신 앉아, 봇들의 단발 호출(-p)만 계정 순환 보호 아래로
      넘긴다. 봇 코드 43개 파일 55곳을 각각 고치는 대신 이 한 곳으로 전부 덮는다.
설치: /opt/homebrew/bin/claude 심볼릭 링크를 install_shim.sh 로 이 스크립트를 가리키게 바꾼다.
      되돌리기: install_shim.sh --uninstall
동작: 아래 어느 하나라도 해당하면 아무것도 안 하고 진짜 claude 로 그대로 넘긴다(투명).
      - 대화형 실행 (-p / --print 가 없음): 사람이 쓰는 창은 건드리지 않는다
      - 자격 저장본이 2개 미만: 순환할 계정이 없으니 끼어들 이유가 없다
      - CLAUDE_ROTATOR_DISABLED=1 : 손으로 끈 경우
      - 이미 순환기 안에서 부른 호출 (되돌이 방지)
      그 밖의 단발 호출만 claude_pool.run 으로 실행한다. 한도 거절이면 계정을 바꿔 재시도.
안전: 순환기 쪽에서 무슨 문제가 생기든 마지막에는 진짜 claude 를 그대로 실행한다.
      순환기가 죽어도 봇이 멈추지 않는 것이 이 파일의 첫 번째 목표다.
주의: 단발 호출은 출력을 다 모은 뒤 한 번에 내보낸다(중간 출력을 실시간으로 흘리지 않음).
      완료 후 결과를 파싱하는 봇들에는 영향이 없다.
마지막 점검: 2026-07-22
"""

from __future__ import annotations

import os
import sys

REAL_CLAUDE = os.environ.get(
    "CLAUDE_REAL_BIN",
    "/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
)
GUARD_ENV = "CLAUDE_ROTATOR_ACTIVE"
DISABLE_ENV = "CLAUDE_ROTATOR_DISABLED"
ROTATOR_DIR = os.path.dirname(os.path.abspath(__file__))
PRINT_FLAGS = {"-p", "--print"}


def passthrough(argv):
    """진짜 claude 로 완전히 넘긴다. 이 프로세스를 그대로 대체하므로 흔적이 안 남는다."""
    os.execv(REAL_CLAUDE, [REAL_CLAUDE, *argv])


def is_single_shot(argv) -> bool:
    for a in argv:
        if a in PRINT_FLAGS:
            return True
        if a.startswith("--print="):
            return True
    return False


def main():
    argv = sys.argv[1:]

    if not os.path.exists(REAL_CLAUDE):
        sys.stderr.write(f"[shim] 진짜 claude 를 못 찾았습니다: {REAL_CLAUDE}\n")
        return 127

    if os.environ.get(GUARD_ENV) or os.environ.get(DISABLE_ENV):
        passthrough(argv)
    if not is_single_shot(argv):
        passthrough(argv)

    # 여기서부터 순환 대상. 순환기를 못 불러오면 그냥 원래대로 넘긴다.
    try:
        if ROTATOR_DIR not in sys.path:
            sys.path.insert(0, ROTATOR_DIR)
        import claude_pool
        import pool_state as st

        if len(st.saved_labels()) < 2:
            passthrough(argv)  # 순환할 계정이 없음
    except Exception:
        passthrough(argv)

    stdin_data = None
    if not sys.stdin.isatty():
        try:
            stdin_data = sys.stdin.read()
        except Exception:
            stdin_data = None

    env = dict(os.environ)
    env[GUARD_ENV] = "1"  # 순환기가 부르는 claude 가 다시 이 파일로 오지 않게

    try:
        proc = claude_pool.run(
            [REAL_CLAUDE, *argv], input=stdin_data, env=env,
            capture_output=True, text=True,
        )
    except claude_pool.AllAccountsExhausted as e:
        sys.stderr.write(f"[shim] {e}\n")
        return 1
    except Exception as e:
        # 순환기 문제로 봇을 멈추지 않는다. 원래 방식으로 한 번 더 실행한다.
        sys.stderr.write(f"[shim] 순환기 오류({type(e).__name__}) — 원래 방식으로 실행합니다\n")
        import subprocess

        proc = subprocess.run([REAL_CLAUDE, *argv], input=stdin_data,
                              capture_output=True, text=True, env=env)

    sys.stdout.write(proc.stdout or "")
    sys.stdout.flush()
    sys.stderr.write(proc.stderr or "")
    sys.stderr.flush()
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
