"""
== 운영 맥락 ==
용도: 윈도우 머신(본체·winserver)의 계정 저장본 creds/<라벨>.json 이 실제로 살아 있는지
      격리 폴더(CLAUDE_CONFIG_DIR)에 사본을 놓고 짧게 태워 확인한다.
      자격 자리(~/.claude/.credentials.json)는 읽기만 하고 절대 바꾸지 않는다
      (이 머신에서 클로드가 돌고 있어도 안전).
실행: 그 머신의 ai-rotator 폴더에서  python burn_check_win.py
출력: 라벨별 "살아 있음 / 한도 상태 / 죽음(재로그인 필요) / 판정 불가" + 지문. 값은 안 찍는다.
동작: ①격리 검사(가짜 자격이 실패해야 진행 — 안 되면 전부 중단) ②지금 쓰는 계정은
      태우지 않고(사용 중 = 생존 자명) 저장본 지문만 자격 자리와 대조 ③나머지 라벨은
      사본을 태워 재발급이 되는지 본다. 재발급이 일어나면 옛 열쇠는 폐기되므로 갱신분을
      주인(이메일) 확인 후 저장본에 즉시 되저장한다. 확인이 안 되면 덮지 않고
      creds/rescued/ 에 보관한다.
왜: 점검 봇(creds_audit)은 폐기된 재발급 열쇠를 못 알아본다(만료일 숫자는 미래라 "정상") —
    진짜 판별은 태워 보는 것뿐. 2026-07-30 Mac A·C·D 전멸을 이 방법으로 발견했다.
의도적 미구현: Mac 은 이 방식이 안 통한다(화면 세션 클로드는 열쇠꾸러미를 먼저 읽어
    격리 폴더가 무시될 수 있음). Mac 은 자격 자리 바꿔치기 + 되비추기 잠시 내리기로 한다.
마지막 점검: 2026-07-30
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import relogin  # noqa: E402  (claude_cmd 재사용)
import switch_account as sw  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LIVE = Path.home() / ".claude" / ".credentials.json"
CREDS = BASE / "creds"
PROMPT = "1+1=? 숫자만 답해."

DEAD_MARKS = ("oauth session expired", "could not be refreshed",
              "not logged in", "please run /login", "login expired")
LIMIT_MARKS = ("weekly limit", "usage limit", "session limit",
               "opus limit", "sonnet limit")


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def fp(path):
    o = read_json(path)
    try:
        token = (o.get("claudeAiOauth") or {}).get("refreshToken") or ""
    except Exception:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else None


def burn_env(sandbox: Path):
    env = dict(os.environ)
    for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    env["CLAUDE_CONFIG_DIR"] = str(sandbox)
    env["CLAUDE_ROTATOR_DISABLED"] = "1"  # 가로채기(있는 머신) 통과
    return env


def run_claude(sandbox: Path):
    try:
        r = subprocess.run(
            relogin.claude_cmd("-p", PROMPT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, env=burn_env(sandbox), cwd=str(Path.home()),
        )
        return r.returncode, ((r.stdout or "") + " " + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return -1, "(180초 안에 안 끝남)"


def isolation_ok() -> bool:
    """가짜 자격을 격리 폴더에 넣고 실패하는지 본다. 성공하면 격리가 안 되는 것 — 전부 중단.

    격리가 안 되는데 계속 가면 모든 라벨이 지금 계정으로 "살아 있음" 오판되고,
    최악에는 태우기가 지금 계정의 자격을 회전시켜 세션을 죽인다.
    """
    box = Path(tempfile.mkdtemp(prefix="claude-burn-sanity-"))
    try:
        (box / ".credentials.json").write_text(
            '{"claudeAiOauth":{"accessToken":"x","refreshToken":"x","expiresAt":1}}',
            encoding="utf-8")
        rc, out = run_claude(box)
        low = out.lower()
        return rc != 0 and any(m in low for m in DEAD_MARKS)
    finally:
        shutil.rmtree(box, ignore_errors=True)


def verdict_of(rc, out):
    low = out.lower()
    if any(m in low for m in DEAD_MARKS):
        return "죽음 — 재로그인 필요"
    if any(m in low for m in LIMIT_MARKS):
        return "살아 있음(지금은 한도 상태)"
    if rc == 0 and out:
        return "살아 있음(정상 응답)"
    return "판정 불가"


def save_refreshed(box_file: Path, label: str) -> str:
    """태우다 재발급된 자격을 주인 확인 후 저장본에 되저장한다."""
    email = None
    try:
        email = sw.cred_email(box_file)
    except Exception:
        pass
    owner = None
    try:
        owner = sw.label_of_email(email)
    except Exception:
        pass
    store = CREDS / (label + ".json")
    if owner == label:
        prev = CREDS / "prev"
        prev.mkdir(exist_ok=True)
        if store.exists():
            shutil.copy2(store, prev / store.name)
        shutil.copy2(box_file, store)
        return "재발급분을 저장본에 되저장함"
    resc = CREDS / "rescued"
    resc.mkdir(exist_ok=True)
    dst = resc / ("burn-%s-%d.json" % (label, int(time.time())))
    shutil.copy2(box_file, dst)
    return "주인 확인 실패(%r) — 저장본 안 덮고 %s 에 보관" % (owner, dst.name)


def burn(label: str, current: str | None) -> bool:
    """라벨 하나를 확인한다. 이상이 없으면 True."""
    store = CREDS / (label + ".json")
    if not store.exists():
        print("%s: 저장본 없음" % label)
        return False
    if label == current:
        live_fp, store_fp = fp(LIVE), fp(store)
        state = "저장본=자격 자리 일치" if live_fp == store_fp else \
            "저장본이 자격 자리와 다름(지문 %s vs %s)" % (store_fp, live_fp)
        print("%s: 지금 사용 중(생존 자명) / %s" % (label, state))
        if live_fp != store_fp:
            # 자격 자리가 살아 있는 최신이므로, 주인만 맞으면 저장본을 그 값으로 맞춘다
            print("  " + save_refreshed(LIVE, label))
        return True

    live_before = fp(LIVE)
    box = Path(tempfile.mkdtemp(prefix="claude-burn-%s-" % label))
    try:
        shutil.copy2(store, box / ".credentials.json")
        before = fp(box / ".credentials.json")
        rc, out = run_claude(box)
        after = fp(box / ".credentials.json")
        refreshed = after is not None and after != before
        v = verdict_of(rc, out)
        print("%s: %s / 종료코드 %s / 재발급 %s / 응답 앞부분: %r"
              % (label, v, rc, "일어남" if refreshed else "없음", out[:80]))
        if refreshed:
            print("  " + save_refreshed(box / ".credentials.json", label))
        if fp(LIVE) != live_before:
            print("  경고: 자격 자리 파일이 바뀌었습니다. 격리가 샌 것 — 즉시 확인 필요!")
            return False
        return "살아 있음" in v
    finally:
        shutil.rmtree(box, ignore_errors=True)


def main() -> int:
    if sys.platform == "darwin":
        print("Mac 에서는 이 방식이 안 통합니다(운영 맥락 참조). 중단.")
        return 1
    state = read_json(BASE / "state.json") or {}
    current = state.get("current")
    print("지금 쓰는 계정:", current or "(모름)")
    if not isolation_ok():
        print("격리 검사 실패 — CLAUDE_CONFIG_DIR 가 안 먹는 환경입니다. 전부 중단.")
        return 1
    print("격리 검사 통과(가짜 자격이 정상적으로 거절됨)")
    bad = 0
    for label in ("A", "B", "C", "D"):
        if not burn(label, current):
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
