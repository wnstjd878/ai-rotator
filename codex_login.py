"""
== 운영 맥락 ==
용도: 계정 라벨을 주면 그 계정 폴더로 codex 로그인을 띄운다.
      폴더 준비(설정 복사) + CODEX_HOME 지정을 대신 해 준다.
실행: python codex_login.py B     → 브라우저가 열리면 그 계정을 고른다
왜 이 도구가 필요한가: 환경변수를 손으로 걸다 빠뜨리면 기본 폴더에 로그인되고,
      그 순간 원래 그 폴더에 있던 계정 자격이 서버에서 폐기된다(2026-07-28 실제로 두 번).
      셸마다 환경변수 문법이 달라서(PowerShell 은 $env:X="..", bash 는 X=".." 명령)
      더 잘 틀린다. 그래서 라벨만 받아 여기서 확실히 건다.
주의: codex logout 은 쓰지 말 것 — 그 폴더의 자격을 지운다.
마지막 점검: 2026-07-28
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import codex_home  # noqa: E402


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    known = codex_home.labels()
    if len(sys.argv) < 2:
        print("사용법: python codex_login.py <라벨>")
        for l in known:
            mark = "로그인 있음" if codex_home.is_logged_in(l) else "로그인 없음"
            print(f"  {l}: {codex_home.home_of(l)}  — {mark}")
        return 1

    label = sys.argv[1].strip()
    if known and label not in known:
        print(f"'{label}' 은 codex_accounts.json 에 없는 라벨입니다. 쓸 수 있는 라벨: {', '.join(known)}")
        return 1

    home = codex_home.prepare(label)
    already = codex_home.is_logged_in(label)
    print(f"[login] 계정 '{label}' 폴더: {home}")
    if already:
        print(f"[login] 이미 로그인이 들어 있습니다. 다시 로그인하면 그 폴더의 기존 계정 자격은 폐기됩니다.")
    print("[login] 브라우저가 열리면 이 계정으로 로그인하세요.")

    env = codex_home.env_for(label)
    try:
        proc = subprocess.run(["codex", "login"], env=env)
    except FileNotFoundError:
        print("[login] codex 를 못 찾았습니다. PATH 를 확인하세요.")
        return 1
    if proc.returncode != 0:
        print(f"[login] 로그인이 끝나지 않았습니다 (종료코드 {proc.returncode}).")
        return proc.returncode

    # 실제로 그 폴더에 로그인이 들어갔는지, 어느 계정인지 확인한다.
    try:
        import codex_usage

        u = codex_usage.get_usage(codex_home.auth_path(label))
        if u:
            print(f"[login] 확인: {u.get('email')} / {u.get('plan')} 요금제 / "
                  f"{codex_usage.summarize(u)}")
        else:
            print(f"[login] 확인 실패({codex_usage.last_error}). 로그인은 됐지만 사용량을 못 읽습니다.")
    except Exception as e:
        print(f"[login] 확인 건너뜀({type(e).__name__})")

    live = codex_home.logged_in_labels()
    print(f"[login] 로그인된 계정: {', '.join(live)}  (총 {len(live)}개)")
    if len(live) < 2:
        print("[login] 계정이 2개 이상이어야 순환이 켜집니다.")
    else:
        # 다른 계정이 이번 로그인으로 죽지 않았는지 바로 확인한다(폴더가 갈려 있으면 안 죽는다)
        try:
            import codex_usage

            for l in live:
                if l == label:
                    continue
                u = codex_usage.get_usage(codex_home.auth_path(l))
                state = f"살아 있음 ({u.get('email')})" if u else f"못 읽음 ({codex_usage.last_error})"
                print(f"[login] 계정 {l}: {state}")
        except Exception:
            pass

    # 방금 로그인한 계정을 지금 쓰는 계정으로 기록해 둔다.
    try:
        import pool_state as st

        st.use("codex")
        with st.with_lock():
            state = st.prune_blocked(st.load_state())
            st.clear_blocked_label(state, label)
            st.set_current(state, label, "로그인 시점")
            st.save_state(state)
        print(f"[login] 지금 쓰는 계정을 '{label}' 로 기록했습니다.")
    except Exception as e:
        print(f"[login] 현재 계정 기록은 건너뜁니다({type(e).__name__}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
