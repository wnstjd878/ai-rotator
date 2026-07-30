"""
== 운영 맥락 ==
용도: 지피티(Codex) 계정 순환 로직을 실제로 돌려서 확인한다. 진짜 한도 상황은 만들 수
      없으므로 codex 호출·자격 교체·사용량 조회를 가짜로 바꿔 끼우고, 상태 파일도
      임시 폴더로 돌린다. 진짜 ~/.codex/auth.json 은 절대 건드리지 않는다.
실행: python test_codex_rotation.py   (모두 통과하면 종료코드 0)
검사 항목:
      1. 성공한 호출은 계정을 안 바꾼다
      2. 답변 본문에 한도 단어가 있어도 성공이면 안 바꾼다
      3. 한도 거절 + 서버 확증이면 다음 계정으로 바꿔 같은 요청을 다시 보낸다
      4. 한도 문구가 보여도 서버에 여유가 있으면 계정을 안 막는다 (확증 단계)
      5. 계정을 바꿔도 안 풀리는 한도(문맥 창·깊이·재시도·예산)는 무시한다
      6. 전 계정이 막히면 예외를 올리고 알림을 부른다
      7. 다른 프로세스가 이미 바꿨으면 또 바꾸지 않는다
      8. 로그인 문제는 계정을 안 바꾸고 멈춘다
      9. 해제 시각을 사용량 응답에서 읽는다 (문구보다 우선)
     10. 교체 전에 지금 자격을 되백업한다 / 계정이 다르면 되백업하지 않는다
     11. 공유 저장소에서 지피티와 클로드의 막힘이 섞이지 않는다
     12. 창 길이로 주간·세션을 가른다 (Pro=주간만, Plus=둘 다)
마지막 점검: 2026-07-28
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import pool_state as st  # noqa: E402
import codex_pool  # noqa: E402  (불러오는 순간 st.use("codex") 가 걸린다)
import codex_home  # noqa: E402
import codex_limit  # noqa: E402
import codex_usage  # noqa: E402

LIMIT_ERR = "stream error: You've hit your usage limit. Upgrade to Pro"
OK_OUT = "안녕하세요"
failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  통과: {name}")
    else:
        print(f"  실패: {name} {detail}")
        failures.append(name)


def usage(weekly=None, session=None, reached=False, reached_type=None,
          weekly_reset=None, session_reset=None):
    return {
        "weekly_pct": weekly, "session_pct": session,
        "weekly_reset": weekly_reset, "session_reset": session_reset,
        "limit_reached": reached, "reached_type": reached_type,
        "plan": "pro", "email": "test@example.com",
    }


class Fake:
    """가짜 codex 실행기. 계정 라벨별로 결과를 정해둔다."""

    def __init__(self, per_label, switch_ok=True, per_label_usage=None):
        self.per_label = per_label
        self.per_label_usage = per_label_usage or {}
        self.switch_ok = switch_ok
        self.calls = []
        self.switches = []
        self.notices = []

    def run(self, args, **kwargs):
        label = st.load_state().get("current")
        self.calls.append(label)
        rc, out, err = self.per_label.get(label, (1, "", "unknown account"))
        return subprocess.CompletedProcess(args, rc, out, err)

    def switch(self, label, current=None):
        self.switches.append(label)
        return self.switch_ok

    def notify(self, message):
        self.notices.append(message)

    # 사용량 조회 대역 — 라벨별로 정해둔 값을 돌려준다.
    # 자격 파일은 <...>/codex-<라벨>/auth.json 이라 부모 폴더 이름에서 라벨을 뽑는다.
    def get_usage(self, cred_path, **kw):
        label = Path(cred_path).parent.name.replace("codex-", "")
        return self.per_label_usage.get(label)

    def get_usage_for(self, cred_path, is_current=False):
        return self.get_usage(cred_path)

    def weekly_left(self, cred_path):
        u = self.get_usage(cred_path)
        if not u or u.get("weekly_pct") is None:
            return None
        return max(0.0, 100 - float(u["weekly_pct"]))

    # 판정·표기는 진짜 함수를 그대로 쓴다(문구가 갈라지면 시험 의미가 없다)
    which_limit = staticmethod(codex_usage.which_limit)
    pct_text = staticmethod(codex_usage.pct_text)
    fmt_reset_full = staticmethod(codex_usage.fmt_reset_full)
    left_phrase = staticmethod(codex_usage.left_phrase)
    summarize = staticmethod(codex_usage.summarize)


class FakeShared:
    """가짜 공유 저장소. 머신들이 같이 보는 한도 상태를 메모리로 흉내낸다."""

    def __init__(self):
        self.blocked = {}
        self.notified = {}
        self.claimed = set()

    def enabled(self):
        return True

    def get_blocked(self, now=None):
        return dict(self.blocked)

    def set_blocked(self, label, until, reason=""):
        self.blocked[label] = max(self.blocked.get(label, 0.0), float(until))
        return True

    def clear_blocked(self, label):
        self.blocked.pop(label, None)
        return True

    def claim_notify(self, reset_at, meta_key="rotator_notified"):
        key = str(round(float(reset_at))) if reset_at else "unknown"
        if self.notified.get(meta_key) == key:
            return False
        self.notified[meta_key] = key
        return True

    def claim_once(self, key):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True


def setup(tmp, labels, current, logged_in=None):
    """상태 파일·계정 목록·계정별 codex 폴더를 임시 폴더에 만든다.

    진짜 ~/.codex 는 절대 안 건드린다. codex_home 의 계정 목록 자리를 임시로 돌린다.
    logged_in 을 주면 그 라벨만 로그인이 있는 것으로 만든다(나머지는 폴더만).
    """
    tmp = Path(tmp)
    logged_in = labels if logged_in is None else logged_in
    accounts = []
    for label in labels:
        home = tmp / f"codex-{label}"
        home.mkdir(parents=True, exist_ok=True)
        if label in logged_in:
            (home / "auth.json").write_text(
                json.dumps({"tokens": {"account_id": f"acct-{label}",
                                       "access_token": f"tok-{label}"}}), encoding="utf-8")
        accounts.append({"label": label, "codex_home": str(home)})
    (tmp / "codex_accounts.json").write_text(json.dumps(accounts), encoding="utf-8")

    st.use("codex")
    st.STATE_FILE = tmp / "codex_state.json"
    st.LOCK_FILE = tmp / "codex_state.lock"
    st.ACCOUNTS_FILE = tmp / "codex_accounts.json"
    st.shared_state = None  # 케이스마다 공유는 끈 채로 시작
    codex_home.ACCOUNTS_FILE = tmp / "codex_accounts.json"
    codex_home.DEFAULT_HOME = tmp / "codex-default"
    st.SAVED_LABELS_FN = codex_home.logged_in_labels
    codex_pool.BASE = tmp
    codex_pool.LOG_FILE = tmp / "rotator.log"
    codex_pool.codex_usage = None  # 기본은 사용량 조회 없음(쓰는 케이스만 따로 끼운다)
    state = st.load_state()
    st.set_current(state, current, "시험 준비")
    st.save_state(state)


def install(fake, with_usage=False):
    codex_pool.subprocess.run = fake.run
    codex_pool._switch = fake.switch
    codex_pool.notify = fake.notify
    if with_usage:
        codex_pool.codex_usage = fake
        codex_limit.confirm_limit = lambda cred, is_current=True: _confirm(fake, cred)
    else:
        codex_limit.confirm_limit = lambda cred, is_current=True: None  # 못 읽음 = 문구 판정 유지


def _confirm(fake, cred):
    u = fake.get_usage(cred)
    if not u:
        return None
    if u.get("limit_reached") or u.get("reached_type"):
        return True
    for k in ("weekly_pct", "session_pct"):
        v = u.get(k)
        if v is not None and float(v) >= 99.0:
            return True
    return False


def case_success(tmp):
    print("\n[1] 성공한 호출은 계정을 안 바꾼다")
    setup(tmp, ["A", "B"], "A")
    fake = Fake({"A": (0, OK_OUT, "")})
    install(fake)
    proc = codex_pool.run(["codex", "exec", "안녕"])
    check("성공 결과를 그대로 돌려줌", proc.returncode == 0)
    check("교체 안 함", fake.switches == [], f"switches={fake.switches}")
    check("계정 그대로", st.load_state()["current"] == "A")


def case_false_positive_body(tmp):
    print("\n[2] 답변에 한도 단어가 있어도 성공이면 안 바꾼다")
    setup(tmp, ["A", "B"], "A")
    body = "사용자가 물어본 'usage limit reached' 라는 문구의 뜻은 다음과 같습니다..."
    fake = Fake({"A": (0, body, "")})
    install(fake)
    proc = codex_pool.run(["codex", "exec", "설명해줘"])
    check("성공으로 판정", proc.returncode == 0)
    check("교체 안 함", fake.switches == [], f"switches={fake.switches}")


def case_rotate_on_limit(tmp):
    print("\n[3] 한도 거절이면 다음 계정으로 바꿔 재시도한다")
    setup(tmp, ["A", "B"], "A")
    fake = Fake({"A": (1, "", LIMIT_ERR), "B": (0, OK_OUT, "")},
                per_label_usage={"A": usage(weekly=100, reached=True,
                                            weekly_reset=time.time() + 3 * 3600),
                                 "B": usage(weekly=10)})
    install(fake, with_usage=True)
    proc = codex_pool.run(["codex", "exec", "안녕"])
    check("최종 성공", proc.returncode == 0 and proc.stdout == OK_OUT)
    check("B 로 교체", fake.switches == ["B"], f"switches={fake.switches}")
    check("A 가 막힘 목록에", "A" in (st.load_state().get("blocked") or {}))
    check("두 계정 다 호출됨", fake.calls == ["A", "B"], f"calls={fake.calls}")
    check("전환 알림 나감", any("A → B" in m for m in fake.notices), f"notices={fake.notices}")


def case_limit_text_but_server_ok(tmp):
    print("\n[4] 한도 문구가 보여도 서버에 여유가 있으면 계정을 안 막는다")
    setup(tmp, ["A", "B"], "A")
    # 실행은 실패했고 꼬리에 한도 문구가 있지만, 서버 사용량은 16% 뿐이다
    fake = Fake({"A": (1, "", LIMIT_ERR)}, per_label_usage={"A": usage(weekly=16)})
    install(fake, with_usage=True)
    proc = codex_pool.run(["codex", "exec", "안녕"])
    check("실패를 그대로 돌려줌", proc.returncode == 1)
    check("교체 안 함", fake.switches == [], f"switches={fake.switches}")
    check("A 를 안 막음", "A" not in (st.load_state().get("blocked") or {}))
    check("일반 실패라 한 번 더 시도", fake.calls == ["A", "A"], f"calls={fake.calls}")


def case_ignored_limits(tmp):
    print("\n[5] 계정을 바꿔도 안 풀리는 한도는 무시한다")
    setup(tmp, ["A", "B"], "A")
    cases = [
        "context window exceeded",
        "Agent depth limit reached. Solve the task yourself.",
        "agent thread limit reached",
        "retry limit reached",
        "not enough retry quota is available for another attempt",
        "session_budget_exceeded",
        "recursion limit reached",
        "OS file watch limit reached.",
    ]
    for text in cases:
        v = codex_limit.classify(1, "", text)
        check(f"한도로 안 봄: {text[:34]}", v != "limit", f"판정={v}")
    # 진짜 한도 표기는 그대로 잡아야 한다
    for text in ["You've hit your usage limit.", "usage_limit_reached",
                 "Usage limit reached. You've reached your usage limit.",
                 "HTTP 429 Too Many Requests"]:
        v = codex_limit.classify(1, "", text)
        check(f"한도로 봄: {text[:34]}", v == "limit", f"판정={v}")


def case_all_exhausted(tmp):
    print("\n[6] 전 계정이 막히면 예외를 올리고 알린다")
    setup(tmp, ["A", "B"], "A")
    reset = time.time() + 2 * 3600
    fake = Fake({"A": (1, "", LIMIT_ERR), "B": (1, "", LIMIT_ERR)},
                per_label_usage={"A": usage(weekly=100, reached=True, weekly_reset=reset),
                                 "B": usage(weekly=100, reached=True, weekly_reset=reset)})
    install(fake, with_usage=True)
    raised = None
    try:
        codex_pool.run(["codex", "exec", "안녕"])
    except codex_pool.AllAccountsExhausted as e:
        raised = e
    check("소진 예외를 올림", raised is not None)
    check("해제 시각을 담음", raised is not None and raised.reset_at is not None)
    check("소진 알림 나감", any("전부 한도" in m for m in fake.notices), f"notices={fake.notices}")


def case_already_switched(tmp):
    print("\n[7] 다른 프로세스가 이미 바꿨으면 또 바꾸지 않는다")
    setup(tmp, ["A", "B"], "A")

    fake = Fake({"A": (1, "", LIMIT_ERR), "B": (0, OK_OUT, "")},
                per_label_usage={"A": usage(weekly=100, reached=True), "B": usage(weekly=5)})
    install(fake, with_usage=True)

    original_run = fake.run
    def run_then_steal(args, **kwargs):
        proc = original_run(args, **kwargs)
        if fake.calls == ["A"]:  # 첫 호출 직후 다른 프로세스가 B 로 바꿔놓은 상황
            s = st.load_state()
            st.set_current(s, "B", "다른 프로세스")
            st.save_state(s)
        return proc
    codex_pool.subprocess.run = run_then_steal

    proc = codex_pool.run(["codex", "exec", "안녕"])
    check("최종 성공", proc.returncode == 0)
    check("교체를 안 함", fake.switches == [], f"switches={fake.switches}")


def case_auth_stops(tmp):
    print("\n[8] 로그인 문제는 계정을 안 바꾸고 멈춘다")
    setup(tmp, ["A", "B"], "A")
    fake = Fake({"A": (1, "", "Not logged in. Please run codex login")})
    install(fake)
    proc = codex_pool.run(["codex", "exec", "안녕"])
    check("결과를 그대로 돌려줌", proc.returncode == 1)
    check("교체 안 함", fake.switches == [], f"switches={fake.switches}")
    check("계정을 안 막음", "A" not in (st.load_state().get("blocked") or {}))


def case_reset_from_usage(tmp):
    print("\n[9] 해제 시각을 사용량 응답에서 읽는다")
    setup(tmp, ["A", "B"], "A")
    reset = time.time() + 4 * 24 * 3600  # 주간 한도라 나흘 뒤
    fake = Fake({"A": (1, "", LIMIT_ERR), "B": (0, OK_OUT, "")},
                per_label_usage={"A": usage(weekly=100, reached=True, reached_type="weekly",
                                            weekly_reset=reset),
                                 "B": usage(weekly=3)})
    install(fake, with_usage=True)
    codex_pool.run(["codex", "exec", "안녕"])
    until = (st.load_state().get("blocked") or {}).get("A")
    check("사용량의 초기화 시각을 그대로 씀", until is not None and abs(until - reset) < 2,
          f"until={until} reset={reset}")
    # 문구에는 시각이 없다. 사용량이 없었다면 기본 5시간으로 떨어졌을 것이다.
    check("기본값(5시간)이 아님", until is not None and until > time.time() + 24 * 3600)


def case_folder_switch(tmp):
    print("\n[10] 계정마다 다른 폴더를 쓴다 (전환 = CODEX_HOME 교체, 자격 복사 없음)")
    setup(tmp, ["A", "B"], "A")
    seen = []

    fake = Fake({"A": (1, "", LIMIT_ERR), "B": (0, OK_OUT, "")},
                per_label_usage={"A": usage(weekly=100, reached=True), "B": usage(weekly=5)})
    install(fake, with_usage=True)

    real_run = fake.run
    def run_record(args, **kwargs):
        seen.append((kwargs.get("env") or {}).get("CODEX_HOME"))
        return real_run(args, **kwargs)
    codex_pool.subprocess.run = run_record

    a_home, b_home = str(codex_home.home_of("A")), str(codex_home.home_of("B"))
    codex_pool.run(["codex", "exec", "안녕"])
    check("첫 시도는 A 폴더로", seen and seen[0] == a_home, f"seen={seen}")
    check("전환 뒤에는 B 폴더로", len(seen) > 1 and seen[1] == b_home, f"seen={seen}")
    check("두 폴더가 서로 다름", a_home != b_home)

    # 자격 파일은 아무도 안 건드렸어야 한다(복사하면 서버가 이전 계정을 폐기한다)
    a_auth = json.loads((Path(a_home) / "auth.json").read_text(encoding="utf-8"))
    b_auth = json.loads((Path(b_home) / "auth.json").read_text(encoding="utf-8"))
    check("A 자격 그대로", a_auth["tokens"]["access_token"] == "tok-A")
    check("B 자격 그대로", b_auth["tokens"]["access_token"] == "tok-B")


def case_not_logged_in_skipped(tmp):
    print("\n[15] 로그인이 없는 계정은 후보에서 빠진다")
    setup(tmp, ["A", "B", "C"], "A", logged_in=["A", "C"])  # B 는 폴더만 있고 로그인 없음
    check("쓸 수 있는 계정은 A, C", st.saved_labels() == ["A", "C"], f"{st.saved_labels()}")

    fake = Fake({"A": (1, "", LIMIT_ERR), "C": (0, OK_OUT, "")},
                per_label_usage={"A": usage(weekly=100, reached=True), "C": usage(weekly=7)})
    install(fake, with_usage=True)
    proc = codex_pool.run(["codex", "exec", "안녕"])
    check("B 를 건너뛰고 C 로", fake.switches == ["C"], f"switches={fake.switches}")
    check("최종 성공", proc.returncode == 0)


def case_revoked_copy(tmp):
    print("\n[13] 갈아끼운 자격 사본이 죽었으면 잠깐 쉬게 하고 다음 계정으로 넘긴다")
    setup(tmp, ["A", "B", "C"], "A")
    revoked = ("ERROR: Your access token could not be refreshed because your refresh token "
               "was revoked. Please log out and sign in again.")
    fake = Fake({"A": (1, "", LIMIT_ERR), "B": (1, "", revoked), "C": (0, OK_OUT, "")},
                per_label_usage={"A": usage(weekly=100, reached=True), "B": usage(weekly=0),
                                 "C": usage(weekly=20)})
    install(fake, with_usage=True)
    proc = codex_pool.run(["codex", "exec", "안녕"])
    check("자격 문제로 판정", codex_limit.classify(1, "", revoked) == "auth")
    check("최종 성공", proc.returncode == 0, f"rc={proc.returncode}")
    check("B 를 건너뛰고 C 까지 감", fake.switches == ["B", "C"], f"switches={fake.switches}")
    blocked = st.load_state().get("blocked") or {}
    check("B 를 잠깐만 쉬게 함", "B" in blocked and blocked["B"] < time.time() + 3600,
          f"blocked={blocked}")


def case_revoked_first_call(tmp):
    print("\n[14] 처음부터 로그인이 안 된 환경이면 계정을 안 돌린다")
    setup(tmp, ["A", "B"], "A")
    revoked = "ERROR: refresh token was revoked. Please log out and sign in again."
    fake = Fake({"A": (1, "", revoked), "B": (0, OK_OUT, "")})
    install(fake)
    proc = codex_pool.run(["codex", "exec", "안녕"])
    check("결과를 그대로 돌려줌", proc.returncode == 1)
    check("교체 안 함", fake.switches == [], f"switches={fake.switches}")
    check("멀쩡한 계정을 안 막음", not (st.load_state().get("blocked") or {}))


def case_home_picks_usable(tmp):
    print("\n[16] 켜기 직전에 못 쓰는 계정이면 알아서 갈아탄다 (--home)")
    setup(tmp, ["A", "B"], "A")
    check("평소엔 지금 계정 폴더", codex_pool._print_home() == 0 and
          st.load_state()["current"] == "A")

    # A 가 막혀 있으면(다른 머신이 한도로 막아둔 경우 포함) B 를 골라야 한다
    with st.with_lock():
        s = st.prune_blocked(st.load_state())
        st.mark_blocked(s, "A", time.time() + 3600, "시험")
        st.save_state(s)
    rc = codex_pool._print_home()
    check("막힌 계정이면 다음 계정으로", rc == 0 and st.load_state()["current"] == "B",
          f"rc={rc} current={st.load_state().get('current')}")

    # 둘 다 막히면 실패를 알려 래퍼가 안내를 띄우게 한다
    with st.with_lock():
        s = st.prune_blocked(st.load_state())
        st.mark_blocked(s, "B", time.time() + 3600, "시험")
        st.save_state(s)
    check("전부 막히면 실패로 알림", codex_pool._print_home() == 1)


def case_shared_namespace(tmp):
    print("\n[11] 공유 저장소에서 지피티와 클로드의 막힘이 안 섞인다")
    setup(tmp, ["A", "B"], "A")
    shared = FakeShared()
    st.shared_state = shared
    until = time.time() + 3600

    state = st.load_state()
    st.mark_blocked(state, "A", until, "시험")
    st.save_state(state)
    check("공유에 codex 접두가 붙음", "codex:A" in shared.blocked,
          f"blocked={list(shared.blocked)}")

    # 클로드가 막아둔 라벨은 지피티 쪽에 안 섞여야 한다
    shared.blocked["B"] = until  # 접두 없음 = 클로드 계정 B
    merged = st.prune_blocked(st.load_state())
    check("지피티는 A 만 막힌 것으로 봄", list((merged.get("blocked") or {})) == ["A"],
          f"merged={merged.get('blocked')}")

    # 반대 방향: 클로드 쪽에서 보면 codex 라벨은 안 보여야 한다
    st.use("claude")
    st.STATE_FILE = Path(tmp) / "claude_state.json"
    st.LOCK_FILE = Path(tmp) / "claude_state.lock"
    st.ACCOUNTS_FILE = Path(tmp) / "codex_accounts.json"
    st.CREDS_DIR = Path(tmp) / "creds" / "codex"
    merged2 = st.prune_blocked(st.load_state())
    check("클로드는 B 만 막힌 것으로 봄", list((merged2.get("blocked") or {})) == ["B"],
          f"merged={merged2.get('blocked')}")
    st.use("codex")


def case_window_kind():
    print("\n[12] 창 길이로 주간·세션을 가른다")
    check("604800초는 주간", codex_usage._window_kind({"limit_window_seconds": 604800}) == "weekly")
    check("18000초는 세션", codex_usage._window_kind({"limit_window_seconds": 18000}) == "session")
    check("창 길이를 모르면 None", codex_usage._window_kind({"used_percent": 5}) is None)
    check("창이 없으면 None", codex_usage._window_kind(None) is None)

    # Pro: 주간 창 하나 / Plus: 5시간 + 주간
    u_pro = usage(weekly=16, weekly_reset=time.time() + 100)
    check("Pro 는 세션 값이 비어 있음", u_pro["session_pct"] is None)
    check("주간 90% 넘으면 주간으로 판정",
          codex_usage.which_limit(usage(weekly=96), threshold=95) == "weekly")
    check("여유 있으면 None", codex_usage.which_limit(usage(weekly=16, session=20)) is None)
    check("서버가 도달을 알리면 그대로 믿음",
          codex_usage.which_limit(usage(weekly=10, reached_type="weekly")) == "weekly")


def case_usage_namespace(tmp):
    print("\n[13] 공유 저장소에서 남은량·물려 있는 기계도 안 섞인다")
    import shared_state as ss

    sent = []    # 올린 줄
    table = []   # 읽을 때 칠판에 있다고 칠 줄

    def fake_request(method, path, body=None, params=None, headers=None):
        if method == "POST":
            sent.extend(body or [])
            return body
        return table

    real_request, real_enabled = ss._request, ss.enabled
    ss._request = fake_request
    ss.enabled = lambda: True
    try:
        ss.put_usage("A", {"weekly_pct": 9.0}, host="시험기계", provider="codex")
        ss.put_usage("A", {"weekly_pct": 50.0}, host="시험기계")  # 클로드(접두 없음)
        check("지피티 남은량 키에 접두가 붙음", sent[0]["key"] == "usage:codex:A",
              f"key={sent[0]['key']}")
        check("클로드 남은량 키는 예전 그대로", sent[1]["key"] == "usage:A",
              f"key={sent[1]['key']}")

        table[:] = list(sent)  # 둘 다 칠판에 있는 상태로 읽는다
        got_codex = ss.get_usage_all(provider="codex") or {}
        got_claude = ss.get_usage_all() or {}
        check("지피티는 자기 것만 읽음",
              list(got_codex) == ["A"] and got_codex["A"]["weekly_pct"] == 9.0,
              f"got={got_codex}")
        check("클로드는 자기 것만 읽음",
              list(got_claude) == ["A"] and got_claude["A"]["weekly_pct"] == 50.0,
              f"got={got_claude}")

        sent.clear()
        table.clear()
        ss.put_current("A", host="시험기계", provider="codex")
        ss.put_current("D", host="시험기계")  # 같은 기계가 클로드는 D 를 쓰는 상황
        check("물려 있는 기계 키도 갈린다",
              sent[0]["key"] == "current:codex:시험기계" and sent[1]["key"] == "current:시험기계",
              f"keys={[r['key'] for r in sent]}")

        table[:] = list(sent)
        cur_codex = ss.get_current_all(provider="codex") or {}
        cur_claude = ss.get_current_all() or {}
        check("지피티 쪽은 A 만 보임", list(cur_codex) == ["A"], f"got={cur_codex}")
        check("클로드 쪽은 D 만 보임", list(cur_claude) == ["D"], f"got={cur_claude}")
    finally:
        ss._request, ss.enabled = real_request, real_enabled


def main():
    real_run = subprocess.run
    cases = [case_success, case_false_positive_body, case_rotate_on_limit,
             case_limit_text_but_server_ok, case_ignored_limits, case_all_exhausted,
             case_already_switched, case_auth_stops, case_reset_from_usage,
             case_folder_switch, case_revoked_copy, case_revoked_first_call,
             case_not_logged_in_skipped, case_home_picks_usable, case_shared_namespace,
             case_usage_namespace]
    for fn in cases:
        with tempfile.TemporaryDirectory() as tmp:
            fn(tmp)
    case_window_kind()
    codex_pool.subprocess.run = real_run

    print("\n" + "=" * 46)
    if failures:
        print(f"실패 {len(failures)}건: " + ", ".join(failures))
        return 1
    print("전부 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
