"""
== 운영 맥락 ==
용도: 3대(본체·Mac·winserver)의 claude CLI 와 본체·Mac 의 codex CLI 를 최신 판으로 유지한다. 가로채기(shim)가
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
_MAC_CODEX_REAL = "/opt/homebrew/lib/node_modules/@openai/codex/bin/codex.js"
_NPM_DIR = Path.home() / "AppData" / "Roaming" / "npm"
# 본체 codex: 가로채기를 씌웠으면 진짜는 codex-real.cmd, 아니면 codex.cmd 가 진짜다.
_WIN_CODEX = str(_NPM_DIR / ("codex-real.cmd" if (_NPM_DIR / "codex-real.cmd").exists() else "codex.cmd"))

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
        # PATH 의 claude 말고 nvm 폴더의 claude 까지 봐야 한다. 봇이 빌드하려고 nvm 을
        # 불러오면 그쪽 claude 가 앞에 와서 순환기를 건너뛴다(install_shim.sh 가 둘 다 덮는다).
        "shim_status": ["ssh", "macmini", "bash ~/projects/ai-rotator/install_shim.sh --status"],
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
    # codex CLI (2026-10-15 추가). 낡은 codex 는 새 모델을 못 본다. 0.144.1 에 갇힌 Mac 에서
    # gpt-5.5 가 은퇴하자 검수가 거절됐고, 새 판 모델(gpt-6-*)은 "requires a newer version" 로 막혔다.
    # winserver 에는 codex 가 없다. 본체는 가로채기 없이 갱신만 한다(지금 안 씌워져 있음).
    {
        "name": "Mac codex",
        "tool": "codex",
        "version": ["ssh", "macmini",
                    f"export PATH=/opt/homebrew/bin:$PATH; node {_MAC_CODEX_REAL} --version"],
        "update": ["ssh", "macmini",
                   "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH; "
                   "npm install -g @openai/codex@latest"],
        # npm 갱신이 /opt/homebrew/bin/codex 의 순환 가로채기를 덮어쓴다(2026-10-15 실측).
        "shim_install": ["ssh", "macmini",
                         "export PATH=/opt/homebrew/bin:$PATH; "
                         "bash ~/projects/ai-rotator/install_codex_shim.sh"],
        "shim_status": ["ssh", "macmini", "bash ~/projects/ai-rotator/install_codex_shim.sh --status"],
    },
    {
        "name": "본체 codex",
        "tool": "codex",
        "version": [_WIN_CODEX, "--version"],
        "update": ["npm.cmd", "install", "-g", "@openai/codex@latest"],
        "shim_install": None,
        "shim_status": None,
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


def judge_shim(name: str, out: str) -> bool:
    """상태 출력 글에서 가로채기가 제자리인지 가린다(통신 없이 글자만 본다 — 시험용)."""
    if name == "Mac codex":
        return "codex_shim.sh" in out and "안 덮임" not in out
    if name == "Mac":
        # 연결이 가로채기 파일을 가리켜야 하고, nvm 쪽에 '안 덮임' 이 하나도 없어야 한다.
        return "claude_shim.sh" in out and "안 덮임" not in out
    # winserver: 원래 실행 파일이 밀려 있고(claude.exe 없음) 래퍼가 있어야 한다.
    # 저쪽 한글은 이 통로를 지나며 깨지므로 True/False 줄로만 판정한다.
    hidden = re.search(r"claude\.exe\s*:\s*(True|False)", out)
    wrapper = re.search(r"claude\.cmd\s*:\s*(True|False)", out)
    if not hidden or not wrapper:
        return False
    return hidden.group(1) == "False" and wrapper.group(1) == "True"


def shim_ok(machine) -> bool | None:
    """가로채기가 제자리에 있는지. 볼 필요가 없는 기계는 None."""
    cmd = machine.get("shim_status")
    if not cmd:
        return None
    r = run(cmd, SHORT_TIMEOUT)
    if r is None:
        return False
    return judge_shim(machine["name"], (r.stdout or "") + (r.stderr or ""))


def verdict(ver, ok, install_failed: bool = False) -> str | None:
    """이 기계 상태를 한 줄 사유로. 문제 없으면 None.

    재설치 명령이 실패하면 상태 확인이 통과해도 문제로 본다. install_shim.sh 는 자리를
    여러 곳(PATH·nvm) 덮는데 일부만 실패할 수 있어서, 명령 실패를 기록만 하고 넘기면
    '문제 0건' 으로 끝난다(2026-08-20 코덱스 검수 지적).
    """
    if ver is None:
        return "판 번호를 못 읽습니다"
    if ok is False:
        return "계정 순환 가로채기가 벗겨졌습니다"
    if install_failed:
        return "가로채기 재설치 명령이 실패했습니다"
    return None


def handle(machine, dry: bool):
    """한 기계를 갱신하고 (이름, 갱신 후 판, 가로채기 상태, 문제 사유) 를 돌려준다."""
    name = machine["name"]
    before = version_of(machine["version"])
    log(f"{name}: 지금 {as_text(before)}")

    if dry:
        # 확인만 하는 회차에도 판정은 똑같이 한다. 여기서 '문제 없음' 으로 흘리면
        # 사람이 확인하려고 돌린 회차가 오히려 거짓 안심을 준다(코덱스 검수 지적).
        ok = shim_ok(machine)
        return name, before, ok, verdict(before, ok)

    r = run(machine["update"], UPDATE_TIMEOUT)
    if r is None or r.returncode != 0:
        rc = "실행 못 함" if r is None else f"rc={r.returncode}"
        log(f"{name}: 갱신 명령 실패({rc})")
        return name, before, shim_ok(machine), "갱신 명령이 실패했습니다"

    # 갱신은 가로채기를 벗긴다(Mac 은 심볼릭을 덮어쓰고, winserver 는 원래 이름을 되살린다).
    # 그래서 갱신에 성공했든 이미 최신이었든 항상 다시 씌운다.
    install_failed = False
    if machine.get("shim_install"):
        s = run(machine["shim_install"], SHORT_TIMEOUT)
        if s is None or s.returncode != 0:
            install_failed = True
            log(f"{name}: 가로채기 재설치 실패")

    after = version_of(machine["version"])
    ok = shim_ok(machine)
    if ok is False and machine.get("shim_install"):
        # 갱신 직후에는 자리가 아직 정리되는 중일 수 있다. 2026-08-20 실측으로 Mac 의
        # nvm 쪽 claude 가 한 번 덮이지 않은 채 남았고, 다시 씌우니 바로 붙었다.
        log(f"{name}: 가로채기가 아직 벗겨져 있어 한 번 더 씌웁니다")
        run(machine["shim_install"], SHORT_TIMEOUT)
        ok = shim_ok(machine)
    if before and after and after > before:
        log(f"{name}: {as_text(before)} -> {as_text(after)} 로 올렸습니다")
    else:
        log(f"{name}: {as_text(after)} (그대로)")

    return name, after, ok, verdict(after, ok, install_failed)


# codex 모델 줄을 "원래 등급의 최신 판"으로 맞춘다(2026-10-15). CLI 가 올라가면 새 판 모델이
# 목록에 보이므로 갱신 뒤에 돈다. 부품은 ~/.claude/hooks/codex_model.py(설정 동기화로 양쪽 같음).
CODEX_MODEL_APPLY = [
    ("본체", [sys.executable, str(Path.home() / ".claude" / "hooks" / "codex_model.py"), "--apply"]),
    ("Mac", ["ssh", "macmini", "python3 ~/.claude/hooks/codex_model.py --apply"]),
]


def apply_codex_models(dry: bool) -> list[str]:
    problems = []
    for name, cmd in CODEX_MODEL_APPLY:
        r = run(cmd + (["--dry-run"] if dry else []), SHORT_TIMEOUT)
        if r is None or r.returncode != 0:
            problems.append(f"{name}: 코덱스 모델 줄 맞추기가 실패했습니다")
            continue
        for line in (r.stdout or "").splitlines():
            if line.strip():
                log(f"{name} 코덱스 모델: {line.strip()}")
    return problems


def main() -> int:
    dry = "--dry-run" in sys.argv[1:]
    results = [handle(m, dry) for m in MACHINES]
    model_problems = apply_codex_models(dry)

    # claude 와 codex 는 판 번호 체계가 달라 도구별로 가장 높은 판을 따로 본다.
    tools = [m.get("tool", "claude") for m in MACHINES]
    newest_by = {t: max((r[1] for r, tt in zip(results, tools) if tt == t and r[1]), default=None)
                 for t in set(tools)}
    problems = []
    for (name, ver, _ok, reason), tool in zip(results, tools):
        newest = newest_by.get(tool)
        if reason:
            problems.append(f"{name}: {reason}")
        elif newest and ver and ver < newest:
            problems.append(f"{name}: {as_text(ver)} 로 뒤처졌습니다"
                            f"(가장 높은 판 {as_text(newest)})")

    problems += model_problems
    for p in problems:  # 개수만 적으면 기록만 보고는 뭐가 걸렸는지 알 수 없다
        log(f"  문제: {p}")
    log("끝. 가장 높은 판 " + ", ".join(f"{t} {as_text(v)}" for t, v in sorted(newest_by.items()))
        + f", 문제 {len(problems)}건")

    if problems and not dry:
        # 낡은 CLI 는 모델 별칭을 낡게 풀어 엉뚱한 판이 잡힌다. 받은 사람이 바로 할 수 있는
        # 일을 적는다(12.2). 알림은 문제가 있을 때만 — 매일 잘 되는 걸 알릴 필요는 없다.
        notify.send(
            "클로드·코덱스 명령도구 자동 갱신에서 걸린 게 있습니다.\n"
            + "\n".join(problems)
            + "\n낡은 채로 두면 클로드는 'opus' 가 옛 판으로 잡히고, 코덱스는 새 모델을 못 씁니다.\n"
              "본체에서 `python auto_update_cli.py` 를 한 번 실행해 보시고, "
              "그래도 같으면 개발 봇에 '고쳐줘' 라고 답장해 주세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
