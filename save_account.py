"""
== 운영 맥락 ==
용도: 현재 Claude Code 로그인을 계정 라벨로 백업한다.
      Windows: ~/.claude/.credentials.json 파일을 creds/<라벨>.json 으로 복사.
      Mac    : Keychain('Claude Code-credentials' / account 'kwon') 값을 creds/<라벨>.json 으로 저장.
실행: 각 계정으로 claude /login 한 뒤  python save_account.py <라벨>
      Mac 은 반드시 본체 화면 세션에서 실행 (Keychain 읽기에 GUI 잠금해제가 필요, ssh 원격 불가).
출력: creds/<라벨>.json
주의: 이 파일은 자격정보. 화면 출력 금지, git 커밋 금지(.gitignore 처리됨).
      Mac 은 security 로 Keychain 값을 파일로만 내보낸다(값이 명령 인자에 안 실림).
마지막 점검: 2026-07-18
"""

import platform
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CREDS_DIR = BASE / "creds"
CREDS_DIR.mkdir(exist_ok=True)

IS_MAC = platform.system() == "Darwin"
LIVE = Path.home() / ".claude" / ".credentials.json"        # Windows 자격 파일
KC_SERVICE = "Claude Code-credentials"                      # Mac Keychain 서비스명
KC_ACCOUNT = "kwon"                                         # Mac Keychain account (OS 사용자)


def save_windows(dest):
    if not LIVE.exists():
        print(f"[save] 현재 로그인 파일이 없습니다: {LIVE}")
        print("[save] 먼저 claude 에서 /login 하세요.")
        return False
    shutil.copy2(LIVE, dest)
    return True


def save_mac(dest):
    # Keychain 값을 stdout 이 아니라 파일로 직접 리다이렉트 → 값이 명령 인자/화면에 안 남음
    try:
        with open(dest, "w", encoding="utf-8") as f:
            subprocess.run(
                ["security", "find-generic-password", "-s", KC_SERVICE, "-a", KC_ACCOUNT, "-w"],
                stdout=f, check=True,
            )
    except subprocess.CalledProcessError:
        print("[save] Keychain 에서 Claude 자격을 못 읽었습니다.")
        print("[save] Mac 본체 화면에서 실행했는지, claude /login 이 돼 있는지 확인하세요.")
        if dest.exists() and dest.stat().st_size == 0:
            dest.unlink()
        return False
    if dest.stat().st_size == 0:
        print("[save] 읽은 값이 비어 있습니다. 로그인 상태를 확인하세요.")
        dest.unlink()
        return False
    return True


def main():
    if len(sys.argv) < 2:
        print("사용법: python save_account.py <라벨>   예) python save_account.py A")
        return 1
    label = sys.argv[1].strip()
    dest = CREDS_DIR / f"{label}.json"

    ok = save_mac(dest) if IS_MAC else save_windows(dest)
    if not ok:
        return 1

    size = dest.stat().st_size
    where = "Keychain" if IS_MAC else "자격 파일"
    print(f"[save] 현재 로그인({where})을 '{label}' 로 백업했습니다 → {dest.name} ({size} bytes)")

    # 방금 저장한 계정이 지금 실제로 꽂혀 있는 계정이다. 순환기가 시작점을 알도록 적어둔다.
    try:
        sys.path.insert(0, str(BASE))
        import pool_state as st

        with st.with_lock():
            state = st.prune_blocked(st.load_state())
            blocked = dict(state.get("blocked") or {})
            blocked.pop(label, None)
            state["blocked"] = blocked
            st.set_current(state, label, "자격 저장 시점")
            st.save_state(state)
        print(f"[save] 지금 쓰는 계정을 '{label}' 로 기록했습니다.")
    except Exception as e:
        print(f"[save] 현재 계정 기록은 건너뜁니다({type(e).__name__}). "
              f"나중에  python claude_pool.py --use {label}  로 알려주세요.")

    saved = sorted(f.stem for f in CREDS_DIR.glob("*.json"))
    print(f"[save] 저장된 계정: {', '.join(saved)}  (총 {len(saved)}개)")
    if len(saved) < 2:
        print("[save] 계정이 2개 이상이어야 순환이 켜집니다. 다음 계정으로 로그인해 이어서 저장하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
