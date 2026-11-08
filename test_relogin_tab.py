"""
== 운영 맥락 ==
용도: relogin.py 의 "탭 주소에서 인증 코드 꺼내기"(2026-11-08) 시험. 크롬·claude 를 부르지 않는다.
실행: python test_relogin_tab.py   (통과/실패 줄과 실패 개수)
확인: 1 되돌아온 주소(platform.claude.com/oauth/code/callback?code=&state=)에서 코드#상태 를 꺼낸다
      2 인증 주소(code=true)·로그인 화면(returnTo 안에 되돌아올 주소)은 코드로 보지 않는다
      3 open_tab_and_wait: 탭 목록에 되돌아온 주소가 나타나면 그 코드를 돌려준다(로그인 프로세스가 안 끝나도)
      4 로그인 프로세스가 먼저 끝나면 DONE / 5 시간 안에 아무것도 없으면 ""
마지막 점검: 2026-11-08
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relogin as R

fail = 0


def ok(c, m):
    global fail
    print(("통과 " if c else "실패 ") + m)
    if not c:
        fail += 1


CB = "https://platform.claude.com/oauth/code/callback?code=testcode-abc_123&state=st%2Fate9"
AUTH = "https://claude.ai/oauth/authorize?code=true&client_id=x&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&state=st"
AUTH2 = "https://platform.claude.com/oauth/authorize?code=true&state=st"
LOGIN = "https://claude.ai/login?returnTo=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback%3Fcode%3Dzzz"

ok(R.code_from_url(CB) == "testcode-abc_123#st/ate9", "1 되돌아온 주소에서 코드#상태 (" + str(R.code_from_url(CB)) + ")")
ok(R.code_from_url(AUTH) is None and R.code_from_url(AUTH2) is None, "2 인증 주소(code=true)는 코드 아님")
ok(R.code_from_url(LOGIN) is None, "2 로그인 화면(returnTo)은 코드 아님")
ok(R.code_from_url("https://claude.ai/new") is None and R.code_from_url("") is None, "2 다른 탭·빈 주소는 None")


class Proc:
    def __init__(self, ends_at=None):
        self.n = 0
        self.ends_at = ends_at

    def poll(self):
        self.n += 1
        return 0 if self.ends_at is not None and self.n >= self.ends_at else None


popen_calls = []
R.subprocess.Popen = lambda *a, **k: popen_calls.append(a[0]) or None   # 크롬 안 띄움
R.chrome_bin = lambda: "chrome.exe"
R.port_for = lambda label: 0
R.LOGIN_TIMEOUT = 3

tick = {"n": 0}


def urls_appear_later(port):
    tick["n"] += 1
    return ["https://claude.ai/new", AUTH] if tick["n"] < 2 else ["https://claude.ai/new", CB]


R.tab_urls = urls_appear_later
r = R.open_tab_and_wait("E", AUTH, Proc())
ok(r == "testcode-abc_123#st/ate9", "3 승인 뒤 탭 주소에서 코드를 돌려줌 (" + str(r) + ")")
ok(popen_calls and popen_calls[0][0] == "chrome.exe" and popen_calls[0][2] == AUTH, "3 전용 크롬에 인증 주소 탭을 염")

R.tab_urls = lambda port: ["https://claude.ai/new", AUTH]
ok(R.open_tab_and_wait("E", AUTH, Proc(ends_at=2)) == "DONE", "4 로그인이 먼저 끝나면 DONE")
ok(R.open_tab_and_wait("E", AUTH, Proc()) == "", "5 시간 안에 승인 없으면 빈 값")

src = Path(R.__file__).read_text(encoding="utf-8")
ok('env["BROWSER"] = str(BASE / "browser_noop.cmd")' in src.split("def relogin(label")[1].split("def show_status")[0], "본체 로그인도 기본 브라우저에 승인 탭을 안 띄움")
print("실패 %d개" % fail if fail else "모두 통과")
sys.exit(1 if fail else 0)
