"""
== 운영 맥락 ==
용도: 저장해둔 계정 자격(creds/<라벨>.json)을 지금 쓰는 자격 파일로 덮어써서
      Claude Code 로그인을 그 계정으로 바꾼다 (재로그인 없음). 세 머신 공통 파일 방식.
      creds/<라벨>.json → ~/.claude/.credentials.json (교체 전 .bak 백업).
방식이 파일 하나로 통일된 이유(2026-07-27 확인): 최신 claude 는 Mac 에서도
      ~/.claude/.credentials.json 파일이 있으면 그걸 우선 읽는다. 그래서 Mac 도
      Keychain 을 안 쓰고 파일 스왑으로 처리한다(Keychain 쓰기는 ssh·launchd 에서
      승인 문제로 막혔었다 — 그 걸림돌이 사라짐).
실행: python switch_account.py <라벨> [--from <떠나는라벨>]
입력: creds/<라벨>.json (그 머신에서 직접 로그인해 저장한 자격이어야 함.
      다른 머신에서 복사한 자격은 access token 만료 후 refresh 가 거부된다 — 복사 금지).
되저장(2026-07-28 추가): 갈아타기 직전에 지금 쓰던 계정의 갱신된 자격을 그 라벨 파일로
      되돌려 저장한다. --from 을 안 주면 state.json 의 current 를 떠나는 계정으로 본다.
      이게 없으면 갱신분이 유실되어 그 계정으로 돌아왔을 때 로그인이 풀린다(save_back 참조).
주의: 교체는 다음 claude 실행부터 적용. 실행 중 세션에는 즉시 반영 안 됨.
      creds/ 파일은 자격정보다. 화면 출력·git 커밋 금지.
마지막 점검: 2026-07-28
"""

import hashlib
import json
import re
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


def _fingerprint(path: Path):
    """자격 파일의 재발급 열쇠 지문(sha256). 값 자체는 어디에도 남기지 않는다."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    token = ((data.get("claudeAiOauth") or {}).get("refreshToken")) or ""
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def current_label():
    """state.json 이 기록한 '지금 쓰는 계정'. 못 읽으면 None."""
    try:
        sys.path.insert(0, str(BASE))
        import pool_state as st

        return st.load_state().get("current")
    except Exception:
        return None


def live_label():
    """지금 실제로 꽂혀 있는 계정의 라벨. 못 알아내면 None.

    claude auth status 가 알려주는 이메일을 accounts.json 의 라벨별 email 과 대조한다.
    사람이 순환기 모르게 /login 하면 state.json 의 current 가 낡는데(사용량이 없어
    계정을 바꾸는 건 흔한 일이다), 그 낡은 값을 믿고 되저장하면 엉뚱한 라벨에
    다른 계정 자격이 들어간다. 그래서 기록보다 이 실측을 우선한다.
    """
    import subprocess

    try:
        accounts = json.loads((BASE / "accounts.json").read_text(encoding="utf-8"))
        by_mail = {(x.get("email") or "").lower(): x.get("label")
                   for x in accounts if x.get("email") and x.get("label")}
    except Exception:
        return None
    if not by_mail:
        return None
    try:
        r = subprocess.run(
            ["claude", "auth", "status", "--text"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(BASE), shell=(sys.platform == "win32"), timeout=60,
        )
    except Exception:
        return None
    m = re.search(r"Email:\s*(\S+)", r.stdout or "")
    return by_mail.get(m.group(1).lower()) if m else None


def save_back(leaving) -> str:
    """갈아타기 직전, 지금 쓰던 계정의 갱신된 자격을 그 라벨 파일에 되저장한다.

    claude 는 실행 중 자격이 낡으면 스스로 새로 발급받아 ~/.claude/.credentials.json 을
    덮어쓴다. 이때 재발급 열쇠(refresh token)도 새것으로 바뀌고 옛 열쇠는 서버에서
    폐기된다. 되저장을 안 하면 creds/<라벨>.json 에 폐기된 옛 열쇠가 남고, 그 계정으로
    되돌아온 순간 "Login expired" 가 뜬다.
    2026-07-28 실제 사고: 이 유실로 저장해둔 계정 네 개가 전부 못 쓰게 됐다.

    라벨을 잘못 알고 덮어쓰면 멀쩡한 계정 하나를 잃는다. 그래서 두 겹으로 막는다.
    (1) 기록(state.current)보다 실측(live_label — claude auth status 의 이메일)을 우선한다.
        사용량이 없어 사람이 /login 으로 계정을 바꾸는 일은 흔하고, 그때 기록이 낡는다.
    (2) 그래도 지금 자격이 다른 라벨의 저장본과 같으면 덮어쓰지 않고 넘어간다.
    돌려주는 값은 기록용 한 줄이다.
    """
    if not LIVE.exists():
        return "지금 쓰는 자격 파일이 없어 되저장 안 함"
    if not leaving:
        leaving = live_label()  # 기록이 비었어도 실측으로 알아낼 수 있다
        if not leaving:
            return "떠나는 계정을 몰라 되저장 안 함"
    dest = CREDS_DIR / f"{leaving}.json"
    if not dest.exists():
        return f"'{leaving}' 저장본이 없어 되저장 안 함"

    live_fp = _fingerprint(LIVE)
    if live_fp is None:
        return "지금 자격을 못 읽어 되저장 안 함"
    if live_fp == _fingerprint(dest):
        return f"'{leaving}' 자격에 변화 없음(되저장 불필요)"

    # 기록보다 실측이 우선. 사람이 /login 으로 계정을 바꿔 기록이 낡았을 수 있다.
    actual = live_label()
    if actual and actual != leaving:
        note = f"기록은 '{leaving}' 이지만 실제로는 '{actual}' 이라 그쪽에 저장합니다"
        leaving = actual
        dest = CREDS_DIR / f"{leaving}.json"
        if not dest.exists() or live_fp == _fingerprint(dest):
            return f"{note} — 이미 최신이라 되저장 불필요"
    else:
        note = None

    # 기록된 라벨과 실제 꽂힌 자격이 어긋나면 덮어쓰지 않는다(다른 계정 자격 오염 방지)
    for other in CREDS_DIR.glob("*.json"):
        if other.stem == leaving:
            continue
        if _fingerprint(other) == live_fp:
            return (f"지금 자격은 '{leaving}' 이 아니라 '{other.stem}' 의 저장본과 같습니다. "
                    f"덮어쓰지 않고 넘어갑니다")

    # 한 세대 되돌리기용. creds/*.json 목록에 안 섞이도록 하위 폴더에 둔다
    prev_dir = CREDS_DIR / "prev"
    prev_dir.mkdir(exist_ok=True)
    shutil.copy2(dest, prev_dir / f"{leaving}.json")
    shutil.copy2(LIVE, dest)
    done = f"'{leaving}' 의 갱신된 자격을 되저장했습니다"
    return f"{note} — {done}" if note else done


def switch_file(src: Path, leaving=None) -> bool:
    """creds 자격을 지금 쓰는 자격 파일 자리로 복사한다. 세 머신 공통.

    복사 전에 지금 쓰던 계정의 갱신분을 그 라벨 파일로 되저장한다(save_back).
    """
    LIVE.parent.mkdir(parents=True, exist_ok=True)
    print(f"[save-back] {save_back(leaving)}")
    if LIVE.exists():
        shutil.copy2(LIVE, LIVE.with_suffix(".json.bak"))  # 되돌리기용 백업
    shutil.copy2(src, LIVE)
    return True


def main():
    labels = list_labels()
    args = [a for a in sys.argv[1:] if a.strip()]
    leaving = None
    if "--from" in args:
        i = args.index("--from")
        if i + 1 < len(args):
            leaving = args[i + 1].strip()
        args = args[:i] + args[i + 2:]
    if not args:
        print("사용법: python switch_account.py <라벨> [--from <떠나는라벨>]")
        print("저장된 계정:", ", ".join(labels) if labels else "(없음 — 먼저 save_account.py)")
        return 1
    label = args[0].strip()
    src = CREDS_DIR / f"{label}.json"
    if not src.exists():
        print(f"[switch] '{label}' 저장본이 없습니다. 저장된 계정: {', '.join(labels)}")
        return 1

    # --from 을 안 주면 기록된 현재 계정을 떠나는 계정으로 본다(사람이 직접 부른 경우)
    if leaving is None:
        leaving = current_label()
    if leaving == label:
        leaving = None  # 같은 계정으로 다시 꽂는 건 되저장 대상이 아니다

    if not switch_file(src, leaving=leaving):
        return 1
    print(f"[switch] Claude Code 로그인을 '{label}' 로 교체했습니다.")
    print("[switch] 열려있는 claude 를 종료하고 다시 실행하면 적용됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
