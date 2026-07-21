"""
== 운영 맥락 ==
용도: Claude Code 로그인을 저장해둔 계정 라벨로 교체한다 (재로그인 없음).
      Windows: creds/<라벨>.json → ~/.claude/.credentials.json 덮어씀 (교체 전 .bak 백업).
      Mac    : creds/<라벨>.json → Keychain('Claude Code-credentials' / account 'kwon') 덮어씀.
실행: python switch_account.py <라벨>
      Mac 은 반드시 본체 화면 세션에서 실행 (Keychain 쓰기에 GUI 잠금해제 필요, ssh 원격 불가).
입력: creds/<라벨>.json
주의: 교체는 다음 claude 실행부터 적용. 실행 중 세션에는 즉시 반영 안 됨.
      creds/ 파일은 자격정보 — 화면 출력·git 커밋 금지.
      Mac 의 Keychain 쓰기는 값이 로컬 프로세스 인자에 잠깐 실린다(본체 로컬 한정). 원격(ssh)에서 실행 금지.
마지막 점검: 2026-07-18
"""

import platform
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CREDS_DIR = BASE / "creds"

IS_MAC = platform.system() == "Darwin"
LIVE = Path.home() / ".claude" / ".credentials.json"
KC_SERVICE = "Claude Code-credentials"
KC_ACCOUNT = "kwon"


def list_labels():
    return sorted(f.stem for f in CREDS_DIR.glob("*.json"))


def switch_windows(src):
    if LIVE.exists():
        shutil.copy2(LIVE, LIVE.with_suffix(".json.bak"))  # 되돌리기용 백업
    LIVE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, LIVE)
    return True


def switch_mac(src):
    value = src.read_text(encoding="utf-8")
    try:
        subprocess.run(
            ["security", "add-generic-password", "-U",
             "-s", KC_SERVICE, "-a", KC_ACCOUNT, "-w", value],
            check=True,
        )
    except subprocess.CalledProcessError:
        print("[switch] Keychain 에 자격을 못 썼습니다. Mac 본체 화면에서 실행했는지 확인하세요.")
        return False
    return True


def main():
    labels = list_labels()
    if len(sys.argv) < 2:
        print("사용법: python switch_account.py <라벨>")
        print("저장된 계정:", ", ".join(labels) if labels else "(없음 — 먼저 save_account.py)")
        return 1
    label = sys.argv[1].strip()
    src = CREDS_DIR / f"{label}.json"
    if not src.exists():
        print(f"[switch] '{label}' 저장본이 없습니다. 저장된 계정: {', '.join(labels)}")
        return 1

    ok = switch_mac(src) if IS_MAC else switch_windows(src)
    if not ok:
        return 1

    where = "Keychain" if IS_MAC else "자격 파일"
    print(f"[switch] Claude Code 로그인({where})을 '{label}' 로 교체했습니다.")
    print("[switch] 열려있는 claude 를 종료하고 다시 실행하면 적용됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
