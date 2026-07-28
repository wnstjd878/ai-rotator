"""
== 운영 맥락 ==
용도: 만료된 계정 자격을 사람 손 없이 다시 받아온다(재로그인 자동화).
      재발급 열쇠(refresh token) 수명이 30일 고정이라 그때마다 3대 x 4계정 = 12번을
      손으로 로그인해야 했다. 그 반복을 없앤다.
흐름: (1) claude auth login 을 띄워 인증 주소를 표준출력에서 받는다
      (2) 그 계정 전용 크롬을 디버그 포트로 띄우고 그 주소를 연다
      (3) 승인 버튼을 눌러 되돌아온 주소에서 인증 코드를 꺼낸다
      (4) 코드를 로그인 프로세스의 표준입력으로 넘긴다 (화면·명령 인자에 절대 안 싣는다)
      (5) claude auth status 로 어느 계정이 붙었는지 확인하고 save_account 로 저장한다
실행: python relogin.py <라벨>              그 계정을 다시 로그인해 creds/<라벨>.json 갱신
                                            (끝나면 쓰던 계정으로 되돌린다 — 한도 없는
                                             계정으로 갈아타지는 사고 방지)
      python relogin.py <라벨> --stay       갱신 후 그 계정을 그대로 쓴다(되돌리지 않음)
      python relogin.py <라벨> --setup      전용 크롬만 띄운다(그 계정으로 1회 로그인해둘 때)
      python relogin.py --status            계정별 재발급 열쇠 만료일 보기
      python relogin.py --reset             전용 크롬을 전부 닫고 폴더를 비운다(처음부터 다시)
전제: 계정별 전용 크롬 폴더(chrome/<라벨>)에 그 계정이 한 번 로그인돼 있어야 자동으로 끝난다.
      비어 있으면 창을 띄운 채 기다리므로 그 창에서 사람이 한 번 로그인하면 된다(그 뒤론 자동).
      사용자가 쓰는 크롬은 건드리지 않는다(전용 폴더 + 전용 포트).
주의: 인증 코드는 자격정보다. 화면 출력·명령 인자·로그에 넣지 않는다(CLAUDE.md 9.1).
      claude auth logout 은 절대 부르지 않는다(그 계정 자격이 서버에서 폐기된다).
의도적 미구현: 계정 비밀번호 자동 입력. 브라우저 로그인은 사람이 1회만 한다(계정 보호).
마지막 점검: 2026-07-28
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

CHROME_DIR = BASE / "chrome"          # 계정별 전용 크롬 폴더 (git 제외)
CREDS_DIR = BASE / "creds"
ACCOUNTS = BASE / "accounts.json"
BASE_PORT = 9240                       # 기존 크롬들(9222~9233)과 안 겹치게
CALLBACK_HOST = "platform.claude.com"
LOGIN_TIMEOUT = 900                    # 사람이 로그인해야 할 때 기다리는 시간(초)

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def labels():
    try:
        data = json.loads(ACCOUNTS.read_text(encoding="utf-8"))
        return [x["label"] for x in data if x.get("label")]
    except Exception:
        return sorted(f.stem for f in CREDS_DIR.glob("*.json"))


def port_for(label):
    ls = labels()
    return BASE_PORT + (ls.index(label) if label in ls else 0)


def chrome_bin():
    import shutil

    for c in CHROME_CANDIDATES:
        if Path(c).is_file():
            return c
    found = shutil.which("chrome") or shutil.which("google-chrome")
    if found:
        return found
    raise RuntimeError("크롬을 못 찾았습니다.")


def close_chrome(label):
    """그 계정 전용 크롬만 닫는다. 사용자가 쓰는 크롬은 절대 안 건드린다.

    탭을 전부 닫으면 그 크롬이 스스로 종료된다. 프로세스 목록을 뒤지지 않는 이유는
    명령줄 전체 조회를 안 하기 위해서다(CLAUDE.md 9.1).
    """
    import urllib.error
    import urllib.request

    port = port_for(label)
    try:
        raw = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3).read()
        targets = json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    for t in targets:
        if t.get("type") == "page" and t.get("id"):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{t['id']}", timeout=3)
            except (urllib.error.URLError, OSError):
                pass
    time.sleep(2)
    return True


def guide_page(label):
    """어느 창이 어느 계정인지 화면에 크게 띄우는 안내 쪽. 창이 여러 개일 때 안 헷갈리게."""
    mail = expected_email(label) or "(accounts.json 에 email 없음)"
    CHROME_DIR.mkdir(parents=True, exist_ok=True)
    path = CHROME_DIR / f"_guide_{label}.html"
    path.write_text(
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        f"<title>{label} 계정 창</title>"
        "<style>"
        "body{margin:0;height:100vh;display:flex;flex-direction:column;"
        "align-items:center;justify-content:center;background:#111;color:#f5f5f5;"
        "font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif}"
        ".label{font-size:180px;font-weight:800;line-height:1;color:#ffd400}"
        ".mail{font-size:34px;margin-top:24px;letter-spacing:-0.5px}"
        ".note{font-size:19px;margin-top:40px;color:#9a9a9a;text-align:center;line-height:1.7}"
        "</style></head><body>"
        f"<div class='label'>{label}</div>"
        f"<div class='mail'>{mail}</div>"
        "<div class='note'>이 창은 이 계정 전용입니다.<br>"
        "옆 탭에서 위 주소로 로그인해 주세요.<br>"
        "다른 창에 로그인해도 서로 밀어내지 않습니다.</div>"
        "</body></html>",
        encoding="utf-8",
    )
    return path.as_uri()


def launch_chrome(label, open_login=False):
    """그 계정 전용 폴더로 크롬을 디버그 포트에 띄운다. 이미 떠 있으면 그대로 쓴다.

    open_login 이면 로그인 화면을 띄운다(계정별 최초 1회 셋업용). 창이 여러 개 떴을 때
    헷갈리지 않게 계정 순서대로 위치를 어긋나게 놓는다.
    """
    import urllib.error
    import urllib.request

    port = port_for(label)
    idx = labels().index(label) if label in labels() else 0
    login_url = "https://claude.ai/login"

    try:  # 이미 떠 있는지 먼저 확인 (사람이 로그인해둔 창을 재사용)
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        print(f"[relogin] '{label}' 전용 크롬이 이미 떠 있습니다(포트 {port}).")
        if open_login:  # 떠 있는 창에 로그인 탭만 하나 연다
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as pw:
                    b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    ctx = b.contexts[0] if b.contexts else b.new_context()
                    pg = ctx.new_page()
                    pg.goto(login_url, wait_until="domcontentloaded", timeout=60000)
                    pg.bring_to_front()
            except Exception as e:
                print(f"[relogin] 로그인 탭을 못 열었습니다({type(e).__name__}). 그 창에서 claude.ai 로 가세요.")
        return port
    except (urllib.error.URLError, OSError):
        pass

    profile = CHROME_DIR / label
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [chrome_bin(),
         f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check",
         "--disable-blink-features=AutomationControlled",  # 자동화 티 안 나게
         f"--window-position={80 * idx},{60 * idx}",
         "--window-size=1150,900"]
        + ([guide_page(label), login_url] if open_login else ["about:blank"]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        time.sleep(1)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
            print(f"[relogin] '{label}' 전용 크롬을 띄웠습니다(포트 {port}).")
            return port
        except (urllib.error.URLError, OSError):
            continue
    raise RuntimeError(f"전용 크롬이 포트 {port} 로 안 떴습니다.")


def start_login():
    """claude auth login 을 띄우고 (프로세스, 인증주소) 를 돌려준다."""
    p = subprocess.Popen(
        ["claude", "auth", "login"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        cwd=str(BASE), shell=(sys.platform == "win32"),
    )
    url, deadline = None, time.time() + 60
    buf = ""
    while time.time() < deadline:
        line = p.stdout.readline()
        if not line:
            if p.poll() is not None:
                break
            continue
        buf += line
        m = re.search(r"https://\S*oauth/authorize\S+", line)
        if m:
            url = m.group(0).rstrip("\"'）)")
            break
    if not url:
        p.kill()
        raise RuntimeError(f"인증 주소를 못 받았습니다. 출력 앞부분: {buf[:300]}")
    return p, url


def on_callback(page):
    """승인이 끝나고 되돌아오는 화면에 실제로 도착했는지.

    주소에 문자열이 들어있는지로 보면 안 된다. 로그인 화면 주소(claude.ai/login)의
    returnTo 안에 되돌아올 주소가 통째로 들어있어서 로그인 화면을 완료로 오인한다
    (2026-07-28 실제로 겪음). 반드시 주소의 호스트 이름으로 판정한다.
    """
    from urllib.parse import urlparse

    try:
        return urlparse(page.url).netloc.lower() == CALLBACK_HOST
    except Exception:
        return False


def grab_code(page):
    """되돌아온 화면에서 인증 코드를 꺼낸다. 값은 화면에 찍지 않는다.

    도착 전에는 아무것도 코드로 보지 않는다. 인증 주소 자체에 code=true 라는 항목이
    있어서, 도착 판정을 느슨하게 하면 'true#상태값' 을 코드로 보내고 400 을 받는다.
    """
    from urllib.parse import unquote

    if not on_callback(page):
        return None
    m = re.search(r"[?&]code=([^&#\s]+)", page.url)
    if m and m.group(1) not in ("true", "false"):
        code = unquote(m.group(1))
        state = re.search(r"[?&]state=([^&#\s]+)", page.url)
        return f"{code}#{unquote(state.group(1))}" if state else code
    # 주소에 안 붙고 화면에 보여주는 경우 (사람이 복사하는 그 값)
    for sel in ("code", "pre", "input[readonly]", "[data-testid*='code']"):
        try:
            for el in page.query_selector_all(sel):
                txt = (el.get_attribute("value") or el.inner_text() or "").strip()
                if len(txt) > 20 and " " not in txt:
                    return txt
        except Exception:
            continue
    return None


def approve(page, url, proc=None):
    """인증 주소를 열고 승인 버튼을 누른다. 코드를 돌려준다(못 받으면 None).

    claude auth login 은 기본 브라우저를 자동으로 열어 내 컴퓨터 안 주소로 결과를 받는
    길도 같이 연다. 그쪽이 먼저 끝나면 코드를 넣을 필요가 없으므로 "DONE" 을 돌려준다.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.bring_to_front()  # 사람이 로그인해야 할 수 있으니 창을 앞으로
    except Exception:
        pass
    time.sleep(2)

    told_login = False
    labels_to_click = ["Authorize", "승인", "Allow", "허용", "Continue", "계속하기", "계속"]
    deadline = time.time() + LOGIN_TIMEOUT
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return "DONE"  # 자동으로 열린 브라우저가 먼저 끝냈다
        if on_callback(page):
            break
        here = page.url

        # 로그인·계정 선택 화면이면 사람이 한 번 해야 한다(계정 보호 — 자동 입력 안 함)
        if "/login" in here or "sign-in" in here or "selectAccount" in here:
            if not told_login:
                print("[relogin] 이 전용 크롬에 아직 그 계정 로그인이 없습니다.")
                print("[relogin] 떠 있는 창에서 직접 로그인해 주세요. 끝나면 자동으로 이어집니다.")
                told_login = True
            time.sleep(3)
            continue

        # 승인 화면이면 버튼을 누른다
        for name in labels_to_click:
            try:
                btn = page.get_by_role("button", name=re.compile(name, re.I))
                if btn.count() == 0:
                    btn = page.get_by_role("link", name=re.compile(name, re.I))
                if btn.count() > 0:
                    btn.first.click(timeout=5000)
                    time.sleep(3)
                    break
            except Exception:
                continue
        else:
            time.sleep(3)
        if page.url == here:
            time.sleep(2)

    if proc is not None and proc.poll() is not None:
        return "DONE"
    if not on_callback(page):
        print("[relogin] 승인 화면까지 못 갔습니다. 뜬 창 상태를 확인해 주세요.")
        return None
    time.sleep(2)
    return grab_code(page)


def finish_login(p, code):
    """받은 코드를 로그인 프로세스의 표준입력으로 넘긴다(인자·화면에 안 싣는다)."""
    try:
        p.stdin.write(code + "\n")
        p.stdin.flush()
    except Exception as e:
        return False, f"코드 전달 실패: {type(e).__name__}"
    try:
        out = p.stdout.read()
    except Exception:
        out = ""
    p.wait(timeout=60)
    return p.returncode == 0, (out or "")[-400:]


def current_email():
    """지금 꽂힌 자격이 실제로 누구 계정인지.

    claude auth status 는 별도 캐시를 읽어 파일 교체 후 낡으므로 쓰지 않는다
    (switch_account.cred_email 설명 참조). 자격으로 서버에 직접 물어본다.
    """
    import switch_account as sa

    return sa.cred_email()


def expected_email(label):
    try:
        data = json.loads(ACCOUNTS.read_text(encoding="utf-8"))
        for x in data:
            if x.get("label") == label:
                return x.get("email")
    except Exception:
        pass
    return None


def restore_previous(before):
    """재로그인 전에 쓰던 계정으로 되돌린다.

    claude auth login 은 그 계정을 지금 쓰는 자격 자리에 꽂아버린다. 만료 갱신만 하려고
    부른 건데 한도가 0% 인 계정으로 갈아타지면 곤란하다(2026-07-28 실제로 그랬음).
    그래서 갱신이 끝나면 원래 쓰던 계정으로 되돌려 놓는다.
    """
    if not before:
        return
    import switch_account as sa

    if sa.live_label() == before:
        return  # 이미 그 계정이면 할 일 없음
    src = CREDS_DIR / f"{before}.json"
    if not src.exists():
        print(f"[relogin] 되돌릴 '{before}' 저장본이 없어 그대로 둡니다.")
        return
    r = subprocess.run([sys.executable, str(BASE / "claude_pool.py"), "--use", before],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode == 0:
        print(f"[relogin] 쓰던 계정 '{before}' 로 되돌렸습니다.")
    else:
        print(f"[relogin] '{before}' 로 못 되돌렸습니다: {(r.stderr or r.stdout).strip()[:200]}")


def relogin(label, keep_current=True):
    if label not in labels():
        print(f"[relogin] '{label}' 은 accounts.json 에 없는 라벨입니다. 쓸 수 있는 라벨: {labels()}")
        return 1
    from playwright.sync_api import sync_playwright

    import switch_account as sa

    before = sa.live_label() if keep_current else None  # 지금 쓰는 계정을 기억해둔다

    port = launch_chrome(label)
    print("[relogin] 로그인 절차를 시작합니다...")
    p, url = start_login()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            code = approve(page, url, p)
    except Exception as e:
        p.kill()
        print(f"[relogin] 브라우저 단계에서 막혔습니다: {type(e).__name__}: {e}")
        restore_previous(before)
        return 1

    if code == "DONE":
        print("[relogin] 자동으로 열린 브라우저에서 로그인이 먼저 끝났습니다.")
        try:
            p.wait(timeout=30)
        except Exception:
            pass
    elif code:
        print(f"[relogin] 인증 코드를 받았습니다(길이 {len(code)}). 로그인을 마칩니다...")
        ok, tail = finish_login(p, code)
        if not ok:
            print(f"[relogin] 로그인이 끝나지 않았습니다: {tail}")
            restore_previous(before)
            return 1
    else:
        p.kill()
        print("[relogin] 인증 코드를 못 받았습니다.")
        restore_previous(before)
        return 1

    email = current_email()
    want = expected_email(label)
    print(f"[relogin] 로그인된 계정: {email or '(못 읽음)'}")
    if want and email and want.lower() != email.lower():
        print(f"[relogin] 경고: '{label}' 는 {want} 여야 하는데 {email} 로 로그인됐습니다.")
        print("[relogin] 저장하지 않고 멈춥니다(라벨이 섞이면 계정을 잃습니다).")
        restore_previous(before)
        return 1

    r = subprocess.run([sys.executable, str(BASE / "save_account.py"), label],
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("[relogin] 자격 저장에 실패했습니다.")
        restore_previous(before)
        return 1
    if email and not want:
        print(f"[relogin] 참고: accounts.json 의 '{label}' 에 email 을 적어두면 다음부터 대조합니다.")
    print(f"[relogin] '{label}' 재로그인 완료.")
    restore_previous(before)  # 만료 갱신하려다 한도 없는 계정으로 갈아타지 않게
    return 0


def show_status():
    """계정별 재발급 열쇠 만료일. 30일 고정이라 미리 보고 몰아서 갱신한다."""
    import hashlib  # noqa: F401  (지문은 안 찍는다 — 값 노출 금지)

    print(f"{'라벨':<6} {'재발급 열쇠 만료':<22} 남은 일수")
    for label in labels():
        f = CREDS_DIR / f"{label}.json"
        if not f.exists():
            print(f"{label:<6} {'(저장본 없음)':<22} -")
            continue
        try:
            o = json.loads(f.read_text(encoding="utf-8"))["claudeAiOauth"]
            exp = int(o.get("refreshTokenExpiresAt") or 0) / 1000
        except Exception:
            print(f"{label:<6} {'(못 읽음)':<22} -")
            continue
        left = (exp - time.time()) / 86400
        print(f"{label:<6} {time.strftime('%Y-%m-%d %H:%M', time.localtime(exp)):<22} {left:.1f}일")


def main():
    args = [a for a in sys.argv[1:] if a.strip()]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.split("실행:")[1].split("전제:")[0].strip())
        return 1
    if args[0] == "--status":
        show_status()
        return 0
    if args[0] == "--reset":
        import shutil as _sh

        for lb in labels():
            if close_chrome(lb):
                print(f"[relogin] '{lb}' 전용 크롬을 닫았습니다.")
        time.sleep(2)
        if CHROME_DIR.exists():
            try:
                _sh.rmtree(CHROME_DIR)
                print("[relogin] 전용 크롬 폴더를 비웠습니다(처음 상태).")
            except Exception as e:
                print(f"[relogin] 폴더를 다 못 지웠습니다({type(e).__name__}). 창이 아직 떠 있을 수 있습니다.")
        return 0
    label = args[0]
    if "--stay" in args:  # 일부러 그 계정으로 갈아타고 싶을 때
        return relogin(label, keep_current=False)
    if "--setup" in args:
        port = launch_chrome(label, open_login=True)
        mail = expected_email(label) or "(accounts.json 에 email 없음)"
        print(f"[relogin] '{label}' 창(포트 {port})에서 {mail} 로 로그인해 두세요.")
        print(f"[relogin] 그 뒤부터는 python relogin.py {label} 이 사람 손 없이 끝납니다.")
        return 0
    return relogin(label)


if __name__ == "__main__":
    raise SystemExit(main())
