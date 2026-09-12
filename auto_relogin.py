"""
== 운영 맥락 ==
용도: 3대(본체·Mac·winserver)의 구독 계정 재발급 열쇠가 만료에 다가가면(10일 이내)
      relogin.py 로 자동 갱신한다. 전용 크롬(chrome/<라벨>)에 로그인 세션이 살아 있으면
      사람 손 없이 끝난다(2026-08-18 실측: A 를 3대에서 무인 갱신). 갱신이 실패하거나
      연장 폭이 작으면(크롬 세션이 낡은 신호) 텔레그램으로 사람을 부른다.
      (2026-09-06) 서버가 CLI 승인 때 재로그인(reauth=1)을 요구하게 바뀌어, 사람 없는
      아침 실행은 대개 시간 초과로 끝난다. 그래서 알림에 "본체에서 계정당 클릭 1회 →
      곧바로 Mac·winserver 연달아" 순서를 명령 그대로 담는다(막 로그인한 세션은
      재인증을 안 물어 나머지 머신은 무인 통과).
실행 시점: Windows 본체 작업 스케줄러 `ai-rotator-auto-relogin` 매일 09:20
      (본체는 꺼질 수 있는 PC 다 — 놓친 날은 켜진 뒤 곧바로 실행되게 등록했고,
       열쇠 수명 30일 대비 문턱 10일이라 며칠 꺼져 있어도 여유가 크다.)
입력: 각 머신의 creds_audit.py --json (해지 계정은 audit 이 이미 제외)
출력: auto_relogin.log + 실패 시 텔레그램(notify.py)
전제: 승인용 전용 크롬이 본체에만 있으므로 이 스크립트는 본체에서만 돈다.
      원격 갱신은 relogin.py --remote 가 ssh 로 알아서 한다.
의도적 미구현: missing/dead/wrong 라벨은 안 건드린다 — 크롬 세션 재사용으로 안 풀리는
      사람 몫이고, 아침 점검 봇(08:04)이 이미 알린다. 동시 실행 잠금도 없다(하루 1회 +
      호출마다 240초 제한이라 겹칠 일이 사실상 없다).
마지막 점검: 2026-08-18
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import notify  # noqa: E402

THRESHOLD_DAYS = 10   # 이보다 적게 남으면 갱신
OK_AFTER_DAYS = 20    # 갱신 후 이만큼도 안 남으면 크롬 세션이 낡은 것으로 보고 사람을 부른다
RELOGIN_TIMEOUT = 240
LOG_FILE = BASE / "auto_relogin.log"

# (표시 이름, relogin.py --remote 인자. None = 이 머신, audit 명령)
MACHINES = [
    ("본체", None,
     [sys.executable, str(BASE / "creds_audit.py"), "--json"]),
    ("Mac", "macmini",
     ["ssh", "macmini", "cd ~/projects/ai-rotator && python3 creds_audit.py --json"]),
    ("winserver", "winserver",
     ["ssh", "winserver", r"cd C:\Users\pc\projects\ai-rotator; python creds_audit.py --json"]),
]

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def log(msg: str) -> None:
    line = time.strftime("%m-%d %H:%M:%S ") + msg
    print(line)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=NO_WINDOW)


def audit(cmd: list[str]) -> dict | None:
    """creds_audit --json 을 돌려 결과 줄만 골라 읽는다. 못 읽으면 None."""
    try:
        r = run(cmd, timeout=180)
    except Exception as e:
        log(f"  점검 실행 실패: {type(e).__name__}")
        return None
    for ln in reversed((r.stdout or "").splitlines()):
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                return json.loads(ln)
            except Exception:
                return None
    return None


def relogin(host: str | None, label: str) -> bool:
    cmd = [sys.executable, str(BASE / "relogin.py")]
    if host:
        cmd += ["--remote", host]
    cmd.append(label)
    try:
        r = run(cmd, timeout=RELOGIN_TIMEOUT)
    except subprocess.TimeoutExpired:
        log(f"  relogin {label} 시간 초과({RELOGIN_TIMEOUT}초)")
        return False
    if r.returncode != 0:
        tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-1:] or ["(출력 없음)"]
        log(f"  relogin {label} 실패(rc={r.returncode}): {tail[0][:120]}")
    return r.returncode == 0


def human_steps(problems: list[tuple]) -> str:
    """실패 목록을 사람이 그 자리에서 칠 명령 순서로 바꾼다.

    계정별로 묶는다. 본체에서 그 계정을 한 번 클릭해 갱신하면 세션이 신선해져
    Mac·winserver 는 클릭 없이 통과하므로, 본체 줄을 항상 맨 앞에 둔다(본체가
    실패 목록에 없어도 넣는다). 12.2: 받은 사람이 복붙만 하면 끝나게 적는다.
    """
    by_label: dict[str, list[tuple]] = {}
    for name, host, label, days in problems:
        by_label.setdefault(label, []).append((name, host, days))
    lines = ["구독 계정 자동 갱신이 안 됐습니다. 본체 PowerShell 에서 아래를 순서대로 실행해 주세요.",
             "(첫 줄을 치면 크롬 창이 뜹니다. 구글 계정 선택에서 그 계정을 한 번 누르면 나머지는 자동입니다.)",
             "",
             "cd " + str(BASE)]
    for label, items in by_label.items():
        left = ", ".join(f"{n} {d}일" for n, _, d in items if d is not None)
        lines.append("")
        lines.append(f"[{label} 계정] 남은 기간: {left or '확인 불가'}")
        lines.append(f"python relogin.py {label}")
        for _, host, _ in items:
            if host:
                lines.append(f"python relogin.py --remote {host} {label}")
    lines += ["", "끝나면 python relogin.py --status 로 남은 일수가 27일 이상인지 보세요."]
    return "\n".join(lines)


def main() -> int:
    dry = "--dry-run" in sys.argv[1:]
    problems: list[str] = []
    renewed = 0

    for name, host, audit_cmd in MACHINES:
        data = audit(audit_cmd)
        if data is None:
            # 점검 자체가 안 되는 것은 아침 점검 봇이 따로 알린다. 여기선 기록만.
            log(f"{name}: 점검 결과를 못 읽음 — 이번 회차 건너뜀")
            continue
        for label, info in (data.get("labels") or {}).items():
            days = info.get("days")
            state = info.get("state")
            if state not in ("ok", "soon", "unknown"):
                continue  # missing/dead/wrong 은 사람 몫(아침 봇이 알림)
            if days is None or days > THRESHOLD_DAYS:
                continue
            if dry:
                log(f"{name} {label}: 만료 {days}일 — 갱신 대상 (dry-run이라 실행 안 함)")
                continue
            log(f"{name} {label}: 만료 {days}일 — 자동 갱신 시작")
            ok = relogin(host, label)
            after = audit(audit_cmd) if ok else None
            new_days = ((after or {}).get("labels") or {}).get(label, {}).get("days")
            if ok and new_days is not None and new_days >= OK_AFTER_DAYS:
                log(f"{name} {label}: 갱신 성공, 만료 {new_days}일")
                renewed += 1
            else:
                got = f"{new_days}일" if new_days is not None else "확인 불가"
                log(f"{name} {label}: 갱신 뒤에도 만료 {got} — 사람 확인 필요")
                problems.append((name, host, label, days))

    # 모델별 자리(slot) 전용 자격(creds/slots/<칸>/<라벨>.json)도 같은 문턱으로 갱신한다(본체만, 2026-09-12).
    # 공용 자격과 다른 로그인이라 따로 만료된다. 전용 크롬 세션이 살아 있으면 무인으로 끝난다.
    try:
        import slot_pool
        for bucket in slot_pool.BUCKETS:
            for label in slot_pool.stored_labels(bucket):
                try:
                    o = json.loads(slot_pool.store_path(bucket, label).read_text(encoding="utf-8"))["claudeAiOauth"]
                    days = round((int(o.get("refreshTokenExpiresAt") or 0) / 1000 - time.time()) / 86400, 1)
                except Exception:
                    continue
                if days > THRESHOLD_DAYS:
                    continue
                if dry:
                    log(f"자리 {bucket} {label}: 만료 {days}일 — 갱신 대상 (dry-run이라 실행 안 함)")
                    continue
                log(f"자리 {bucket} {label}: 만료 {days}일 — 자동 갱신 시작")
                r = run([sys.executable, str(BASE / "relogin.py"), "--slot", bucket, label], RELOGIN_TIMEOUT)
                if r.returncode == 0:
                    log(f"자리 {bucket} {label}: 갱신 성공")
                    renewed += 1
                else:
                    log(f"자리 {bucket} {label}: 갱신 실패 — 사람 확인 필요")
                    problems.append(("본체", None, f"--slot {bucket} {label}", days))
    except Exception as e:
        log(f"자리 자격 점검 중 오류({type(e).__name__})")

    if problems and not dry:
        # 크롬 세션이 낡으면 relogin 이 붙잡히거나 연장 폭이 작아진다. 사람이 한 번
        # 로그인해 주면 다시 무인으로 돈다. 12.2: 받은 사람이 그 자리에서 할 일을 적는다.
        notify.send(human_steps(problems))
    log(f"끝. 갱신 {renewed}건, 사람 확인 필요 {len(problems)}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
