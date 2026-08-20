"""
== 운영 맥락 ==
용도: 3대(본체·Mac·winserver)의 claude CLI 를 최신 판으로 유지한다. 가로채기(shim)가
      깔린 기계는 스스로 갱신되지 못한다 — 설치 자리(claude.exe / PATH 의 claude)를
      가로채기가 차지하고 있어서, 갱신본이 엉뚱한 자리에 깔리거나 다음 재설치 때 지워진다.
      그래서 여기서 대신 갱신하고, 갱신 뒤에는 반드시 가로채기를 다시 씌운다.
실행 시점: Windows 본체 작업 스케줄러 `ai-rotator-auto-update-cli` 매일 09:25
      (본체는 꺼질 수 있는 PC 다 — 놓친 날은 켜진 뒤 곧바로 돈다.)
입력: 각 머신의 claude 실행 파일 `--version`
출력: auto_update_cli.log + 문제 있을 때만 텔레그램(notify.py)
왜 필요한가: 낡은 CLI 는 모델 별칭을 낡게 푼다. 2.1.219 미만에서는 `opus` 가 Opus 5 가
      아니라 Opus 4.8 을 가리킨다. 2026-08-20 실측으로 Mac 은 2.1.210, winserver 는
      7월 26일 판(2.1.220)에 갇혀 있었고 본체만 2.1.237 이었다. 그래서 같은 `opus` 를
      써도 기계마다 다른 판이 잡혔다.
의도적 미구현:
      1. 최신 판 번호를 서버에 묻지 않는다. 갱신 명령이 알아서 최신을 받아오므로,
         세 기계 중 가장 높은 판을 최신으로 보고 뒤처진 기계만 짚는다.
      2. 실호출로 검증하지 않는다(계정 한도를 태운다). 판 번호와 가로채기 상태로 갈음한다.
      3. 본체는 가로채기를 안 쓴다(설계상 정상 — 대화형은 cc 가 순환을 맡는다).
         그래서 본체는 갱신만 하고 재설치 단계가 없다.
마지막 점검: 2026-08-20
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import notify  # noqa: E402

LOG_FILE = BASE / "auto_update_cli.log"
UPDATE_TIMEOUT = 600   # npm 설치가 몇 분 걸릴 수 있다
SHORT_TIMEOUT = 180
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_WIN_CLAUDE = str(Path.home() / ".local" / "bin" / "claude.exe")
_MAC_REAL = "/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
_WS_REAL = r"& $env:USERPROFILE\.local\bin\claude-real.exe"
_WS_SHIM = r"C:\Users\pc\projects\ai-rotator\install_shim.ps1"

# 가로채기 상태 판정은 **영문 부분으로만** 한다. winserver 의 한글 출력은 이 통로를 지나며
# 글자가 깨져 오기 때문이다(아침 점검 봇도 같은 이유로 True/False 줄만 읽는다).
MACHINES = [
    {
        "name": "본체",
        "version": [_WIN_CLAUDE, "--version"],
        "update": [_WIN_CLAUDE, "update"],
        "shim_install": None,
        "shim_status": None,
    },
    {
        "name": "Mac",
        "version": ["ssh", "macmini", f"{_MAC_REAL} --version"],
        "update": ["ssh", "macmini",
                   "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH; "
                   "npm install -g @anthropic-ai/claude-code@latest"],
        "shim_install": ["ssh", "macmini",
                         "export PATH=/opt/homebrew/bin:$PATH; "
                         "bash ~/projects/ai-rotator/install_shim.sh"],
        "shim_status": ["ssh", "macmini", "ls -l /opt/homebrew/bin/claude"],
    },
    {
        "name": "winserver",
        "version": ["ssh", "winserver", f"{_WS_REAL} --version"],
        "update": ["ssh", "winserver", f"{_WS_REAL} update"],
        "shim_install": ["ssh", "winserver",
                         f"powershell -NoProfile -ExecutionPolicy Bypass -File {_WS_SHIM}"],
        "shim_status": ["ssh", "winserver",
                        f"powershell -NoProfile -ExecutionPolicy Bypass -File {_WS_SHIM} -Status"],
    },
]


def log(msg: str) -> None:
    line = time.strftime("%m-%d %H:%M:%S ") + msg
    print(line)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, creationflags=NO_WINDOW)
    except Exception as e:
        log(f"  실행 실패: {type(e).__name__}")
        return None


def version_of(cmd: list[str]):
    """claude --version 에서 판 번호 튜플. 못 읽으면 None."""
    r = run(cmd, SHORT_TIMEOUT)
    if r is None:
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", (r.stdout or "") + (r.stderr or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def as_text(ver) -> str:
    return ".".join(str(x) for x in ver) if ver else "확인 불가"


def shim_ok(machine) -> bool | None:
    """가로채기가 제자리에 있는지. 볼 필요가 없는 기계는 None."""
    cmd = machine.get("shim_status")
    if not cmd:
        return None
    r = run(cmd, SHORT_TIMEOUT)
    if r is None:
        return False
    out = (r.stdout or "") + (r.stderr or "")
    if machine["name"] == "Mac":
        # PATH 의 claude 가 가로채기 파일을 가리켜야 한다.
        return "claude_shim.sh" in out
    # winserver: 원래 실행 파일이 밀려 있고(claude.exe 없음) 래퍼가 있어야 한다.
    hidden = re.search(r"claude\.exe\s*:\s*(True|False)", out)
    wrapper = re.search(r"claude\.cmd\s*:\s*(True|False)", out)
    if not hidden or not wrapper:
        return False
    return hidden.group(1) == "False" and wrapper.group(1) == "True"


def handle(machine, dry: bool):
    """한 기계를 갱신하고 (이름, 갱신 후 판, 가로채기 상태, 문제 사유) 를 돌려준다."""
    name = machine["name"]
    before = version_of(machine["version"])
    log(f"{name}: 지금 {as_text(before)}")

    if dry:
        return name, before, shim_ok(machine), None

    r = run(machine["update"], UPDATE_TIMEOUT)
    if r is None or r.returncode != 0:
        rc = "실행 못 함" if r is None else f"rc={r.returncode}"
        log(f"{name}: 갱신 명령 실패({rc})")
        return name, before, shim_ok(machine), "갱신 명령이 실패했습니다"

    # 갱신은 가로채기를 벗긴다(Mac 은 심볼릭을 덮어쓰고, winserver 는 원래 이름을 되살린다).
    # 그래서 갱신에 성공했든 이미 최신이었든 항상 다시 씌운다.
    if machine.get("shim_install"):
        s = run(machine["shim_install"], SHORT_TIMEOUT)
        if s is None or s.returncode != 0:
            log(f"{name}: 가로채기 재설치 실패")

    after = version_of(machine["version"])
    ok = shim_ok(machine)
    if before and after and after > before:
        log(f"{name}: {as_text(before)} -> {as_text(after)} 로 올렸습니다")
    else:
        log(f"{name}: {as_text(after)} (그대로)")

    if after is None:
        return name, after, ok, "판 번호를 못 읽습니다"
    if ok is False:
        return name, after, ok, "계정 순환 가로채기가 벗겨졌습니다"
    return name, after, ok, None


def main() -> int:
    dry = "--dry-run" in sys.argv[1:]
    results = [handle(m, dry) for m in MACHINES]

    newest = max((v for _, v, _, _ in results if v), default=None)
    problems = []
    for name, ver, _ok, reason in results:
        if reason:
            problems.append(f"{name}: {reason}")
        elif newest and ver and ver < newest:
            problems.append(f"{name}: {as_text(ver)} 로 뒤처졌습니다"
                            f"(가장 높은 판 {as_text(newest)})")

    log(f"끝. 가장 높은 판 {as_text(newest)}, 문제 {len(problems)}건")

    if problems and not dry:
        # 낡은 CLI 는 모델 별칭을 낡게 풀어 엉뚱한 판이 잡힌다. 받은 사람이 바로 할 수 있는
        # 일을 적는다(12.2). 알림은 문제가 있을 때만 — 매일 잘 되는 걸 알릴 필요는 없다.
        notify.send(
            "클로드 명령도구 자동 갱신에서 걸린 게 있습니다.\n"
            + "\n".join(problems)
            + "\n낡은 채로 두면 모델을 'opus' 로 지정해도 옛 판(Opus 4.8)이 잡힙니다.\n"
              "본체에서 `python auto_update_cli.py` 를 한 번 실행해 보시고, "
              "그래도 같으면 개발 봇에 '고쳐줘' 라고 답장해 주세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
