"""
== 운영 맥락 ==
용도: 세 머신(본체·Mac·winserver) 중 어디서 계정 저장본이 격리(creds/dead/)돼 순환에서 빠졌는지
      매시간 보고, 빠진 동안 하루 한 번 텔레그램으로 다시 알린다.
      격리 알림(claude_pool._notify_cred_dead)은 격리하는 순간 한 번뿐이라 놓치면 그만이었다.
      2026-09-26 winserver B 가 9/10 부터 16일째 빠져 있던 것을 우연히 발견해 만들었다.
실행 시점: 본체 작업 스케줄러 automation-watchdog(1시간)이 stuck_alert.py watch 로 부른다.
           ~/.claude/watchdog-targets.txt 의 probe: 줄. 알림은 이 스크립트가 직접 보내고,
           마지막 줄에 현재 시각을 찍어 감시 쪽엔 "돌았다"만 알린다. 이 스크립트가 죽거나
           머신에 접속을 못 하면 종료코드 1 → 감시가 "판단 못 함"으로 세어 이틀 이어지면 알린다.
입력: 각 머신 creds/ 폴더의 파일 이름만(ssh 로 이름만 조회, 자격 내용은 안 읽는다 = 9.1).
      계정 목록은 이 폴더 accounts.json 의 label.
출력: stuck_alert 상태 파일(키 ai-rotator-dead-<머신>-<라벨>), 텔레그램 알림(Mac 창구 경유).
외부 의존: ssh macmini / ssh winserver, ~/.claude/ops/stuck_alert.py
의도적 미구현: 자동 재로그인(전용 크롬에 사람 로그인이 필요할 수 있어 auto_relogin 몫으로 둠).
시험: python cred_watch.py --dry  (알림 안 보내고 판정만 찍음)
마지막 점검: 2026-09-26
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path.home() / ".claude" / "ops"))
import stuck_alert  # noqa: E402

# relogin.REMOTES 와 같은 값. relogin 을 불러오면 크롬 제어 모듈까지 딸려와 여기선 옮겨 적는다.
REMOTES = {
    "macmini": ("Mac", "ls ~/projects/ai-rotator/creds"),
    "winserver": ("winserver", r"Get-ChildItem -Name C:\Users\pc\projects\ai-rotator\creds"),
}


def labels() -> list:
    rows = json.loads((BASE / "accounts.json").read_text(encoding="utf-8"))
    return [r["label"] for r in rows if r.get("label")]


def saved_labels_local() -> set:
    return {p.stem for p in (BASE / "creds").glob("*.json")}


def saved_labels_remote(host: str, cmd: str) -> set:
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", host, cmd],
                       capture_output=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError(f"{host} 접속 실패(종료코드 {r.returncode})")
    names = r.stdout.decode("utf-8", "replace").split()
    return {n[:-5] for n in names if n.endswith(".json")}


def relogin_cmd(host: str | None, label: str) -> str:
    return f"python relogin.py --remote {host} {label}" if host else f"python relogin.py {label}"


def check(where: str, host: str | None, saved: set, want: list, dry: bool) -> list:
    missing = [l for l in want if l not in saved]
    for l in want:
        key = f"ai-rotator-dead-{where}-{l}"
        if l in saved:
            stuck_alert.clear_stuck(key)
            continue
        stuck_alert.report_stuck(
            key, f"클로드 '{l}' 계정({where})",
            f"{where} 에 저장된 '{l}' 로그인이 격리돼 순환에서 빠져 있습니다",
            hours=0, repeat_hours=24, last_ok="",
            headline=f"클로드 '{l}' 계정이 {where} 에서 아직 순환에서 빠져 있습니다.",
            howto=("할 일: 본체 ai-rotator 폴더에서 아래 한 줄을 실행하고 브라우저 승인 한 번이면 "
                   f"자동으로 복귀합니다.\n  {relogin_cmd(host, l)}"),
            dry=dry)
    return missing


def main() -> int:
    dry = "--dry" in sys.argv
    want = labels()
    failed = []
    try:
        miss = check("본체", None, saved_labels_local(), want, dry)
        print(f"본체\t빠진 계정: {', '.join(miss) or '없음'}")
    except Exception as e:
        failed.append(f"본체({type(e).__name__})")
    for host, (where, cmd) in REMOTES.items():
        try:
            miss = check(where, host, saved_labels_remote(host, cmd), want, dry)
            print(f"{where}\t빠진 계정: {', '.join(miss) or '없음'}")
        except Exception as e:
            failed.append(f"{where}({str(e)[:60] or type(e).__name__})")
    if failed:
        print("확인 못 함: " + ", ".join(failed), file=sys.stderr)
        return 1
    print(f"{datetime.now():%Y-%m-%d %H:%M}")  # 분까지만. 초까지 찍으면 감시 시각보다 늦어 "-1시간 전"으로 보임
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
