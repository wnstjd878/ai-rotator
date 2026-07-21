"""
== 운영 맥락 ==
용도: 지금 크롬(디버그 포트 9222)에 붙어, 로그인된 claude.ai 계정의
      Fable / 모든 모델 / 세션 사용률을 화면에서 읽고 "Fable 사용가능/막힘"을 판정한다.
판정 기준: Fable 막대 >= 50% 이면 막힘 (사장님 확인: 화면이 전부, 50% 넘으면 안 됨).
실행: (1) launch_chrome_debug.ps1 로 크롬을 9222 로 띄운 뒤  (2) python reader.py
      계정(구글 프로필)을 바꿔가며 3번 돌리면 3계정 상태를 다 본다.
출력: 콘솔 표 + captured/usage.png
전제: CDP 붙기. 재로그인 안 함.
마지막 점검: 2026-07-18
"""

import re
import sys
import time
from pathlib import Path

try:  # Windows 콘솔(cp949)에서 한글 판정 출력이 깨지지 않게
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
CAP_DIR = BASE / "captured"
CAP_DIR.mkdir(exist_ok=True)

CDP = "http://localhost:9222"
USAGE_URL = "https://claude.ai/settings/usage"
FULL = 100  # 표시된 사용률 중 하나라도 이 값 이상이면 그 계정은 막힘(한도 도달)
EMAIL_RE = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"


def pct_after(text, label):
    """'<label> ... 27% 사용됨' 형태에서 라벨 뒤에 가장 가까운 % 숫자를 찾는다."""
    idx = text.find(label)
    if idx == -1:
        return None
    window = text[idx: idx + 200]
    m = re.search(r"(\d{1,3})\s*%", window)
    return int(m.group(1)) if m else None


def all_pcts(text):
    """화면에 보이는 모든 0~100% 숫자를 모은다 (모델 구분 없이 최대 사용률 판정용)."""
    out = []
    for m in re.findall(r"(\d{1,3})\s*%", text):
        v = int(m)
        if 0 <= v <= 100:
            out.append(v)
    return out


def connect_page(p):
    """열려있는 크롬(9222)에 붙어 새 탭 하나를 연다. (browser, page) 반환."""
    browser = p.chromium.connect_over_cdp(CDP)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    return browser, context.new_page()


def read_usage(page):
    """usage 페이지를 열어 사용률을 읽는다. rotate.py 가 계정마다 재사용한다.
    반환 dict:
      logged_in  : 로그인 상태 (False 면 나머지 값 없음)
      account    : 화면에서 읽은 이메일 (없으면 None)
      fable      : Fable 사용률 % (None = 못읽음)
      all_models : 모든 모델 % / session : 현재 세션 %
    부수효과: captured/usage_dom.txt, captured/usage.png 갱신.
    """
    page.goto(USAGE_URL, wait_until="networkidle", timeout=60000)
    time.sleep(3)

    if "login" in page.url:
        return {"logged_in": False}

    try:
        text = page.inner_text("body")
    except Exception:
        text = ""
    (CAP_DIR / "usage_dom.txt").write_text(text, encoding="utf-8")
    try:
        page.screenshot(path=str(CAP_DIR / "usage.png"), full_page=True)
    except Exception:
        pass

    emails = sorted(set(re.findall(EMAIL_RE, text)))
    pcts = all_pcts(text)
    max_pct = max(pcts) if pcts else None
    return {
        "logged_in": True,
        "account": emails[0] if emails else None,
        "fable": pct_after(text, "Fable"),
        "all_models": pct_after(text, "모든 모델"),
        "session": pct_after(text, "현재 세션"),
        "max_pct": max_pct,  # 화면 전체에서 가장 높은 사용률
        "usable": (max_pct is not None and max_pct < FULL),
    }


def verdict_line(info):
    """read_usage 결과 dict 를 사람이 읽는 판정 한 줄로. 기준: 한도 100% 아니면 사용 가능."""
    if not info.get("logged_in"):
        return "로그아웃 — 크롬에서 로그인 필요"
    mp = info.get("max_pct")
    if mp is None:
        return "사용률을 못 읽음 (usage_dom.txt / usage.png 확인)"
    if mp >= FULL:
        return f"막힘  (최대 사용률 {mp}% — 한도 도달)"
    return f"사용 가능  (최대 사용률 {mp}%, 여유 {FULL - mp}%p)"


def main():
    with sync_playwright() as p:
        try:
            browser, page = connect_page(p)
        except Exception as e:
            print(f"[reader] 크롬(9222)에 못 붙었습니다: {e}")
            print("[reader] 먼저 launch_chrome_debug.ps1 로 크롬을 디버그 포트로 띄우세요.")
            return 1

        print(f"[reader] {USAGE_URL} 여는 중...")
        info = read_usage(page)
        page.close()

        if not info.get("logged_in"):
            print("[reader] 이 크롬 세션이 로그아웃 상태입니다. 크롬에서 로그인 후 다시 실행하세요.")
            return 1

        print("\n========== 사용량 판정 ==========")
        print(f"  계정        : {info.get('account') or '(이메일 못읽음)'}")
        print(f"  현재 세션   : {info.get('session') if info.get('session') is not None else '?'}%")
        print(f"  모든 모델   : {info.get('all_models') if info.get('all_models') is not None else '?'}%")
        print(f"  Fable       : {info.get('fable') if info.get('fable') is not None else '?'}%")
        print(f"  최대 사용률 : {info.get('max_pct') if info.get('max_pct') is not None else '?'}%")
        print(f"  판정        : {verdict_line(info)}")
        print("=================================")
        print("[reader] 여러 계정을 자동으로 순회하려면 rotate.py 를 쓰세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
