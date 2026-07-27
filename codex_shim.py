"""
== 운영 맥락 ==
용도: codex 실행 파일 자리에 대신 앉아, 자동 호출(codex exec / codex review)만 계정 순환
      보호 아래로 넘긴다. 부르는 쪽(배포 교차검수 codex-review.sh 등)은 안 고친다.
설치: bash install_codex_shim.sh          (되돌리기: --uninstall, 확인: --status)
동작: 아래 어느 하나라도 해당하면 아무것도 안 하고 진짜 codex 로 그대로 넘긴다(투명).
      - 대화형 실행 (exec/review 서브커맨드가 아님): 사람이 쓰는 창은 건드리지 않는다
      - 자격 저장본이 2개 미만: 순환할 계정이 없으니 끼어들 이유가 없다
      - CODEX_ROTATOR_DISABLED=1 : 손으로 끈 경우
      - 이미 순환기 안에서 부른 호출 (되돌이 방지)
안전: 순환기 쪽에서 무슨 문제가 생기든 마지막에는 진짜 codex 를 그대로 실행한다.
      순환기가 죽어도 검수·자동 호출이 멈추지 않는 것이 이 파일의 첫 번째 목표다.
주의: 보호를 받는 호출은 출력을 다 모은 뒤 한 번에 내보낸다(중간 출력을 실시간으로
      흘리지 않음). 그래서 대화형은 일부러 건드리지 않는다.
마지막 점검: 2026-07-28
"""

from __future__ import annotations

import os
import sys

GUARD_ENV = "CODEX_ROTATOR_ACTIVE"
DISABLE_ENV = "CODEX_ROTATOR_DISABLED"
ROTATOR_DIR = os.path.dirname(os.path.abspath(__file__))
SINGLE_SHOT_CMDS = {"exec", "e", "review"}

# 진짜 codex 를 찾을 자리. 판이 올라가며 경로가 바뀔 수 있으므로 여러 곳을 본다.
REAL_CANDIDATES = [
    "/opt/homebrew/lib/node_modules/@openai/codex/bin/codex.js",
    "/usr/local/lib/node_modules/@openai/codex/bin/codex.js",
    os.path.expanduser("~/.local/bin/codex-real"),
    os.path.expanduser("~/AppData/Roaming/npm/codex-real.cmd"),
]


def find_real_codex() -> str | None:
    """진짜 codex 실행 파일. 못 찾으면 None. 가로채기 자신은 건너뛴다."""
    override = os.environ.get("CODEX_REAL_BIN")
    if override and os.path.exists(override):
        return override

    here_dir = os.path.dirname(os.path.realpath(__file__))
    for cand in REAL_CANDIDATES:
        if os.path.exists(cand) and os.path.dirname(os.path.realpath(cand)) != here_dir:
            return cand

    import shutil

    found = shutil.which("codex")
    if found and os.path.dirname(os.path.realpath(found)) != here_dir:
        return found
    return None


REAL_CODEX = find_real_codex()


def passthrough(argv):
    """진짜 codex 로 완전히 넘긴다.

    유닉스는 execv 로 이 프로세스를 그대로 대체한다(흔적 안 남음).
    Windows 는 execv 가 부모를 즉시 끝내버려 부르는 쪽이 출력을 놓칠 수 있으므로,
    자식을 끝까지 기다렸다가 그 종료 코드로 나간다.
    """
    if os.name == "nt":
        import subprocess

        proc = subprocess.run([REAL_CODEX, *argv])
        sys.exit(proc.returncode)
    os.execv(REAL_CODEX, [REAL_CODEX, *argv])


def is_single_shot(argv) -> bool:
    """자동 호출(비대화형)인지. 첫 번째 서브커맨드로만 판단한다."""
    for a in argv:
        if a.startswith("-"):
            continue
        return a in SINGLE_SHOT_CMDS
    return False


def main():
    argv = sys.argv[1:]

    if not REAL_CODEX:
        sys.stderr.write("[shim] 진짜 codex 를 못 찾았습니다. "
                         "install_codex_shim.sh --uninstall 로 되돌리세요.\n")
        return 127

    if os.environ.get(GUARD_ENV) or os.environ.get(DISABLE_ENV):
        passthrough(argv)
    if not is_single_shot(argv):
        passthrough(argv)

    # 여기서부터 순환 대상. 순환기를 못 불러오면 그냥 원래대로 넘긴다.
    try:
        if ROTATOR_DIR not in sys.path:
            sys.path.insert(0, ROTATOR_DIR)
        import codex_pool
        import pool_state as st

        # 실제로 고를 수 있는 계정 기준으로 판단한다. 저장본 개수만 보면
        # 계정 목록에 없는 라벨로 저장했을 때 가로채기는 켜지고 고를 계정은 없는
        # 상태가 되어 첫 한도에서 곧바로 전 계정 소진이 된다.
        usable = set(st.saved_labels()) & set(st.account_labels())
        if len(usable) < 2:
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
    env[GUARD_ENV] = "1"  # 순환기가 부르는 codex 가 다시 이 파일로 오지 않게

    try:
        proc = codex_pool.run(
            [REAL_CODEX, *argv], input=stdin_data, env=env,
            capture_output=True, text=True,
        )
    except codex_pool.AllAccountsExhausted as e:
        sys.stderr.write(f"[shim] {e}\n")
        return 1
    except Exception as e:
        # 순환기 문제로 자동 호출을 멈추지 않는다. 원래 방식으로 한 번 더 실행한다.
        sys.stderr.write(f"[shim] 순환기 오류({type(e).__name__}). 원래 방식으로 실행합니다\n")
        import subprocess

        proc = subprocess.run([REAL_CODEX, *argv], input=stdin_data,
                              capture_output=True, text=True, env=env)

    sys.stdout.write(proc.stdout or "")
    sys.stdout.flush()
    sys.stderr.write(proc.stderr or "")
    sys.stderr.flush()
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
