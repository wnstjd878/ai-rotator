"""
== 운영 맥락 ==
용도: 2026-10-15 09:25 정기 자동 갱신(auto_update_cli.py, codex 추가 첫날) 결과를 한 번 확인하고
      텔레그램으로 결과를 보낸다. 잘 됐어도 보낸다(사람이 결과를 기다리고 있음).
실행 시점: 작업 스케줄러 `ai-rotator-check-update-once` 2026-10-15 09:43 한 번(놓치면 켜진 뒤 바로).
      끝나면 자기 작업을 지운다.
확인: ① 오늘 갱신 기록의 마지막 "끝." 줄과 문제 목록 ② Mac·본체 codex 계정 순환 장치
      ③ 검수 모델 줄(review_model)이 sol 등급인지
의도적 미구현: 고치기는 안 한다. 문제를 알리면 사람이 개발 봇이나 Claude 에 넘긴다.
마지막 점검: 2026-10-15
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import auto_update_cli as au  # noqa: E402
import notify  # noqa: E402

TASK = "ai-rotator-check-update-once"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def today_lines() -> list[str]:
    stamp = time.strftime("%m-%d")
    try:
        lines = au.LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    # 오늘 정기 실행(09시 이후) 한 회차만. 마지막 "끝." 줄과 그 앞쪽 같은 회차를 본다.
    todays = [l for l in lines if l.startswith(stamp) and l[6:8] >= "09"]
    ends = [i for i, l in enumerate(todays) if " 끝. " in l]
    if not ends:
        return todays
    last = ends[-1]
    start = max([i for i in ends[:-1]] + [-1]) + 1
    return todays[start:last + 1]


def review_models() -> list[str]:
    bad = []
    for p in sorted(Path.home().glob(".codex*/config.toml")):
        m = re.search(r'^review_model = "([^"]*)"', p.read_text(encoding="utf-8"), re.M)
        if not m or not m.group(1).endswith("-sol"):
            bad.append(f"본체 {p.parent.name}: {m.group(1) if m else '없음'}")
    r = au.run(["ssh", "macmini", "grep -h '^review_model' ~/.codex*/config.toml"], au.SHORT_TIMEOUT)
    got = re.findall(r'"([^"]*)"', (r.stdout if r else "") or "")
    if not got:
        bad.append("Mac: 검수 모델 줄을 못 읽음")
    bad += [f"Mac: {g}" for g in got if not g.endswith("-sol")]
    return bad


def main() -> int:
    run_lines = today_lines()
    end = next((l for l in reversed(run_lines) if " 끝. " in l), "")
    problems = [l.split("문제: ", 1)[1] for l in run_lines if "문제: " in l]
    if not end:
        problems.insert(0, "오늘 09:25 정기 갱신 기록이 없습니다(본체가 꺼져 있었거나 작업이 안 돌았음)")

    for m in au.MACHINES:
        if m.get("tool") == "codex" and au.shim_ok(m) is False:
            problems.append(f"{m['name']}: 계정 순환 장치가 벗겨져 있습니다")
    problems += [f"검수 모델이 sol 등급이 아님, {b}" for b in review_models()]

    vers = re.search(r"가장 높은 판 (.+?), 문제", end)
    if problems:
        msg = ("오늘 아침 클로드·코덱스 자동 갱신 확인 결과, 걸린 게 있습니다.\n"
               + "\n".join(f"{i}. {p}" for i, p in enumerate(problems, 1))
               + "\nClaude 창에서 '갱신 문제 고쳐줘' 라고 하시거나 개발 봇에 '고쳐줘' 라고 답장해 주세요.")
    else:
        msg = ("오늘 아침 클로드·코덱스 자동 갱신 확인 결과, 문제 없습니다.\n"
               + (f"최신 판: {vers.group(1)}\n" if vers else "")
               + "본체·Mac 코덱스 계정 순환 장치 정상, 검수 모델 sol 등급 유지.")
    au.log("1회 확인: " + ("문제 " + str(len(problems)) + "건" if problems else "문제 없음"))
    notify.send(msg)

    if "--keep" not in sys.argv:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"], capture_output=True,
                       creationflags=NO_WINDOW)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
