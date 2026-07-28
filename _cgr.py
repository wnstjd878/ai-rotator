# -*- coding: utf-8 -*-
"""비용 상한 아침 점검 — 어젯밤~오늘 새벽 자동 작업이 상한 안에서 제대로 돌았는지 보고한다.

== 운영 맥락 ==
실행 시점: launchd com.user.cost-guard-report (매일 08:04)
점검 대상 (2026-07-21 비용 통제 조치의 사후 감시):
  1. 인플루언서 프로필 보강 — 하루 500명 상한, Apify 30명씩 분할 호출.
     완주 여부(성공/실패 수)와 배치 실패 유무를 오늘 로그에서 읽는다.
  2. 바이럴레이더 미디어 이해 — Gemini 하루 1,000건 상한.
     카운터 파일의 날짜·건수가 정상인지, 상한에 닿았는지 본다.
  4. 계정 순환 가로채기 (2026-07-28 추가) — Mac·winserver 두 대에서 가로채기가 아직
     자리에 앉아 있는지. claude 를 업데이트하면 벗겨져 계정 교체가 조용히 멈춘다.
  5. 계정 자격 (2026-07-29 추가) — Mac·winserver 의 저장된 자격이 살아 있는지, 라벨과
     실제 계정이 맞는지, 재발급 열쇠 만료가 7일 이내인지. 셋 다 겉으로는 멀쩡해 보이다가
     순환기가 그 계정을 고르는 순간 봇이 멈춘다.
  3. 코드 자동 백업 (2026-07-28 추가) — 오늘 GitHub 에 못 올라간 폴더가 있는지.
     Mac 은 로그를 직접 읽고, 윈도우 본체는 그쪽이 남겨주는 상태 파일을 본다
     (윈도우엔 ssh 서버가 없어 여기서 로그를 못 읽는다).
     백업 실패는 당장 증상이 없어 며칠씩 방치되므로 아침에 한 번 짚는다.
입력: mkt-agent/logs/food-creators-enrich.log, viral-radar/data/enrich_daily_count.json,
      ~/.claude/sync-projects.log, ~/.claude/win-sync-status.txt
출력: 텔레그램 메시지 1건 (TELEGRAM_TOKEN / CHAT_ID 는 mkt-agent/.env 에서 실행 중에만 읽음)
외부 의존: 텔레그램 Bot API (발송 실패해도 종료 코드 0 — 점검 자체는 로그에 남음)
의도적 미구현:
  - 이상 발견 시 자동 수리 (읽기 전용 점검. 사람이 판단해야 하는 영역)
  - 상한 값 자동 조정 (사용자 결정 사항 — CLAUDE.md 4.1 외부 영향 작업)
재시작 정책: 하루 1회. 실패해도 다음날 재시도.
마지막 점검: 2026-07-29
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import requests

HOME = Path.home()
ENV_PATH = HOME / "projects" / "mkt-agent" / ".env"
ENRICH_LOG = HOME / "projects" / "mkt-agent" / "logs" / "food-creators-enrich.log"
COUNT_PATH = HOME / "projects" / "viral-radar" / "data" / "enrich_daily_count.json"
MEDIA_LOG = HOME / "projects" / "viral-radar" / "logs" / "media-enrich.log"
ROTATOR_DIR = HOME / "projects" / "ai-rotator"
SYNC_LOG = HOME / ".claude" / "sync-projects.log"
WIN_SYNC_STATUS = HOME / ".claude" / "win-sync-status.txt"

PROFILE_DAILY_TARGET = 500
GEMINI_DAILY_MAX = 1000
WIN_SYNC_STALE_HOURS = 30  # 윈도우 백업 소식이 이보다 오래되면 알린다


def _env(key: str) -> str:
    """.env 에서 값을 실행 중에만 읽는다 (명령 인자·출력에 남기지 않음 — CLAUDE.md 9.1)."""
    if os.environ.get(key):
        return os.environ[key].strip()
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _tail(path: Path, limit: int = 4000) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]
    except OSError:
        return []


def check_profile_enrich() -> tuple[str, bool]:
    """프로필 보강이 오늘 완주했는지. (보고문, 이상여부)"""
    lines = _tail(ENRICH_LOG)
    if not lines:
        return "프로필 보강: 로그를 읽지 못했습니다.", True

    # 오늘 실행 구간만 본다 — 마지막 "enrich 대상" 줄부터 끝까지
    start = 0
    for i in range(len(lines) - 1, -1, -1):
        if "enrich 대상" in lines[i]:
            start = i
            break
    today = lines[start:]

    target = None
    m = re.search(r"enrich 대상 (\d+)명", today[0] if today else "")
    if m:
        target = int(m.group(1))

    done_line = next((ln for ln in reversed(today) if "완료." in ln), "")
    m2 = re.search(r"성공 (\d+) / 실패 (\d+)", done_line)
    batch_fail = sum(1 for ln in today if "Apify 배치 실패" in ln)

    if not m2:
        return (
            f"프로필 보강: 완료 줄이 없습니다 (대상 {target or '?'}명). "
            "실행이 중간에 끊겼을 수 있습니다.",
            True,
        )

    ok, fail = int(m2.group(1)), int(m2.group(2))
    problem = batch_fail > 0 or (target is not None and ok + fail < target)
    text = f"프로필 보강: 대상 {target or '?'}명 중 성공 {ok} / 실패 {fail}"
    if batch_fail:
        text += f", 묶음 실패 {batch_fail}회 (해당 계정은 다음 실행에 자동 재시도)"
    if target and target < PROFILE_DAILY_TARGET:
        text += f"\n  참고: 상한 {PROFILE_DAILY_TARGET}명보다 적게 잡혔습니다 (보강 대상 자체가 적음)"
    return text, problem


def check_gemini_cap() -> tuple[str, bool]:
    """미디어 이해 일 상한 카운터 상태. (보고문, 이상여부)"""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        data = json.loads(COUNT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "미디어 이해: 카운터 파일을 읽지 못했습니다.", True

    date, count = data.get("date"), int(data.get("count", 0))
    if date != today:
        # 자정 이후 아직 한 건도 안 돌았다는 뜻 — 새벽에도 수집이 도는 구조라 이상 신호
        return (
            f"미디어 이해: 오늘({today}) 처리 기록이 없습니다 "
            f"(마지막 기록 {date}, {count}건). 작업이 멈췄는지 확인이 필요합니다.",
            True,
        )

    text = f"미디어 이해: 오늘 {count}건 / 상한 {GEMINI_DAILY_MAX}건"
    if count >= GEMINI_DAILY_MAX:
        text += "\n  상한에 도달해 남은 글은 내일로 미뤄집니다 (정상 동작)"
    last = next((ln for ln in reversed(_tail(MEDIA_LOG, 50)) if '"queue"' in ln), "")
    m = re.search(r'"pending":\s*(\d+)', last)
    if m:
        text += f"\n  대기 중인 글 {int(m.group(1))}건"
    return text, False


def _still_unpushed(proj_name: str):
    """그 폴더에 아직 못 올린 커밋이 남아 있는지. True/False, 확인 못 하면 None.

    로그만 보면 "실패 뒤 사람이 손으로 올린" 경우를 걸러낼 수 없어서 실제 상태를 본다.
    실패로 기록된 폴더만 확인하므로 보통 0~2개, 값싸다.
    """
    for base in (HOME / "projects", HOME / "tools"):
        repo = base / proj_name
        if not (repo / ".git").exists():
            continue
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "rev-list", "--count", "@{upstream}..HEAD"],
                capture_output=True, text=True, timeout=15,
            )
            if out.returncode != 0:
                return None
            return int((out.stdout or "0").strip() or 0) > 0
        except Exception:
            return None
    return None


def check_backup() -> tuple[str, bool]:
    """코드 자동 백업이 GitHub 까지 올라갔는지. 프로젝트별 '마지막 상태'만 본다.

    실패했다가 다음 주기에 올라간 것은 문제로 치지 않는다. 백업은 실패해도 당장
    증상이 없어 며칠씩 방치되므로(2026-07-28 실제로 그랬다) 아침에 한 번 짚어준다.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    last: dict[str, tuple[str, str]] = {}  # 폴더 -> (상태, 사유)
    pushed = 0
    for ln in _tail(SYNC_LOG, 4000):
        if not ln.startswith(f"[{today}"):
            continue
        m = re.search(r"\[(/[^\]]+)\]", ln)
        if not m:
            continue
        name = m.group(1).rstrip("/").split("/")[-1]
        if "push failed" in ln:
            reason = ln.split("): ", 1)[1] if "): " in ln else "사유 기록 없음"
            last[name] = ("fail", reason)
        elif "BLOCKED" in ln:
            last[name] = ("blocked", ln.split(": ", 2)[-1][:80])
        elif "pushed" in ln:
            last[name] = ("ok", "")
            pushed += 1

    # 로그의 마지막 줄이 실패여도 그 뒤에 사람이 손으로 올려 해결된 경우가 있다.
    # 그대로 보고하면 오탐이 쌓이고, 오탐이 잦으면 알림을 안 보게 된다. 실제 상태를 본다.
    bad_now = {}
    for name, (state, reason) in last.items():
        if state == "ok":
            continue
        if state == "fail" and _still_unpushed(name) is False:
            continue  # 이후 해결됨
        bad_now[name] = (state, reason)
    lines = [f"코드 자동 백업 (Mac): 오늘 {pushed}건 올림"]
    for name, (state, reason) in sorted(bad_now.items()):
        mark = "차단" if state == "blocked" else "실패"
        lines.append(f"  {name} {mark}: {reason[:90]}")

    # 윈도우 본체 쪽은 로그를 여기서 못 읽는다(윈도우엔 ssh 서버가 없다).
    # 윈도우가 올려주는 상태 파일을 대신 본다. 파일이 낡았으면 그 자체가 신호다.
    win_bad = False
    try:
        raw = WIN_SYNC_STATUS.read_text(encoding="utf-8-sig", errors="ignore").strip().splitlines()
        when = datetime.strptime(raw[0].strip(), "%Y-%m-%d %H:%M:%S")
        age_h = (datetime.now() - when).total_seconds() / 3600
        body = "\n".join(raw[1:]).strip()
        if age_h > WIN_SYNC_STALE_HOURS:
            lines.append(f"윈도우 백업: {int(age_h)}시간째 소식 없음 (본체가 꺼져 있거나 안 돎)")
            win_bad = True
        elif body.startswith("FAIL"):
            lines.append("윈도우 백업: 실패 있음")
            for f in body.splitlines()[1:]:
                lines.append(f"  {f[:90]}")
            win_bad = True
        else:
            lines.append(f"윈도우 백업: 정상 ({when:%m/%d %H:%M})")
    except Exception:
        lines.append("윈도우 백업: 상태를 못 읽음")
        win_bad = True

    return "\n".join(lines), bool(bad_now) or win_bad


def check_rotator_shim() -> tuple[str, bool]:
    """계정 순환 가로채기가 아직 자리에 앉아 있는지 (2026-07-28 추가).

    claude 를 업데이트하면 원본 실행 파일이 되살아나 가로채기를 건너뛴다. 그러면 봇들이
    한도에 걸려도 계정을 못 바꾸고 조용히 전패한다(07/28 winserver 가 하루 종일 그랬다).
    당장 증상이 안 보이는 고장이라 아침에 한 번 짚는다.
    """
    lines, bad = [], False

    # Mac: 연결된 자리가 가로채기 파일을 가리키는지. 이 머신은 문자표가 utf-8 이라 글자로 본다.
    try:
        r = subprocess.run(["bash", str(ROTATOR_DIR / "install_shim.sh"), "--status"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        out = (r.stdout or "") + (r.stderr or "")
        if "claude_shim" in out:
            lines.append("순환 가로채기 (Mac): 정상")
        else:
            lines.append("순환 가로채기 (Mac): 벗겨짐 — install_shim.sh 다시 실행 필요")
            bad = True
    except Exception as exc:
        lines.append(f"순환 가로채기 (Mac): 확인 실패 ({str(exc)[:80]})")
        bad = True

    # winserver: 저쪽 문자표가 달라 한글이 깨져 오므로 True/False 줄만 읽는다.
    try:
        r = subprocess.run(
            ["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "winserver",
             "powershell -NoProfile -ExecutionPolicy Bypass -File "
             r"C:\Users\pc\projects\ai-rotator\install_shim.ps1 -Status"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        flags = {}
        for ln in (r.stdout or "").splitlines():
            if ":" in ln and ("True" in ln or "False" in ln):
                k, v = ln.split(":", 1)
                flags[k.strip()] = v.strip() == "True"
        if not flags:
            lines.append("순환 가로채기 (winserver): 상태를 못 읽음")
            bad = True
        elif flags.get("claude.cmd") and flags.get("claude-real.exe") and not flags.get("claude.exe"):
            lines.append("순환 가로채기 (winserver): 정상")
        else:
            lines.append("순환 가로채기 (winserver): 벗겨짐 — install_shim.ps1 다시 실행 필요")
            bad = True
    except Exception as exc:
        lines.append(f"순환 가로채기 (winserver): 확인 실패 ({str(exc)[:80]})")
        bad = True

    return "\n".join(lines), bad


def check_rotator_creds() -> tuple[str, bool]:
    """3대의 계정 자격이 살아 있고 라벨과 맞는지, 만료가 임박했는지 (2026-07-29 추가).

    자격이 죽거나 라벨이 섞여도 겉으로는 멀쩡해 보인다. 순환기가 그 계정을 고르는
    순간에야 "Login expired" 로 드러나고 그때는 이미 봇이 멈춘 뒤다. 실제 사고:
      - 07/28 갈아탈 때 갱신분을 안 챙겨 3대에서 자격 5개가 죽었다.
      - 같은 날 winserver 의 B 자리에 A 계정 자격이 들어가 있었다(라벨 섞임).
    재발급 열쇠는 30일 수명이라 만료 임박(7일 이내)도 같이 본다. 고침은 relogin.py.
    윈도우 본체는 ssh 서버가 없어 여기서 못 본다 — Mac·winserver 두 대만 본다.
    """
    lines, bad = [], False

    def run_audit(name, cmd):
        nonlocal bad
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=180)
            data = None
            for ln in reversed((r.stdout or "").splitlines()):  # 결과 줄만 골라 읽는다
                ln = ln.strip()
                if ln.startswith("{"):
                    data = json.loads(ln)
                    break
        except Exception as exc:
            lines.append(f"계정 자격 ({name}): 확인 실패 ({str(exc)[:80]})")
            bad = True
            return
        if data is None:
            lines.append(f"계정 자격 ({name}): 상태를 못 읽음")
            bad = True
            return

        labels = data.get("labels") or {}
        if not data.get("bad"):
            days = [v["days"] for v in labels.values() if v.get("days")]
            soonest = f", 가장 이른 만료 {min(days):.0f}일 뒤" if days else ""
            lines.append(f"계정 자격 ({name}): 정상 {len(labels)}개{soonest}")
            return

        bad = True
        detail = []
        if data.get("dead"):
            detail.append(f"죽음 {', '.join(data['dead'])}")
        if data.get("wrong"):
            detail.append("라벨 섞임 " + ", ".join(
                f"{k}자리에 {(labels.get(k) or {}).get('actual') or '?'}"
                for k in data["wrong"]))
        if data.get("soon"):
            detail.append(f"만료 임박 {', '.join(data['soon'])}")
        lines.append(f"계정 자격 ({name}): " + " / ".join(detail)
                     + " — relogin.py 로 다시 로그인 필요")

    run_audit("Mac", ["python3", str(ROTATOR_DIR / "creds_audit.py"), "--json"])
    run_audit("winserver", [
        "/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "winserver",
        r"cd C:\Users\pc\projects\ai-rotator; python creds_audit.py --json"])
    return "\n".join(lines), bad


def main() -> int:
    parts, problems = [], []
    for check in (check_profile_enrich, check_gemini_cap, check_backup,
                  check_rotator_shim, check_rotator_creds):
        try:
            text, bad = check()
        except Exception as exc:  # 점검 하나가 죽어도 나머지는 보고
            text, bad = f"{check.__name__} 점검 중 오류: {str(exc)[:150]}", True
        parts.append(text)
        problems.append(bad)

    head = "[비용 상한 아침 점검] " + ("확인 필요" if any(problems) else "정상")
    message = head + "\n\n" + "\n\n".join(parts)
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] {message}")

    token, chat_id = _env("TELEGRAM_TOKEN"), _env("CHAT_ID")
    if not (token and chat_id):
        print("텔레그램 설정 없음 — 발송 건너뜀")
        return 0
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        # 상태 200이어도 본문에 ok=false 가 올 수 있다 (CLAUDE.md 0.2)
        if r.status_code != 200 or not (r.json() or {}).get("ok"):
            print(f"텔레그램 발송 실패: {r.status_code} {r.text[:200]}")
    except Exception as exc:
        print(f"텔레그램 발송 실패: {str(exc)[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
