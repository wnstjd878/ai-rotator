"""
== 운영 맥락 ==
용도: 저장해둔 지피티 계정 자격(creds/codex/<라벨>.json)을 지금 쓰는 자격 파일로 덮어써서
      codex 로그인을 그 계정으로 바꾼다 (재로그인 없음).
      creds/codex/<라벨>.json → ~/.codex/auth.json (교체 전 .bak 백업)
실행: python codex_switch.py <라벨>
되백업이 먼저인 이유(이걸 빠뜨리면 사고): codex 는 돌면서 자기 자격을 갱신해
      ~/.codex/auth.json 을 스스로 덮어쓴다. 그대로 다른 계정으로 갈아끼우면 방금까지
      쓰던 계정의 최신 자격이 사라지고, 나중에 그 계정으로 돌아올 때 낡은 사본이
      들어가 로그인이 깨진다. 그래서 교체 직전에 현재 자격을 그 라벨 자리에 되백업한다.
      되백업은 계정 번호(account_id)가 같을 때만 한다 — 라벨과 실제 로그인이 어긋나
      있으면 남의 자리를 덮어쓰기 때문이다.
주의: 교체는 다음 codex 실행부터 적용. 실행 중인 codex 창에는 즉시 반영 안 됨.
      creds/ 파일은 자격정보다. 화면 출력·git 커밋 금지.
      codex logout 은 쓰지 말 것 — 저장된 자격을 지워버린다. 계정 추가는 codex login 만.
마지막 점검: 2026-07-28
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

try:  # Windows 콘솔·파이프에서 한글 출력이 깨지지 않게
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

CREDS_DIR = BASE / "creds" / "codex"
LIVE = Path.home() / ".codex" / "auth.json"


def list_labels():
    return sorted(f.stem for f in CREDS_DIR.glob("*.json")) if CREDS_DIR.exists() else []


def _account_id(path: Path):
    """자격 파일의 계정 번호. 값 자체는 자격이 아니지만 화면에 찍지는 않는다."""
    try:
        return ((json.load(open(path, encoding="utf-8")) or {}).get("tokens") or {}).get("account_id")
    except Exception:
        return None


def is_stale(label: str) -> bool:
    """그 라벨 사본이 지금 쓰는 자격보다 낡았는지. 낡았으면 되백업이 필요하다."""
    dest = CREDS_DIR / f"{label}.json"
    if not LIVE.exists():
        return False
    if not dest.exists():
        return True
    try:
        return LIVE.stat().st_mtime > dest.stat().st_mtime + 1
    except OSError:
        return False


def backup_current(label: str) -> str:
    """지금 쓰는 자격을 그 라벨 자리에 되백업한다. 결과 사유를 문자열로 돌려준다."""
    if not label:
        return "현재 계정을 몰라 되백업 건너뜀"
    if not LIVE.exists():
        return "지금 쓰는 자격 파일이 없어 되백업 건너뜀"
    dest = CREDS_DIR / f"{label}.json"
    if dest.exists():
        live_id, dest_id = _account_id(LIVE), _account_id(dest)
        if live_id and dest_id and live_id != dest_id:
            return f"'{label}' 저장본과 지금 로그인이 서로 다른 계정이라 되백업 안 함"
    CREDS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".json.tmp")
    shutil.copy2(LIVE, tmp)
    tmp.replace(dest)  # 원자 교체 — 반쯤 쓰인 자격 파일이 남지 않게
    return f"'{label}' 자격을 최신으로 되백업"


def switch_file(src: Path) -> bool:
    """creds 자격을 지금 쓰는 자격 파일 자리로 복사한다."""
    LIVE.parent.mkdir(parents=True, exist_ok=True)
    if LIVE.exists():
        shutil.copy2(LIVE, LIVE.with_suffix(".json.bak"))  # 되돌리기용 백업
    shutil.copy2(src, LIVE)
    return True


def switch_to(label: str, current: str | None = None) -> tuple[bool, str]:
    """되백업 → 교체를 한 번에. (성공 여부, 사람이 읽는 사유)."""
    src = CREDS_DIR / f"{label}.json"
    if not src.exists():
        return False, f"'{label}' 저장본이 없습니다. 저장된 계정: {', '.join(list_labels()) or '(없음)'}"
    note = backup_current(current) if current and current != label else ""
    switch_file(src)
    return True, note


def main():
    if len(sys.argv) < 2:
        print("사용법: python codex_switch.py <라벨>")
        print("저장된 계정:", ", ".join(list_labels()) or "(없음 — 먼저 codex_save.py)")
        return 1
    label = sys.argv[1].strip()

    current = None
    try:
        import pool_state as st

        st.use("codex")
        current = (st.load_state() or {}).get("current")
    except Exception:
        pass

    ok, note = switch_to(label, current)
    if not ok:
        print(f"[switch] {note}")
        return 1
    if note:
        print(f"[switch] {note}")
    print(f"[switch] codex 로그인을 '{label}' 로 교체했습니다.")
    print("[switch] 열려있는 codex 를 종료하고 다시 실행하면 적용됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
