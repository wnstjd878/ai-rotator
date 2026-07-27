"""
== 운영 맥락 ==
용도: 저장해둔 계정 자격(creds/<라벨>.json)을 지금 쓰는 자격 파일로 덮어써서
      Claude Code 로그인을 그 계정으로 바꾼다 (재로그인 없음). 세 머신 공통 파일 방식.
      creds/<라벨>.json → ~/.claude/.credentials.json (교체 전 .bak 백업).
방식이 파일 하나로 통일된 이유(2026-07-27 확인): 최신 claude 는 Mac 에서도
      ~/.claude/.credentials.json 파일이 있으면 그걸 우선 읽는다. 그래서 Mac 도
      Keychain 을 안 쓰고 파일 스왑으로 처리한다(Keychain 쓰기는 ssh·launchd 에서
      승인 문제로 막혔었다 — 그 걸림돌이 사라짐).
실행: python switch_account.py <라벨>
입력: creds/<라벨>.json (그 머신에서 직접 로그인해 저장한 자격이어야 함.
      다른 머신에서 복사한 자격은 access token 만료 후 refresh 가 거부된다 — 복사 금지).
주의: 교체는 다음 claude 실행부터 적용. 실행 중 세션에는 즉시 반영 안 됨.
      creds/ 파일은 자격정보다. 화면 출력·git 커밋 금지.
마지막 점검: 2026-07-27
"""

import shutil
import sys
from pathlib import Path

try:  # Windows 콘솔·파이프에서 한글 출력이 깨지지 않게
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
CREDS_DIR = BASE / "creds"
LIVE = Path.home() / ".claude" / ".credentials.json"


def list_labels():
    return sorted(f.stem for f in CREDS_DIR.glob("*.json"))


def switch_file(src: Path) -> bool:
    """creds 자격을 지금 쓰는 자격 파일 자리로 복사한다. 세 머신 공통."""
    LIVE.parent.mkdir(parents=True, exist_ok=True)
    if LIVE.exists():
        shutil.copy2(LIVE, LIVE.with_suffix(".json.bak"))  # 되돌리기용 백업
    shutil.copy2(src, LIVE)
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

    if not switch_file(src):
        return 1
    print(f"[switch] Claude Code 로그인을 '{label}' 로 교체했습니다.")
    print("[switch] 열려있는 claude 를 종료하고 다시 실행하면 적용됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
