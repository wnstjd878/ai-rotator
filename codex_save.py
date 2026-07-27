"""
== 운영 맥락 ==
용도: 현재 codex 로그인(~/.codex/auth.json)을 계정 라벨로 백업한다.
실행: 각 계정으로 codex login 한 뒤  python codex_save.py <라벨>
출력: creds/codex/<라벨>.json
확인: 저장한 자격으로 서버에 사용량을 한 번 물어 계정 주소와 요금제를 보여준다.
      로그인만 하고 저장 명령을 안 돌리면 순환에 안 잡히므로, 저장이 실제로 됐는지
      눈으로 확인할 수 있게 한 것이다.
주의: 이 파일은 자격정보. 화면 출력 금지, git 커밋 금지(.gitignore 처리됨).
      codex logout 은 쓰지 말 것 — 저장된 자격을 지운다. 계정 바꿔 로그인할 땐 codex login 만.
마지막 점검: 2026-07-28
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

CREDS_DIR = BASE / "creds" / "codex"
LIVE = Path.home() / ".codex" / "auth.json"


def main():
    if len(sys.argv) < 2:
        print("사용법: python codex_save.py <라벨>   예) python codex_save.py A")
        return 1
    label = sys.argv[1].strip()

    if not LIVE.exists():
        print("[save] 현재 codex 로그인 파일이 없습니다. 먼저 codex login 을 하세요.")
        return 1

    import pool_state as st

    st.use("codex")

    # codex_accounts.json 에 없는 라벨로 저장하면 순환기가 그 계정을 못 고른다.
    # 그대로 두면 첫 한도에서 곧바로 "전 계정 소진"이 되므로 여기서 먼저 막는다.
    known = st.account_labels()
    if known and label not in known:
        print(f"[save] '{label}' 은 codex_accounts.json 에 없는 라벨입니다. "
              f"쓸 수 있는 라벨: {', '.join(known)}")
        return 1

    CREDS_DIR.mkdir(parents=True, exist_ok=True)
    dest = CREDS_DIR / f"{label}.json"
    shutil.copy2(LIVE, dest)
    print(f"[save] 현재 로그인을 '{label}' 로 백업했습니다 → {dest.name} ({dest.stat().st_size} bytes)")

    # 저장한 자격이 실제로 통하는지, 어느 계정인지 확인한다.
    try:
        import codex_usage

        u = codex_usage.get_usage(dest)
        if u:
            print(f"[save] 확인: {u.get('email')} / {u.get('plan')} 요금제 / "
                  f"{codex_usage.summarize(u)}")
        else:
            print(f"[save] 확인 실패({codex_usage.last_error}). 저장은 됐지만 "
                  f"이 자격으로 사용량을 못 읽습니다.")
    except Exception as e:
        print(f"[save] 확인 건너뜀({type(e).__name__})")

    # 방금 저장한 계정이 지금 실제로 꽂혀 있는 계정이다. 순환기가 시작점을 알도록 적어둔다.
    try:
        with st.with_lock():
            state = st.prune_blocked(st.load_state())
            st.clear_blocked_label(state, label)
            st.set_current(state, label, "자격 저장 시점")
            st.save_state(state)
        print(f"[save] 지금 쓰는 계정을 '{label}' 로 기록했습니다.")
    except Exception as e:
        print(f"[save] 현재 계정 기록은 건너뜁니다({type(e).__name__}). "
              f"나중에  python codex_pool.py --use {label}  로 알려주세요.")

    saved = sorted(f.stem for f in CREDS_DIR.glob("*.json"))
    print(f"[save] 저장된 계정: {', '.join(saved)}  (총 {len(saved)}개)")
    if len(saved) < 2:
        print("[save] 계정이 2개 이상이어야 순환이 켜집니다. "
              "다음 계정으로 codex login 한 뒤 이어서 저장하세요. (codex logout 금지)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
