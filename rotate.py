"""
== 운영 맥락 ==
용도: 여러 Claude 로그인을 자동으로 순회하며, 사용량 한도(100%)에 아직 안 걸린
      계정을 찾아 Claude Code 로그인을 그 계정으로 교체한다. Windows·Mac 공용.
      reader.py(판정) + switch_account.py(교체) 를 이어주는 오케스트레이터.
흐름: accounts.json 의 계정을 위에서부터 하나씩 →
      (1) 그 계정의 크롬 프로필을 9222 로 재시작 (Win: .ps1 / Mac: .sh)
      (2) claude.ai/settings/usage 에서 모든 사용률 읽어 최대치 판정 (reader.read_usage)
      (3) 최대 사용률 < 100% 면 "이 계정 사용 가능" → creds/<라벨>.json 으로 자격 교체 후 종료
      (4) 막혔으면 다음 계정. 전부 100% 면 그나마 여유 큰 계정을 알려주고 교체 안 함.
실행: python rotate.py            (여유 계정 찾으면 자격까지 교체)
      python rotate.py --dry-run  (판정만, 교체는 안 함 — 셋업 확인용)
      python rotate.py --restart  (교체 성공하면 새 터미널 창에서 claude 자동 실행)
설정: accounts.json  (accounts.example.json 참고. 라벨=creds 파일명=웹 프로필 잇는 공통 키)
전제: 각 크롬 프로필에 해당 계정으로 claude.ai 로그인돼 있어야 하고,
      각 계정으로 claude /login 후 save_account.py <라벨> 로 creds/<라벨>.json 저장돼 있어야 함.
플랫폼: Windows 는 자격 파일(.credentials.json) 스왑 → 완전 자동.
        Mac 은 Keychain 을 다뤄 본체 화면 세션에서 실행해야 함(ssh 원격 자동 불가).
주의: 계정마다 크롬을 강제 종료 후 재시작한다 — 순회 중에는 크롬으로 다른 작업 하지 말 것.
      creds/ 는 자격정보 — 화면 출력·git 커밋 금지 (.gitignore 처리됨).
마지막 점검: 2026-07-18
"""

import json
import platform
import subprocess
import sys
import time
from pathlib import Path

try:  # Windows 콘솔(cp949)에서 한글 판정 출력이 깨지지 않게
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from playwright.sync_api import sync_playwright

import reader
from reader import FULL

BASE = Path(__file__).resolve().parent
ACCOUNTS = BASE / "accounts.json"
CREDS_DIR = BASE / "creds"
PS1 = BASE / "launch_chrome_debug.ps1"
SH = BASE / "launch_chrome_debug.sh"
IS_WIN = platform.system() == "Windows"
CDP_WAIT_TRIES = 15  # 크롬 재시작 후 CDP 붙기 재시도 횟수 (1초 간격)


def load_accounts():
    if not ACCOUNTS.exists():
        print(f"[rotate] {ACCOUNTS.name} 이 없습니다.")
        print("[rotate] accounts.example.json 을 복사해 accounts.json 을 만들고 계정을 채우세요.")
        return None
    try:
        data = json.loads(ACCOUNTS.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[rotate] accounts.json 을 못 읽었습니다: {e}")
        return None
    if not isinstance(data, list) or not data:
        print("[rotate] accounts.json 은 계정 객체의 리스트여야 합니다. accounts.example.json 참고.")
        return None
    return data


def restart_chrome(profile):
    """지정 프로필의 크롬을 디버그 포트로 재시작 (Win: .ps1 / Mac: .sh)."""
    print(f"[rotate] 크롬 재시작 중 (프로필: {profile})...")
    if IS_WIN:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(PS1), "-Force", "-Profile", profile],
            check=False,
        )
    else:
        subprocess.run(["bash", str(SH), profile, "-Force"], check=False)


def read_one(profile):
    """프로필 크롬을 재시작하고 그 계정의 사용량 dict 를 읽어 반환."""
    restart_chrome(profile)
    with sync_playwright() as p:
        browser = None
        for _ in range(CDP_WAIT_TRIES):
            try:
                browser, page = reader.connect_page(p)
                break
            except Exception:
                time.sleep(1)
        if browser is None:
            return {"logged_in": False, "error": "크롬(9222)에 못 붙음 — 재시작 실패 가능"}
        try:
            return reader.read_usage(page)
        except Exception as e:
            return {"logged_in": False, "error": str(e)}
        finally:
            try:
                page.close()
            except Exception:
                pass


def print_summary(results):
    print("\n============== 계정별 사용량 요약 ==============")
    print(f"  {'라벨':<6} {'계정':<26} {'최대':>6} {'세션':>6} {'모든모델':>8} {'Fable':>7}  판정")
    for r in results:
        label = r.get("label", "?")
        acc = r.get("account") or ("(로그아웃)" if not r.get("logged_in") else "(이메일 못읽음)")

        def pf(v):
            return f"{v}%" if v is not None else "?"

        print(f"  {label:<6} {acc:<26} {pf(r.get('max_pct')):>6} "
              f"{pf(r.get('session')):>6} {pf(r.get('all_models')):>8} {pf(r.get('fable')):>7}  "
              f"{reader.verdict_line(r)}")
    print("===============================================")


def restart_claude():
    """새 터미널 창에서 claude 를 실행한다 (지금 돌고 있는 세션은 안 죽임)."""
    print("[rotate] 새 터미널 창에서 claude 를 실행합니다...")
    try:
        if IS_WIN:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command",
                 "Start-Process powershell -ArgumentList '-NoExit','-Command','claude'"],
                close_fds=True,
            )
        else:
            subprocess.Popen(
                ["osascript", "-e", 'tell application "Terminal" to do script "claude"'],
                close_fds=True,
            )
        print("[rotate] 새 창을 띄웠습니다. 그 창에서 교체된 계정으로 claude 가 뜹니다.")
    except Exception as e:
        print(f"[rotate] 새 창 실행 실패: {e}")
        print("[rotate] 직접 새 터미널에서 claude 를 실행하세요.")


def apply_switch(label, dry, restart):
    """선택된 라벨로 CLI 자격 교체 (switch_account.py 재사용). 성공+restart 면 claude 재시작."""
    cred = CREDS_DIR / f"{label}.json"
    if not cred.exists():
        print(f"[rotate] 경고: creds/{label}.json 이 없어 CLI 자격 교체를 건너뜁니다.")
        print(f"[rotate] 그 계정으로 claude /login 후  python save_account.py {label}  하세요.")
        return 3
    if dry:
        print(f"[rotate] (판정만, 교체 생략) 선택된 계정 = {label}")
        return 0
    ret = subprocess.run([sys.executable, str(BASE / "switch_account.py"), label], check=False)
    if ret.returncode == 0 and restart:
        restart_claude()
    return ret.returncode


def main():
    dry = "--dry-run" in sys.argv
    restart = "--restart" in sys.argv
    accounts = load_accounts()
    if accounts is None:
        return 1

    results = []
    chosen = None
    for acc in accounts:
        label = str(acc.get("label", "")).strip()
        profile = str(acc.get("chrome_profile", "Default")).strip() or "Default"
        if not label:
            print("[rotate] label 이 빈 계정 항목을 건너뜁니다.")
            continue

        print(f"\n[rotate] === '{label}' (크롬 프로필: {profile}) 확인 ===")
        info = read_one(profile)
        info["label"] = label
        results.append(info)

        if not info.get("logged_in"):
            print(f"[rotate] {label}: {info.get('error', '로그아웃/못읽음')} — 다음 계정")
            continue

        print(f"[rotate] {label}: {reader.verdict_line(info)}")
        if info.get("usable"):
            chosen = info
            break

    print_summary(results)

    if chosen is None:
        readable = [r for r in results if r.get("max_pct") is not None]
        if readable:
            best = min(readable, key=lambda r: r["max_pct"])
            print(f"[rotate] 전부 한도 {FULL}% 도달이라 바로 쓸 계정이 없습니다.")
            print(f"[rotate] 그나마 여유 큰 계정: '{best['label']}' (최대 사용률 {best['max_pct']}%)")
        else:
            print("[rotate] 어떤 계정도 사용량을 못 읽었습니다. 로그인/화면 라벨을 확인하세요.")
        return 2

    print(f"\n[rotate] 선택: '{chosen['label']}' (최대 사용률 {chosen['max_pct']}%). 자격 교체 진행.")
    return apply_switch(chosen["label"], dry, restart)


if __name__ == "__main__":
    raise SystemExit(main())
