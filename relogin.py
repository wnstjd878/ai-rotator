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
      python relogin.py --remote <머신> <라벨>  원격 머신(macmini/winserver) 자격을 갱신.
                                            그쪽 CLI 를 ssh 로 띄우고 승인은 이 PC 크롬이
                                            한다. 원격 데스크톱 접속 없이 여기서 끝난다.
      python relogin.py --serve <라벨>       원격에서 불리는 쪽(사람이 직접 칠 일 없음)
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
import shutil
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
LIVE_BACKUP = BASE / "creds" / "prev" / "_before_relogin.json"  # 되돌릴 자격 임시 보관
CREDS_DIR = BASE / "creds"
ACCOUNTS = BASE / "accounts.json"
BASE_PORT = 9250                       # 9240대는 백도씨·아임웹 크롬(9241)과 겹쳐 9250대로 옮김(2026-09-06)
CALLBACK_HOST = "platform.claude.com"
WARM_URL = "https://claude.ai/new"     # 갱신 전에 자동화 연결 없이 먼저 띄우는 화면
LOGIN_TIMEOUT = 900                   # 사람이 로그인해야 할 때 기다리는 시간(초)

# 원격 머신: 이름 → (그쪽 작업 폴더, 파이썬 명령, 그쪽 기본 셸). ssh 설정에 그 이름이 있어야 한다.
# 셸을 따로 적는 이유: 명령 이어붙이는 문자가 다르다(PowerShell 은 ';', 나머지는 '&&').
# 예전엔 파이썬 명령이 python3 인지로 갈랐는데, 노트북도 'python' 이라 그 기준으로는
# PowerShell 로 잘못 갈린다(노트북 기본 셸은 cmd). 2026-08-04 출장 노트북 추가하며 바꿈.
REMOTES = {
    "macmini": ("projects/ai-rotator", "python3", "unix"),
    "winserver": (r"C:\Users\pc\projects\ai-rotator", "python", "powershell"),
    "laptop": (r"C:\Users\wnstj\ai-rotator", "python", "cmd"),
}

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
        # 첫 탭은 항상 "이 창은 A 계정" 안내 쪽 — 창이 여러 개 떠도 어느 계정인지 보이게(2026-09-12 사용자 요청)
        # 갱신 때도 claude.ai 를 자동화 연결 없이 먼저 한 번 띄운다. 창을 띄우자마자
        # Playwright 가 붙은 채로 첫 로그인을 하면 claude.ai 가 "브라우저를 확인할 수
        # 없습니다" 로 막는다(2026-10-13 A·B 에서 겪음, 먼저 띄운 뒤 붙이니 A~D 전부 통과).
        + [guide_page(label)] + [login_url if open_login else WARM_URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        time.sleep(1)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
            print(f"[relogin] '{label}' 전용 크롬을 띄웠습니다(포트 {port}).")
            if not open_login:
                wait_warm(port)
            return port
        except (urllib.error.URLError, OSError):
            continue
    raise RuntimeError(f"전용 크롬이 포트 {port} 로 안 떴습니다.")


def wait_warm(port, limit=25):
    """먼저 띄운 claude.ai 탭이 다 뜰 때까지 자동화 연결 없이 기다린다(탭 목록 조회만)."""
    import urllib.error
    import urllib.request

    settled = 0
    deadline = time.time() + limit
    while time.time() < deadline:
        time.sleep(1)
        try:
            raw = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3).read()
            pages = [t for t in json.loads(raw.decode("utf-8")) if t.get("type") == "page"]
        except (urllib.error.URLError, OSError, ValueError):
            continue
        # 제목이 생기면 화면이 뜬 것이다(로그인돼 있으면 /new, 아니면 /login 으로 간다).
        if any("claude.ai" in t.get("url", "") and "Claude" in t.get("title", "") for t in pages):
            settled += 1
            if settled >= 4:  # 뜬 뒤 몇 초 더 둔다(확인 절차가 끝날 시간)
                return True
        else:
            settled = 0
    print("[relogin] claude.ai 첫 화면이 제때 안 떴습니다. 그대로 이어갑니다.")
    return False


def claude_bin():
    """claude 실행 파일 경로. ssh 비대화형은 PATH 가 /usr/bin:/bin 뿐이라 직접 찾는다."""
    import shutil

    found = shutil.which("claude") or shutil.which("claude.exe") or shutil.which("claude.cmd")
    if found:
        return found
    for c in ("/opt/homebrew/bin/claude", "/usr/local/bin/claude",
              str(Path.home() / ".local/bin/claude"),
              str(Path.home() / ".local/bin/claude.exe"),
              str(Path.home() / ".local/bin/claude.cmd")):
        if Path(c).is_file():
            return c
    raise RuntimeError("claude 실행 파일을 못 찾았습니다.")


def claude_cmd(*args):
    """claude 실행 명령. .cmd/.bat 은 그대로는 실행이 안 되므로 cmd /c 로 감싼다.

    winserver 의 claude 는 가로채기(claude.CMD)라 이 처리가 없으면 실행 자체가 안 된다.
    """
    exe = claude_bin()
    if sys.platform == "win32" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *args]
    return [exe, *args]


def start_login(env=None):
    """claude auth login 을 띄우고 (프로세스, 인증주소) 를 돌려준다.

    `env` 를 주면 그 환경으로 띄운다(자리 로그인은 CLAUDE_CONFIG_DIR 을 임시 폴더로 준다).
    """
    p = subprocess.Popen(
        claude_cmd("auth", "login"),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        cwd=str(BASE), shell=False, env=env,
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


def pump_wait(page, ms):
    """브라우저 이벤트를 소화하면서 기다린다.

    time.sleep 으로 기다리면 새 탭 열림·주소 변경 알림이 파이썬 쪽에 반영되지 않아
    page.url 과 탭 목록이 낡은 채 멈춘다(2026-07-30 A·C·D 로그인 대기에서 실제로 겪음 —
    사람이 로그인을 끝냈는데 절차가 낡은 화면만 보다 제한 시간까지 헛돌았다).
    브라우저에 말을 거는 대기(wait_for_timeout)여야 알림이 소화된다.
    """
    try:
        page.wait_for_timeout(ms)
        return
    except Exception:
        pass
    try:
        for pg in list(page.context.pages):
            try:
                pg.wait_for_timeout(ms)
                return
            except Exception:
                continue
    except Exception:
        pass
    time.sleep(ms / 1000)


def find_arrival(page):
    """같은 창의 모든 탭에서 도착 화면을 찾는다. 없으면 None.

    로그인 과정(구글 로그인 등)이 처음 연 탭을 닫고 새 탭으로 흐름을 이어가기도 한다.
    처음 탭만 지켜보면 도착을 영영 못 보고 제한 시간까지 헛돈다(2026-07-30 A·C 에서
    실제로 겪음 — 사람이 로그인을 끝냈는데 "승인 화면까지 못 갔습니다"로 실패).
    """
    try:
        pages = list(page.context.pages)
    except Exception:
        pages = [page]
    for pg in pages:
        if on_callback(pg):
            return pg
    return None


def approve(page, url, proc=None):
    """인증 주소를 열고 승인 버튼을 누른다. 코드를 돌려준다(못 받으면 None).

    claude auth login 은 기본 브라우저를 자동으로 열어 내 컴퓨터 안 주소로 결과를 받는
    길도 같이 연다. 그쪽이 먼저 끝나면 코드를 넣을 필요가 없으므로 "DONE" 을 돌려준다.
    """
    # 지난 시도가 남긴 옛 도착 탭을 먼저 닫는다. 안 닫으면 모든 탭을 훑는 도착 찾기가
    # 그 탭의 낡은 코드(1회용이라 이미 무효)를 집어 400 을 받는다(2026-07-30 C 에서 겪음).
    for pg in list(page.context.pages):
        if pg is not page and on_callback(pg):
            try:
                pg.close()
            except Exception:
                pass
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
        arrived = find_arrival(page)
        if arrived is not None:
            page = arrived
            break
        try:
            here = page.url
        except Exception:
            # 처음 탭이 닫혔다. 남은 탭들에서 도착을 계속 찾는다.
            pump_wait(page, 3000)
            continue

        # 로그인·계정 선택 화면이면 사람이 한 번 해야 한다(계정 보호 — 자동 입력 안 함)
        if "/login" in here or "sign-in" in here or "selectAccount" in here:
            if not told_login:
                print("[relogin] 이 전용 크롬에 아직 그 계정 로그인이 없습니다.")
                print("[relogin] 떠 있는 창에서 직접 로그인해 주세요. 끝나면 자동으로 이어집니다.")
                told_login = True
            pump_wait(page, 3000)
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
            pump_wait(page, 3000)
        try:
            moved = page.url != here
        except Exception:
            moved = True
        if not moved:
            pump_wait(page, 2000)

    if proc is not None and proc.poll() is not None:
        return "DONE"
    page = find_arrival(page) or page
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


def restore_previous(before, keep=None):
    """재로그인 전에 쓰던 계정으로 되돌린다.

    claude auth login 은 그 계정을 지금 쓰는 자격 자리에 꽂아버린다. 만료 갱신만 하려고
    부른 건데 한도가 0% 인 계정으로 갈아타지면 곤란하다(2026-07-28 실제로 그랬음).
    그래서 갱신이 끝나면 원래 쓰던 계정으로 되돌려 놓는다.

    keep 은 시작 때 떠둔 원래 자격이다. 그걸로 되돌리면 방금 갱신된 최신 자격이
    그대로 살아난다. 없으면 creds 저장본으로 되돌린다.
    """
    if not before:
        return
    import switch_account as sa

    live = Path.home() / ".claude" / ".credentials.json"
    if keep and Path(keep).exists():
        try:
            # 순서가 중요하다. --use 가 creds 저장본을 자격 자리에 꽂으므로 그걸 먼저 부르고
            # (기록도 같이 맞춰진다), 그 위에 시작 때 떠둔 최신본을 얹는다.
            subprocess.run([sys.executable, str(BASE / "claude_pool.py"), "--use", before],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
            shutil.copy2(keep, live)
            shutil.copy2(keep, CREDS_DIR / f"{before}.json")  # 저장본도 최신으로
            Path(keep).unlink(missing_ok=True)
            print(f"[relogin] 쓰던 계정 '{before}' 로 되돌렸습니다.")
            return
        except Exception as e:
            print(f"[relogin] 떠둔 자격으로 못 되돌렸습니다({type(e).__name__}). 저장본으로 시도합니다.")

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


def finish_common(p, code, label, before):
    """로그인 마무리 + 계정 대조 + 저장 + 되돌리기. 이 PC용과 원격용이 함께 쓴다.

    되돌리기 전까지 몇 초간은 새로 로그인한 계정이 꽂혀 있다. 그 사이에 봇이나 사람이
    claude 를 켜면 한도 없는 계정을 물어 "주간 한도 도달" 을 본다(2026-07-28 실제로 겪음).
    그래서 원래 자격을 미리 떠놨다가 저장이 끝나는 즉시 제자리로 돌린다.
    """
    keep = None
    if before:
        try:  # 되돌릴 자격을 미리 확보(파일이 곧 덮어써지므로 지금 떠둔다)
            keep = LIVE_BACKUP
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path.home() / ".claude" / ".credentials.json", keep)
        except Exception:
            keep = None

    if code == "DONE":
        print("[relogin] 자동으로 열린 브라우저에서 로그인이 먼저 끝났습니다.")
        try:
            p.wait(timeout=30)
        except Exception:
            pass
    else:
        ok, tail = finish_login(p, code)
        if not ok:
            print(f"[relogin] 로그인이 끝나지 않았습니다: {tail}")
            restore_previous(before)
            return 1

    email = current_email()
    want = expected_email(label)
    print(f"[relogin] 로그인된 계정: {email or '(못 읽음)'}")
    if want and email and want.lower() != email.lower():
        print(f"[relogin] 경고: '{label}' 는 {want} 여야 하는데 {email} 로 로그인됐습니다.")
        print("[relogin] 저장하지 않고 멈춥니다(라벨이 섞이면 계정을 잃습니다).")
        restore_previous(before, keep)
        return 1

    r = subprocess.run([sys.executable, str(BASE / "save_account.py"), label],
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("[relogin] 자격 저장에 실패했습니다.")
        restore_previous(before, keep)
        return 1
    print(f"[relogin] '{label}' 재로그인 완료.")
    if before == label:
        # 지금 쓰던 계정을 그 자리에서 갱신한 경우. 여기서 keep(시작 때 떠둔 옛 자격)으로
        # 되돌리면 방금 받은 새 자격을 활성 자리와 저장본 양쪽에서 옛것으로 도로 덮는다 —
        # B 를 세 번 갱신해도 만료가 7일대 그대로였던 원인(2026-08-18 실측). 새 자격이
        # 이미 활성 자리와 저장본에 있으므로 되돌릴 것 없이 떠둔 파일만 지운다.
        # 실패 경로들(로그인 미완/라벨 불일치/저장 실패)은 되돌리는 게 맞아 그대로 둔다.
        if keep:
            Path(keep).unlink(missing_ok=True)
        print(f"[relogin] '{label}' 을(를) 쓰던 중이라 되돌리기 없이 새 자격을 그대로 씁니다.")
        return 0
    restore_previous(before, keep)
    return 0


def relogin_slot(label, bucket):
    """모델별 자리(slot) 전용 자격을 새로 받는다(2026-09-12). creds/slots/<칸>/<라벨>.json 에 저장.

    공용 자격(creds/<라벨>.json)과는 **다른 로그인(다른 재발급 열쇠)** 이어야 한다. 같은 자격을
    두 곳에 두면 한쪽이 갱신할 때 다른 쪽 열쇠가 폐기된다. 그래서 임시 설정 폴더에서 로그인해
    그 결과만 옮기고, 지금 쓰는 공용 자격은 전혀 건드리지 않는다.
    """
    import os
    import shutil as _sh

    import slot_pool

    if label not in labels():
        print(f"[relogin] '{label}' 은 accounts.json 에 없는 라벨입니다. 쓸 수 있는 라벨: {labels()}")
        return 1
    if bucket not in slot_pool.BUCKETS:
        print(f"[relogin] 칸 이름은 {', '.join(slot_pool.BUCKETS)} 중 하나여야 합니다.")
        return 1
    from playwright.sync_api import sync_playwright

    import switch_account as sa

    tmp = slot_pool.POOL_ROOT / f"_login_{bucket}_{label}"
    if tmp.exists():
        _sh.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    root_cfg = Path.home() / ".claude.json"
    if root_cfg.exists():  # 첫 실행 안내를 건너뛰게 설정 사본을 심는다(로그인이 계정 정보를 덮어쓴다)
        _sh.copy2(root_cfg, tmp / ".claude.json")
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env["CLAUDE_CONFIG_DIR"] = str(tmp)
    # 기본 브라우저(사용자 크롬)에 인증 탭이 하나씩 쌓이지 않게 빈 브라우저를 꽂는다(BROWSER 는 claude 가 존중함, 2026-09-12 실측).
    env["BROWSER"] = str(BASE / "browser_noop.cmd")

    port = launch_chrome(label)
    print(f"[relogin] {bucket} 자리용 '{label}' 로그인을 시작합니다...")
    p, url = start_login(env=env)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            code = approve(page, url, p)
    except Exception as e:
        p.kill()
        print(f"[relogin] 브라우저 단계에서 막혔습니다: {type(e).__name__}: {e}")
        return 1
    if not code:
        p.kill()
        print("[relogin] 인증 코드를 못 받았습니다.")
        return 1
    if code == "DONE":
        try:
            p.wait(timeout=30)
        except Exception:
            pass
    else:
        ok, tail = finish_login(p, code)
        if not ok:
            print(f"[relogin] 로그인이 끝나지 않았습니다: {tail}")
            return 1
    cred = tmp / ".credentials.json"
    if not cred.exists():
        try:
            tail = re.sub(r"code=[^&\s]+", "code=…", (p.stdout.read() or ""))[-400:]
        except Exception:
            tail = ""
        print(f"[relogin] 로그인은 끝났는데 자격 파일이 안 생겼습니다(종료코드 {p.returncode}). 출력 끝: {tail}")
        return 1
    email = sa.cred_email(cred)
    want = expected_email(label)
    print(f"[relogin] 로그인된 계정: {email or '(못 읽음)'}")
    if want and email and want.lower() != email.lower():
        print(f"[relogin] 경고: '{label}' 는 {want} 여야 하는데 {email} 로 로그인됐습니다. 저장하지 않습니다.")
        _sh.rmtree(tmp, ignore_errors=True)
        return 1
    dest = slot_pool.store_path(bucket, label)
    obj = json.loads(cred.read_text(encoding="utf-8"))
    sa._write_owner_only(dest, obj)
    try:  # 계정 표시 정보(이메일 등)는 자리 폴더의 .claude.json 에 맞춰 넣는 용도
        info = (json.loads((tmp / ".claude.json").read_text(encoding="utf-8")) or {}).get("oauthAccount")
        if isinstance(info, dict):
            slot_pool.account_path(bucket, label).write_text(json.dumps(info), encoding="utf-8")
    except Exception:
        pass
    _sh.rmtree(tmp, ignore_errors=True)
    print(f"[relogin] {bucket} 자리용 '{label}' 자격을 저장했습니다.")
    return 0


URL_MARK = "__RELOGIN_URL__"


def serve(label):
    """원격에서 불리는 쪽. 인증 주소를 표준출력으로 내고 인증 코드를 표준입력으로 받는다.

    브라우저가 없는 머신(ssh 로 붙은 Mac·winserver)을 위한 방식이다. 승인은 부르는 쪽
    PC 의 전용 크롬이 하고, 받은 코드만 이 통로로 넘어온다(값이 명령 인자에 안 실림).
    """
    if label not in labels():
        print(f"[relogin] '{label}' 은 accounts.json 에 없는 라벨입니다.", flush=True)
        return 1
    import switch_account as sa

    before = sa.live_label()
    print(f"[relogin] 쓰던 계정: {before or '(모름)'}", flush=True)
    try:
        p, url = start_login()
    except Exception as e:
        print(f"[relogin] 로그인 절차를 못 띄웠습니다: {type(e).__name__}: {e}", flush=True)
        return 1
    print(f"{URL_MARK} {url}", flush=True)

    code = (sys.stdin.readline() or "").strip()
    if not code or code == "__ABORT__":
        p.kill()
        print("[relogin] 부르는 쪽에서 중단했습니다.", flush=True)
        restore_previous(before, keep)
        return 1
    return finish_common(p, "DONE" if code == "__DONE__" else code, label, before)


def remote(host, label):
    """이 PC 의 전용 크롬으로 승인하고, 원격 머신의 자격을 갱신한다."""
    from playwright.sync_api import sync_playwright

    if host not in REMOTES:
        print(f"[relogin] 모르는 머신 '{host}'. 쓸 수 있는 것: {', '.join(REMOTES)}")
        return 1
    workdir, py, shell = REMOTES[host]
    if shell == "powershell":   # 세미콜론으로 이어붙인다
        remote_cmd = f"cd {workdir}; {py} relogin.py --serve {label}"
    else:                       # unix(zsh) 와 cmd 는 둘 다 && 로 이어붙인다
        remote_cmd = f"cd {workdir} && {py} relogin.py --serve {label}"

    p = subprocess.Popen(
        ["ssh", host, remote_cmd],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )

    url, deadline = None, time.time() + 120
    while time.time() < deadline:
        line = p.stdout.readline()
        if not line:
            if p.poll() is not None:
                break
            continue
        if line.startswith(URL_MARK):
            url = line[len(URL_MARK):].strip()
            break
        if line.strip():
            print(f"  [{host}] {line.rstrip()}")
    if not url:
        p.kill()
        print(f"[relogin] {host} 에서 인증 주소를 못 받았습니다.")
        return 1

    print(f"[relogin] {host} 의 인증 주소를 받았습니다. 이 PC 크롬에서 승인합니다...")
    try:
        port = launch_chrome(label)
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            code = approve(page, url)  # 원격이라 '자동 브라우저' 길은 안 쓴다
    except Exception as e:
        try:
            p.stdin.write("__ABORT__\n")
            p.stdin.flush()
        except Exception:
            pass
        p.kill()
        print(f"[relogin] 브라우저 단계에서 막혔습니다: {type(e).__name__}: {e}")
        return 1

    if not code or code == "DONE":
        try:
            p.stdin.write("__ABORT__\n")
            p.stdin.flush()
        except Exception:
            pass
        p.kill()
        print("[relogin] 인증 코드를 못 받았습니다.")
        return 1

    print(f"[relogin] 인증 코드를 받았습니다(길이 {len(code)}). {host} 로 넘깁니다...")
    try:
        p.stdin.write(code + "\n")   # 값은 통로로만 넘어간다(명령 인자에 안 실림)
        p.stdin.flush()
    except Exception as e:
        p.kill()
        print(f"[relogin] 코드 전달 실패: {type(e).__name__}")
        return 1

    for line in p.stdout:
        if line.strip():
            print(f"  [{host}] {line.rstrip()}")
    p.wait(timeout=120)
    return p.returncode


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
        restore_previous(before, keep)
        return 1

    if not code:
        p.kill()
        print("[relogin] 인증 코드를 못 받았습니다.")
        restore_previous(before, keep)
        return 1
    if code != "DONE":
        print(f"[relogin] 인증 코드를 받았습니다(길이 {len(code)}). 로그인을 마칩니다...")
    return finish_common(p, code, label, before)


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
    if args[0] == "--serve":  # 원격에서 불리는 쪽
        return serve(args[1]) if len(args) > 1 else 1
    if args[0] == "--remote":  # 이 PC 크롬으로 원격 머신 자격을 갱신
        if len(args) < 3:
            print(f"사용법: python relogin.py --remote <{'|'.join(REMOTES)}> <라벨>")
            return 1
        return remote(args[1], args[2])
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
    if args[0] == "--slot":  # 모델별 자리 전용 자격
        if len(args) < 3:
            print("사용법: python relogin.py --slot <Opus|Fable> <라벨>")
            return 1
        return relogin_slot(args[2], args[1])
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
