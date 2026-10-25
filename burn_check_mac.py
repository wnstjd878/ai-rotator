"""
== 운영 맥락 ==
용도: Mac 의 계정 저장본 creds/<라벨>.json 이 실제로 살아 있는지 짧게 태워 확인한다.
      점검 봇(creds_audit)은 폐기된 재발급 열쇠를 못 알아본다(만료일 숫자는 미래라 "정상") —
      진짜 판별은 태워 보는 것뿐. 2026-07-30 Mac A·C·D 전멸을 이 방법으로 발견했다.
실행: **반드시 비화면 자리(ssh)에서**  ssh localhost 'cd ~/projects/ai-rotator && python3 burn_check_mac.py'
      화면 세션에서 돌리면 클로드가 열쇠꾸러미를 먼저 읽어 어느 라벨을 꽂아도 지금 계정으로
      나가므로 판정이 통째로 무효다. 열쇠꾸러미가 읽히는 자리면 스스로 중단한다.
      아침 점검 봇(mkt-agent cost_guard_report)이 주 1회 ssh 로 부른다.
동작: ①되비추기 launchd 를 잠시 내린다(켜두면 5분 주기가 시험 중인 자격 파일을 덮어 판정이
      깨지고, 재발급 직후라면 갱신분을 잃어 계정이 죽는다) ②자격 자리를 백업 ③지금 쓰는
      계정을 뺀 라벨마다 저장본을 자격 자리에 꽂고 claude 를 한 번 불러 재발급이 되는지 본다
      ④재발급되면 주인(이메일) 확인 후 저장본에 되저장(확인 실패 시 creds/rescued/ 보관)
      ⑤자격 자리 복원 + 되비추기 재장전. 값은 화면에 안 찍는다(지문 12자리만).
의도적 미구현: 윈도우식 격리 폴더(CLAUDE_CONFIG_DIR) 방식 — Mac 화면 세션은 열쇠꾸러미가
      우선이라 신뢰할 수 없다. 윈도우는 burn_check_win.py 를 쓴다.
마지막 점검: 2026-07-30
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import cred_mirror as cm  # noqa: E402
import switch_account as sw  # noqa: E402

LIVE = Path.home() / ".claude" / ".credentials.json"
CLAUDE = "/opt/homebrew/bin/claude"
MIRROR_JOB = "com.user.claude-cred-mirror"
MIRROR_PLIST = Path.home() / "Library/LaunchAgents" / (MIRROR_JOB + ".plist")
PROMPT = "1+1=? 숫자만 답해."

DEAD_MARKS = ("oauth session expired", "could not be refreshed",
              "not logged in", "please run /login", "login expired")
LIMIT_MARKS = ("weekly limit", "usage limit", "session limit",
               "opus limit", "sonnet limit")


def fp(path):
    o = cm.read_json(path)
    return cm.fingerprint(o) if o else None


def keychain_readable() -> bool:
    acct = cm.keychain_account()
    if not acct:
        return False
    return cm.read_keychain(acct) is not None


def mirror_pause() -> None:
    subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), MIRROR_JOB)],
                   capture_output=True, text=True, timeout=20)


def mirror_resume() -> bool:
    subprocess.run(["launchctl", "bootstrap", "gui/%d" % os.getuid(), str(MIRROR_PLIST)],
                   capture_output=True, text=True, timeout=20)
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=20)
    return MIRROR_JOB in (r.stdout or "")


def run_claude():
    env = dict(os.environ)
    for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    env["CLAUDE_ROTATOR_DISABLED"] = "1"  # 가로채기 통과(순환 개입 금지)
    try:
        r = subprocess.run([CLAUDE, "-p", PROMPT], capture_output=True, text=True,
                           timeout=180, env=env, cwd=str(Path.home()))
        return r.returncode, ((r.stdout or "") + " " + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return -1, "(180초 안에 안 끝남)"


def verdict_of(rc, out):
    low = out.lower()
    if any(m in low for m in DEAD_MARKS):
        return "죽음 — 재로그인 필요"
    if any(m in low for m in LIMIT_MARKS):
        return "살아 있음(지금은 한도 상태)"
    if rc == 0 and out:
        return "살아 있음(정상 응답)"
    return "판정 불가"


def save_refreshed(src: Path, label: str) -> str:
    """재발급된 자격을 주인 확인 후 저장본에 되저장한다. 확인 실패 시 덮지 않고 보관."""
    email = None
    try:
        email = sw.cred_email(src)
    except Exception:
        pass
    owner = None
    try:
        owner = sw.label_of_email(email)
    except Exception:
        pass
    store = cm.creds_dir() / (label + ".json")
    if owner == label:
        prev = cm.creds_dir() / "prev"
        prev.mkdir(exist_ok=True)
        if store.exists():
            shutil.copy2(store, prev / store.name)
        shutil.copy2(src, store)
        return "재발급분을 저장본에 되저장함"
    resc = cm.creds_dir() / "rescued"
    resc.mkdir(exist_ok=True)
    dst = resc / ("burn-%s-%d.json" % (label, int(time.time())))
    shutil.copy2(src, dst)
    os.chmod(dst, 0o600)
    return "주인 확인 실패(%r) — 저장본 안 덮고 %s 에 보관" % (owner, dst.name)


def burn_env_mode() -> int:
    """환경변수 열쇠 방식: 저장본의 재발급 열쇠를 실제로 한 번 회전시켜 생존을 본다.

    claude 를 띄우지 않고 자격 파일(LIVE)·열쇠꾸러미·되비추기를 건드리지 않는다. 화면 자리에서도
    유효하다. 지금 쓰는 계정도 같이 본다(순환기가 유일한 갱신자라 회전해도 봇이 다치지 않고, 봇은
    다음 호출에 새 출입증을 그대로 꺼내 쓴다). 출력은 cost_guard_report 가 읽는 형식 그대로:
    "<라벨>: 살아 있음(...)" / "<라벨>: 죽음 — 재로그인 필요".
    """
    import cred_refresh as cr
    try:
        import pool_state as st
        current = st.load_state().get("current")
    except Exception:
        current = None
    print("지금 쓰는 계정:", current or "(모름)")
    print("방식: 환경변수 열쇠(저장본 단일 소유), 재발급 열쇠를 회전시켜 판정")
    bad = 0
    for label in ("A", "B", "C", "D"):
        store = cm.creds_dir() / (label + ".json")
        if not store.exists():
            print("%s: 저장본 없음" % label)
            bad += 1
            continue
        before = fp(store)
        if cr.refresh_file(store):  # min_left 없음 = 무조건 새로 받는다(재발급 열쇠가 살았는지가 판정)
            print("%s: 살아 있음(재발급 성공) / 지문 %s → %s" % (label, before, fp(store)))
            continue
        err = cr.last_error or ""
        if "HTTP 400" in err or "HTTP 401" in err or "재발급 열쇠 없음 또는 만료" in err:
            print("%s: 죽음 — 재로그인 필요 (%s)" % (label, err))
        else:
            print("%s: 판정 불가 (%s)" % (label, err))
        bad += 1
    return 1 if bad else 0


def main() -> int:
    if sys.platform != "darwin":
        print("Mac 전용입니다. 윈도우는 burn_check_win.py 를 쓰세요.")
        return 1
    try:
        import cred_refresh as _cr
        if _cr.env_token_mode():
            return burn_env_mode()
    except Exception:
        pass
    if keychain_readable():
        print("화면 세션(열쇠꾸러미가 읽히는 자리)입니다 — 판정이 무효라 중단. ssh 로 부르세요.")
        return 1
    try:
        import pool_state as st
        current = st.load_state().get("current")
    except Exception:
        current = None
    print("지금 쓰는 계정:", current or "(모름)")

    bad = 0
    mirror_pause()
    backup = cm.creds_dir() / "prev" / "_burn_backup.json"
    backup.parent.mkdir(exist_ok=True)
    shutil.copy2(LIVE, backup)
    os.chmod(backup, 0o600)
    try:
        for label in ("A", "B", "C", "D"):
            store = cm.creds_dir() / (label + ".json")
            if not store.exists():
                print("%s: 저장본 없음" % label)
                bad += 1
                continue
            if label == current:
                same = fp(store) == fp(LIVE)
                print("%s: 지금 사용 중(생존 자명) / 저장본=자격 자리 %s"
                      % (label, "일치" if same else "불일치(되비추기가 곧 맞춤)"))
                continue
            shutil.copy2(store, LIVE)
            before = fp(LIVE)
            rc, out = run_claude()
            after = fp(LIVE)
            refreshed = after is not None and after != before
            v = verdict_of(rc, out)
            if "살아 있음" not in v:
                bad += 1
            print("%s: %s / 종료코드 %s / 재발급 %s" % (label, v, rc,
                  "일어남" if refreshed else "없음"))
            if refreshed:
                print("  " + save_refreshed(LIVE, label))
    finally:
        shutil.copy2(backup, LIVE)
        backup.unlink(missing_ok=True)
        if not mirror_resume():
            print("경고: 되비추기 재장전 실패 — launchctl bootstrap 를 손으로 확인하세요!")
            bad += 1
    print("자격 자리 복원 완료. 지금 지문:", fp(LIVE))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
