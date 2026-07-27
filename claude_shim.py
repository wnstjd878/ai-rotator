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
대체: 구독 계정이 전부 한도에 걸리고 부르는 쪽이 CLAUDE_API_FALLBACK=1 을 준 호출만
      api_fallback 이 직접 호출로 대신 답한다(회원이 기다리는 일만 — 2026-07-27).
      봇 입장에서는 CLI 가 답한 것과 같은 모양이라 코드를 안 고쳐도 된다(2026-07-27).
안전: 순환기 쪽에서 무슨 문제가 생기든 마지막에는 진짜 claude 를 그대로 실행한다.
      순환기가 죽어도 봇이 멈추지 않는 것이 이 파일의 첫 번째 목표다.
주의: 단발 호출은 출력을 다 모은 뒤 한 번에 내보낸다(중간 출력을 실시간으로 흘리지 않음).
      완료 후 결과를 파싱하는 봇들에는 영향이 없다.
마지막 점검: 2026-07-22
"""

from __future__ import annotations

import os
import sys

GUARD_ENV = "CLAUDE_ROTATOR_ACTIVE"
DISABLE_ENV = "CLAUDE_ROTATOR_DISABLED"
FALLBACK_ENV = "CLAUDE_API_FALLBACK"   # 이 값이 "1" 인 호출만 직접 호출로 대체
ROTATOR_DIR = os.path.dirname(os.path.abspath(__file__))
PRINT_FLAGS = {"-p", "--print"}

# 진짜 claude 를 찾을 자리. 판이 올라가며 경로가 바뀔 수 있으므로 여러 곳을 본다.
# 설치 스크립트가 CLAUDE_REAL_BIN 을 넘겨주면 그게 최우선이다(아래 find_real_claude).
REAL_CANDIDATES = [
    "/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
    "/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js",
    "/usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
    os.path.expanduser("~/.local/share/claude/claude"),
    # Windows(winserver): 설치 스크립트가 원본을 claude-real.exe 로 옆에 밀어둔다
    os.path.expanduser("~/.local/bin/claude-real.exe"),
    os.path.expanduser("~/AppData/Local/Programs/claude/claude-real.exe"),
]


def find_real_claude() -> str | None:
    """진짜 claude 실행 파일. 못 찾으면 None.

    가로채기 자신을 다시 부르지 않도록, 찾은 경로가 이 파일을 가리키면 건너뛴다.
    """
    override = os.environ.get("CLAUDE_REAL_BIN")
    if override and os.path.exists(override):
        return override

    here = os.path.realpath(__file__)
    here_dir = os.path.dirname(here)
    for cand in REAL_CANDIDATES:
        if os.path.exists(cand) and os.path.dirname(os.path.realpath(cand)) != here_dir:
            return cand

    # 마지막 수단: PATH 에서 찾되 가로채기 자신은 뺀다
    import shutil

    found = shutil.which("claude")
    if found and os.path.dirname(os.path.realpath(found)) != here_dir:
        return found
    return None


REAL_CLAUDE = find_real_claude()


def passthrough(argv):
    """진짜 claude 로 완전히 넘긴다.

    유닉스는 execv 로 이 프로세스를 그대로 대체한다(흔적 안 남음).
    Windows 는 execv 가 부모를 즉시 끝내버려 부르는 쪽(cmd/봇)이 출력을 놓칠 수 있으므로,
    자식을 끝까지 기다렸다가 그 종료 코드로 나간다.
    """
    if os.name == "nt":
        import subprocess

        proc = subprocess.run([REAL_CLAUDE, *argv])
        sys.exit(proc.returncode)
    os.execv(REAL_CLAUDE, [REAL_CLAUDE, *argv])


def is_single_shot(argv) -> bool:
    for a in argv:
        if a in PRINT_FLAGS:
            return True
        if a.startswith("--print="):
            return True
    return False


# 값을 뒤에 하나 더 받는 것으로 아는 플래그. 여기 없는 플래그도 값을 받는 쪽으로
# 보수적으로 처리한다(프롬프트를 값으로 잘못 보면 표준입력을 읽어 원래 동작이 되지만,
# 반대로 값을 프롬프트로 잘못 보면 표준입력 프롬프트가 통째로 사라지기 때문).
VALUE_FLAGS = {
    "--model", "--system-prompt", "--append-system-prompt", "--output-format",
    "--input-format", "--allowedTools", "--allowed-tools", "--disallowedTools",
    "--disallowed-tools", "--tools", "--setting-sources", "--json-schema",
    "--max-budget-usd", "--effort", "--permission-mode", "--max-turns",
    "--mcp-config", "--add-dir", "--session-id", "--cwd", "--resume",
}


def has_prompt_argument(argv) -> bool:
    """프롬프트가 인자로 이미 들어 있는지. 있으면 claude 는 표준입력을 안 쓴다."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            return i + 1 < len(argv)
        if a.startswith("-"):
            if "=" in a:
                i += 1
                continue
            if a in PRINT_FLAGS or a in ("--verbose", "--safe-mode", "--no-chrome",
                                         "--disable-slash-commands", "--no-session-persistence",
                                         "--strict-mcp-config", "--version", "--help"):
                i += 1
                continue
            i += 2  # 모르는 플래그는 값을 받는 것으로 본다
            continue
        return True  # 플래그가 아닌 토큰 = 프롬프트
    return False


def main():
    argv = sys.argv[1:]

    if not REAL_CLAUDE:
        sys.stderr.write("[shim] 진짜 claude 를 못 찾았습니다. "
                         "install_shim.sh --uninstall 로 되돌리세요.\n")
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

        # 실제로 고를 수 있는 계정 기준으로 판단한다. 저장본 개수만 보면
        # accounts.json 에 없는 라벨로 저장했을 때 가로채기는 켜지고 고를 계정은 없는
        # 상태가 되어 첫 한도에서 곧바로 전 계정 소진이 된다.
        usable = set(st.saved_labels()) & set(st.account_labels())
        if len(usable) < 2:
            passthrough(argv)  # 순환할 계정이 없음
    except Exception:
        passthrough(argv)

    # 프롬프트가 인자에 이미 있으면 표준입력을 건드리지 않는다. 봇이 입력 통로를 열어두고
    # 닫지 않는 경우가 있는데, 그때 여기서 읽으면 진짜 claude 는 멀쩡히 끝나는 일에
    # 가로채기만 영원히 기다린다.
    stdin_data = None
    if not has_prompt_argument(argv) and not sys.stdin.isatty():
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
        # 구독이 전부 떨어졌다. 회원이 기다리는 일이면 직접 호출로 한 번 답해 본다(2026-07-27).
        # 켜는 쪽이 기본이 아니다 — 부르는 쪽이 CLAUDE_API_FALLBACK=1 을 준 호출만 대체한다.
        # 백그라운드 일(수집·분류·백필)은 구독이 풀릴 때까지 기다리는 게 맞다.
        sys.stderr.write(f"[shim] {e}\n")
        if os.environ.get(FALLBACK_ENV) != "1":
            return 1
        try:
            import api_fallback

            code, out, err = api_fallback.run(argv, stdin_data)
            if code == 0:
                sys.stdout.write(out)
                sys.stdout.flush()
                sys.stderr.write("[shim] 구독이 다 차서 API 로 답했습니다\n")
                return 0
            sys.stderr.write(err)
        except Exception as fe:
            sys.stderr.write(f"[shim] API 대체 실패({type(fe).__name__})\n")
        return 1
    except Exception as e:
        # 순환기 문제로 봇을 멈추지 않는다. 원래 방식으로 한 번 더 실행한다.
        sys.stderr.write(f"[shim] 순환기 오류({type(e).__name__}). 원래 방식으로 실행합니다\n")
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
