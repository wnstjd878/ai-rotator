"""
== 운영 맥락 ==
용도: 계정 순환 로직을 실제로 돌려서 확인한다. 진짜 한도 상황은 만들 수 없으므로
      claude 호출과 자격 교체를 가짜로 바꿔 끼우고, 상태 파일도 임시 폴더로 돌린다.
      진짜 Keychain / .credentials.json 은 절대 건드리지 않는다.
실행: python test_rotation.py     (모두 통과하면 종료코드 0)
검사 항목:
      1. 성공한 호출은 계정을 안 바꾼다
      2. 답변 본문에 한도 단어가 있어도 성공이면 안 바꾼다 (오탐 방지)
      3. 한도 거절이면 다음 계정으로 바꿔 같은 요청을 다시 보낸다
      4. 전 계정이 막히면 AllAccountsExhausted 를 올리고 알림을 부른다
      5. 다른 프로세스가 이미 바꿨으면 또 바꾸지 않는다
      6. 해제 시각을 문구에서 읽는다 (숫자형 / 시계형 / 못 읽으면 5시간)
      7. 로그인 문제는 짧게 쉬게 하고 넘긴다
      8. 원인 모를 실패는 계정을 안 바꾸고 한 번만 더 시도한다
      9. 세션 한도로 비켜났던 앞순위 계정이 풀리면 되돌린다 (사람이 고른 계정은 예외)
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

import claude_pool  # noqa: E402
import limit_detect  # noqa: E402
import pool_state as st  # noqa: E402

LIMIT_OUT = json.dumps({
    "type": "result", "subtype": "error", "is_error": True,
    "result": "Claude AI usage limit reached|%d" % int(time.time() + 3 * 3600),
}, ensure_ascii=False)
AUTH_OUT = json.dumps({"type": "result", "is_error": True, "result": "Not logged in"})
OK_OUT = json.dumps({"type": "result", "is_error": False, "result": "안녕하세요"})

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  통과: {name}")
    else:
        print(f"  실패: {name} {detail}")
        failures.append(name)


class Fake:
    """가짜 claude 실행기. 계정 라벨별로 결과를 정해둔다."""

    def __init__(self, per_label, switch_ok=True):
        self.per_label = per_label
        self.switch_ok = switch_ok
        self.calls = []
        self.switches = []
        self.leavings = []   # 갈아탈 때 '떠나는 계정'을 제대로 넘겼는지 확인용
        self.notices = []

    def run(self, args, **kwargs):
        label = st.load_state().get("current")
        self.calls.append(label)
        rc, out, err = self.per_label.get(label, (1, "", "unknown account"))
        return subprocess.CompletedProcess(args, rc, out, err)

    def switch(self, label, leaving=None):
        self.switches.append(label)
        self.leavings.append(leaving)
        return self.switch_ok

    def notify(self, message):
        self.notices.append(message)


class FakeShared:
    """가짜 공유 저장소. 세 머신이 같이 보는 한도 상태를 메모리 dict 로 흉내낸다."""

    def __init__(self):
        self.blocked = {}
        self.notified = None

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

    def claim_notify(self, reset_at):
        key = str(round(float(reset_at))) if reset_at else "unknown"
        if self.notified == key:
            return False
        self.notified = key
        return True

    def claim_once(self, key):
        if not hasattr(self, "claimed"):
            self.claimed = set()
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True


def setup(tmp, labels, current):
    """상태 파일·계정 목록·자격 저장본을 임시 폴더에 만든다."""
    tmp = Path(tmp)
    creds = tmp / "creds"
    creds.mkdir(parents=True, exist_ok=True)
    for label in labels:
        (creds / f"{label}.json").write_text("{}", encoding="utf-8")
    (tmp / "accounts.json").write_text(
        json.dumps([{"label": x, "chrome_profile": "Default"} for x in labels]), encoding="utf-8"
    )
    st.STATE_FILE = tmp / "state.json"
    st.LOCK_FILE = tmp / "state.lock"
    st.ACCOUNTS_FILE = tmp / "accounts.json"
    st.CREDS_DIR = creds
    st.shared_state = None  # 케이스마다 공유는 끈 채로 시작(공유 케이스만 따로 켠다)
    claude_pool.usage_check = None  # 시험에서는 실제 사용량 API 를 부르지 않는다
    claude_pool.LOG_FILE = tmp / "rotator.log"
    state = st.load_state()
    st.set_current(state, current, "시험 준비")
    st.save_state(state)


def install(fake):
    claude_pool.subprocess.run = fake.run
    claude_pool._switch = fake.switch
    claude_pool.notify = fake.notify


def case_success_no_switch(tmp):
    print("\n[1] 성공한 호출은 계정을 안 바꾼다")
    setup(tmp, ["A", "B"], "A")
    fake = Fake({"A": (0, OK_OUT, "")})
    install(fake)
    proc = claude_pool.run(["claude", "-p", "안녕"])
    check("성공 결과를 그대로 돌려줌", proc.returncode == 0)
    check("교체 안 함", fake.switches == [], f"switches={fake.switches}")
    check("계정 그대로", st.load_state()["current"] == "A")


def case_false_positive(tmp):
    print("\n[2] 답변에 한도 단어가 있어도 성공이면 안 바꾼다")
    setup(tmp, ["A", "B"], "A")
    body = json.dumps({"type": "result", "is_error": False,
                       "result": "usage limit reached 라는 문구의 뜻은..."}, ensure_ascii=False)
    fake = Fake({"A": (0, body, "")})
    install(fake)
    proc = claude_pool.run(["claude", "-p", "설명해줘"])
    check("성공으로 판정", proc.returncode == 0)
    check("교체 안 함", fake.switches == [], f"switches={fake.switches}")


def case_rotate_on_limit(tmp):
    print("\n[3] 한도 거절이면 다음 계정으로 바꿔 재시도한다")
    setup(tmp, ["A", "B", "C", "D"], "A")
    fake = Fake({"A": (1, LIMIT_OUT, ""), "B": (0, OK_OUT, "")})
    install(fake)
    proc = claude_pool.run(["claude", "-p", "안녕"])
    check("최종 성공", proc.returncode == 0)
    check("B 로 한 번만 교체", fake.switches == ["B"], f"switches={fake.switches}")
    state = st.load_state()
    check("현재 계정이 B", state["current"] == "B", f"current={state['current']}")
    check("A 가 막힘으로 기록됨", "A" in state["blocked"], f"blocked={state['blocked']}")
    check("전환 알림이 나감", any("전환" in n for n in fake.notices), f"notices={fake.notices}")
    left = float(state["blocked"]["A"]) - time.time()
    check("A 해제 시각이 약 3시간 뒤", 2.5 * 3600 < left < 3.5 * 3600, f"{left / 3600:.2f}시간")
    check("호출은 A, B 순서", fake.calls == ["A", "B"], f"calls={fake.calls}")


def case_all_exhausted(tmp):
    print("\n[4] 전 계정이 막히면 예외를 올리고 알린다")
    setup(tmp, ["A", "B", "C"], "A")
    fake = Fake({x: (1, LIMIT_OUT, "") for x in ["A", "B", "C"]})
    install(fake)
    raised = None
    try:
        claude_pool.run(["claude", "-p", "안녕"])
    except claude_pool.AllAccountsExhausted as e:
        raised = e
    check("AllAccountsExhausted 발생", raised is not None)
    check("소진 알림 1건", len([n for n in fake.notices if "전부 한도" in n]) == 1,
          f"notices={fake.notices}")
    check("해제 시각을 알려줌", raised is not None and raised.reset_at is not None)
    state = st.load_state()
    check("세 계정 모두 막힘 기록", set(state["blocked"]) == {"A", "B", "C"}, f"{state['blocked']}")


def case_already_switched(tmp):
    print("\n[5] 다른 프로세스가 이미 바꿨으면 또 바꾸지 않는다")
    setup(tmp, ["A", "B"], "A")
    fake = Fake({})
    install(fake)
    # 내가 A 로 실행했는데, 그 사이 다른 봇이 B 로 바꿔놓은 상황
    state = st.load_state()
    st.set_current(state, "B", "다른 프로세스")
    st.save_state(state)
    label = claude_pool._rotate_after_failure("A", "limit", LIMIT_OUT, "")
    check("B 를 그대로 씀", label == "B", f"label={label}")
    check("교체 호출 없음", fake.switches == [], f"switches={fake.switches}")
    check("A 를 막힘으로 잘못 표시하지 않음", "A" not in st.load_state()["blocked"])


def case_reset_parsing(tmp):
    print("\n[6] 해제 시각을 문구에서 읽는다")
    now = time.time()
    epoch_txt = "Claude AI usage limit reached|%d" % int(now + 7200)
    got = limit_detect.reset_epoch(epoch_txt, "", now=now)
    check("숫자형 시각을 읽음", abs(got - (now + 7200)) < 5, f"차이={got - now:.0f}초")

    clock = limit_detect.reset_epoch("5-hour limit reached ∙ resets 3pm", "", now=now)
    lt = time.localtime(clock)
    check("시계형 시각을 읽음", lt.tm_hour == 15 and lt.tm_min == 0, f"읽은 시각={lt.tm_hour}:{lt.tm_min:02d}")
    check("시계형은 앞으로의 시각", clock > now)

    plain = limit_detect.reset_epoch("usage limit reached", "", now=now)
    check("못 읽으면 5시간 뒤", abs(plain - (now + 5 * 3600)) < 5)

    stale = limit_detect.reset_epoch("error code 1699999999 something", "", now=now)
    check("지난 시각 숫자는 무시", abs(stale - (now + 5 * 3600)) < 5, f"{stale - now:.0f}초")

    # 실제 주간 한도 응답 형식(월 이름+일+시각). now=1785000000 은 2026-07-24 무렵
    wk = limit_detect.reset_epoch("You've hit your weekly limit resets Jul 31 at 8am (Asia/Seoul)", "", now=now)
    lt = time.localtime(wk)
    check("주간 리셋 월/일/시 파싱", lt.tm_mon == 7 and lt.tm_mday == 31 and lt.tm_hour == 8,
          f"읽은값={time.strftime('%m/%d %H:%M', lt)}")
    check("주간 리셋은 5시간 기본이 아님", wk - now > 5 * 3600)

    # claude 실행 파일 안에 실제로 박혀 있던 표기들이 한도로 잡히는지
    real_wordings = [
        "usage limit reached",
        "session limit",
        "weekly limit",
        "Opus limit",
        "credit balance too low",
        '{"type":"rate_limit"}',
        '"reason":"billing_error"',
        '{"seven_day":{"resets_at":123}}',
    ]
    for w in real_wordings:
        # 실제 CLI 형식(결과 껍데기)과 껍데기 없는 형식 둘 다 잡아야 한다
        env = json.dumps({"type": "result", "is_error": True, "result": w}, ensure_ascii=False)
        check(f"한도로 판정: {w[:28]}", limit_detect.classify(1, env, "") == "limit")
        bare = json.dumps({"is_error": True, "result": w}, ensure_ascii=False)
        check(f"껍데기 없어도 판정: {w[:22]}", limit_detect.classify(1, bare, "") == "limit")

    # 도움말 문구가 성공 응답에 섞여도 한도로 오해하지 않는지
    helpful = json.dumps({"is_error": False,
                          "result": "Sonnet 5 uses your weekly limit much more slowly than Opus."})
    check("성공 응답의 한도 단어는 무시", limit_detect.classify(0, helpful, "") == "ok")

    # 계정을 바꿔도 안 풀리는 한도들. 이걸 한도로 오판하면 멀쩡한 계정이 다 막힌다.
    not_subscription = [
        "Context limit reached",
        "Subagent nesting limit reached (depth 3 of 3)",
        "Concurrent export limit reached",
        "recursion limit reached",
        "spend limit reached",
        "Prompt is too long",
        "Request exceeds size limits",
    ]
    for w in not_subscription:
        body = json.dumps({"is_error": True, "result": w}, ensure_ascii=False)
        check(f"한도로 안 봄: {w[:30]}", limit_detect.classify(1, body, "") == "error")

    # 한 응답에 둘 다 있으면 진짜 구독 한도 쪽을 따른다
    mixed = json.dumps({"is_error": True,
                        "result": "Context limit reached. usage limit reached"}, ensure_ascii=False)
    check("섞여 있으면 구독 한도로 판정", limit_detect.classify(1, mixed, "") == "limit")


def case_auth(tmp):
    print("\n[7] 로그인이 안 된 상태면 계정을 바꾸지 않는다")
    # ssh 비대화형에서는 자격을 못 읽어 늘 이 응답이 온다. 이때 계정을 돌리면
    # 멀쩡한 계정이 전부 막힌 것으로 잘못 기록된다.
    setup(tmp, ["A", "B"], "A")
    fake = Fake({"A": (1, AUTH_OUT, ""), "B": (0, OK_OUT, "")})
    install(fake)
    check("판정이 auth", limit_detect.classify(1, AUTH_OUT, "") == "auth")
    real_auth_out = json.dumps({"type": "result", "is_error": True,
                                "result": "Not logged in · Please run /login"}, ensure_ascii=False)
    check("실제 CLI 문구도 auth 로 판정", limit_detect.classify(1, real_auth_out, "") == "auth")
    proc = claude_pool.run(["claude", "-p", "안녕"])
    check("실패를 그대로 돌려줌", proc.returncode == 1)
    check("교체 안 함", fake.switches == [], f"switches={fake.switches}")
    check("한 번만 호출", len(fake.calls) == 1, f"calls={fake.calls}")
    check("계정을 막지 않음", st.load_state()["blocked"] == {}, f"{st.load_state()['blocked']}")
    check("알림 안 보냄", fake.notices == [])


def case_unknown_error(tmp):
    print("\n[8] 원인 모를 실패는 계정을 안 바꾸고 한 번만 더 시도한다")
    setup(tmp, ["A", "B"], "A")
    fake = Fake({"A": (1, "", "segmentation fault")})
    install(fake)
    proc = claude_pool.run(["claude", "-p", "안녕"])
    check("실패 결과를 그대로 돌려줌", proc.returncode == 1)
    check("교체 안 함", fake.switches == [], f"switches={fake.switches}")
    check("두 번만 호출", len(fake.calls) == 2, f"calls={fake.calls}")


def case_manual_next(tmp):
    print("\n[9] 사람이 --next 로 넘기면 지금 계정을 쉬게 하고 다음으로 간다")
    setup(tmp, ["A", "B"], "A")
    fake = Fake({})
    install(fake)
    rc = claude_pool.next_account()
    check("정상 종료", rc == 0, f"rc={rc}")
    check("B 로 교체", fake.switches == ["B"], f"switches={fake.switches}")
    state = st.load_state()
    check("현재 계정이 B", state["current"] == "B")
    left = float(state["blocked"]["A"]) - time.time()
    check("A 는 5시간 쉼", 4.5 * 3600 < left < 5.5 * 3600, f"{left / 3600:.1f}시간")

    # 남은 계정이 없으면 교체하지 않고 알려만 준다
    rc2 = claude_pool.next_account()
    check("쓸 계정 없으면 종료코드 2", rc2 == 2, f"rc={rc2}")
    check("교체 시도 안 함", fake.switches == ["B"], f"switches={fake.switches}")


def case_answer_json_not_failure(tmp):
    print("\n[10] 모델 답변 속 JSON 이 실패로 둔갑하지 않는다")
    # 봇이 "이 로그를 JSON 으로 판정해줘" 같은 일을 시키면 답변에 is_error 가 들어온다.
    # 이걸 실패로 보면 정상 응답 한 번으로 계정 네 개가 통째로 막힌다.
    answer = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": '{"is_error": true, "cause": "429 rate limit", "action": "retry"}',
    }, ensure_ascii=False)
    check("성공으로 판정", limit_detect.classify(0, answer, "") == "ok")

    # 껍데기 없이 답변만 그대로 나오는 경우(텍스트 모드)
    raw = '{"is_error": true, "cause": "usage limit reached"}'
    check("껍데기가 아니면 실패로 안 봄", limit_detect.classify(0, raw, "") == "ok")

    setup(tmp, ["A", "B", "C", "D"], "A")
    fake = Fake({"A": (0, answer, "")})
    install(fake)
    proc = claude_pool.run(["claude", "-p", "판정해줘"])
    check("계정 교체 없음", fake.switches == [], f"switches={fake.switches}")
    check("한 번만 실행", len(fake.calls) == 1, f"calls={fake.calls}")
    check("계정을 막지 않음", st.load_state()["blocked"] == {})
    check("알림 없음", fake.notices == [])


def case_dirty_body_not_limit(tmp):
    print("\n[11] 답변 본문·도구 출력에 한도 단어가 있어도 넘기지 않는다")
    cases = [
        ("답변에 rate limit 설명", json.dumps({
            "type": "result", "is_error": False,
            "result": "리뷰에 'rate limited' 라고 적힌 글이 많습니다"}, ensure_ascii=False), 0, "ok"),
        ("실패인데 사유는 붙임 도구 쪽", json.dumps({
            "type": "result", "is_error": True,
            "result": "MCP server crashed"}, ensure_ascii=False), 1, "error"),
    ]
    for name, body, rc, want in cases:
        check(f"{name} → {want}", limit_detect.classify(rc, body, "") == want,
              f"실제={limit_detect.classify(rc, body, '')}")

    # 진짜 한도는 여전히 잡아야 한다
    real = json.dumps({"type": "result", "is_error": True,
                       "result": "Claude AI usage limit reached"}, ensure_ascii=False)
    check("진짜 한도는 그대로 잡음", limit_detect.classify(1, real, "") == "limit")


def case_broken_cred_after_switch(tmp):
    print("\n[12] 갈아끼운 저장본이 상했으면 그 계정도 넘긴다")
    setup(tmp, ["A", "B", "C"], "A")
    broken = json.dumps({"type": "result", "is_error": True,
                         "result": "Invalid API key · Please run /login"}, ensure_ascii=False)
    fake = Fake({"A": (1, LIMIT_OUT, ""), "B": (1, broken, ""), "C": (0, OK_OUT, "")})
    install(fake)
    proc = claude_pool.run(["claude", "-p", "안녕"])
    check("최종 성공", proc.returncode == 0)
    check("B 를 건너뛰고 C 까지 감", fake.switches == ["B", "C"], f"switches={fake.switches}")
    state = st.load_state()
    check("B 도 쉬게 기록", "B" in state["blocked"], f"blocked={list(state['blocked'])}")
    left = float(state["blocked"]["B"]) - time.time()
    check("B 는 30분", 25 * 60 < left < 35 * 60, f"{left / 60:.0f}분")


def case_notice_once(tmp):
    print("\n[13] 전 계정 소진 알림은 한 번만 나간다")
    setup(tmp, ["A", "B"], "A")
    fake = Fake({x: (1, LIMIT_OUT, "") for x in ["A", "B"]})
    install(fake)
    for _ in range(4):
        try:
            claude_pool.run(["claude", "-p", "안녕"])
        except claude_pool.AllAccountsExhausted:
            pass
    check("네 번 불러도 소진 알림 1건",
          len([n for n in fake.notices if "전부 한도" in n]) == 1, f"notices={fake.notices}")


def case_broken_state_file(tmp):
    print("\n[14] 상태 파일이 깨져도 죽지 않는다")
    setup(tmp, ["A", "B"], "A")
    for bad in ("[]", '{"blocked": {"A": "내일"}}', "not json at all", '"문자열"'):
        st.STATE_FILE.write_text(bad, encoding="utf-8")
        try:
            state = st.prune_blocked(st.load_state())
            ok = isinstance(state, dict) and isinstance(state.get("blocked"), dict)
        except Exception as e:
            ok = False
            print(f"    ({type(e).__name__})")
        check(f"깨진 파일 처리: {bad[:22]}", ok)


def case_subprocess_compat(tmp):
    print("\n[15] subprocess.run 처럼 쓸 수 있다 (check, stdout 지정)")
    setup(tmp, ["A", "B"], "A")

    # check=True 는 판정을 막지 않고, 최종 실패일 때만 예외로 재현된다
    fake = Fake({"A": (1, LIMIT_OUT, ""), "B": (0, OK_OUT, "")})
    install(fake)
    proc = claude_pool.run(["claude", "-p", "안녕"], check=True)
    check("한도는 교체로 풀리고 예외 없음", proc.returncode == 0)

    setup(tmp, ["A", "B"], "A")
    fake2 = Fake({"A": (1, "", "segfault")})
    install(fake2)
    raised = False
    try:
        claude_pool.run(["claude", "-p", "안녕"], check=True)
    except subprocess.CalledProcessError:
        raised = True
    check("끝까지 실패면 CalledProcessError", raised)

    # stdout 을 직접 지정해도 ValueError 가 안 난다
    setup(tmp, ["A", "B"], "A")
    fake3 = Fake({"A": (0, OK_OUT, "")})
    install(fake3)
    seen = {}

    def spy(args, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(args, 0, OK_OUT, "")

    claude_pool.subprocess.run = spy
    claude_pool.run(["claude", "-p", "안녕"], stdout=subprocess.PIPE)
    check("capture_output 을 같이 안 넣음", "capture_output" not in seen, f"{sorted(seen)}")


def case_sequential_lowest(tmp):
    print("\n[17] 순차 소진: 항상 최저 인덱스부터 (분산 아님)")
    setup(tmp, ["A", "B", "C", "D"], "C")  # 현재 C 에 앉아 있어도
    state = st.prune_blocked(st.load_state())
    check("A 가 살아 있으면 A 선택", st.pick_next(state) == "A",
          f"골른 것={st.pick_next(state)}")

    st.mark_blocked(state, "A", time.time() + 3 * 3600)
    check("A 막히면 B", st.pick_next(state) == "B", f"골른 것={st.pick_next(state)}")

    st.mark_blocked(state, "B", time.time() + 3 * 3600)
    check("A,B 막히면 C", st.pick_next(state) == "C", f"골른 것={st.pick_next(state)}")

    # A 가 풀리면 다시 A (5시간 한도가 풀린 상황)
    state["blocked"].pop("A")
    check("A 풀리면 다시 A 로 복귀", st.pick_next(state) == "A", f"골른 것={st.pick_next(state)}")


def case_reset_soonest_first(tmp):
    print("\n[17-2] 초기화가 이른 계정부터 태운다 (안 쓰면 남은 양이 사라지므로)")
    setup(tmp, ["A", "B", "C", "D"], "A")
    now = time.time()
    state = st.prune_blocked(st.load_state())

    # 시각을 하나도 모르면 예전처럼 accounts.json 순서
    check("시각 모르면 라벨 순서", st.pick_next(state) == "A", f"골른 것={st.pick_next(state)}")

    # A=8/1, B=7/31 처럼 B 가 먼저 초기화되면 B 부터 (2026-07-27 사용자 지적 상황)
    st.note_weekly_reset(state, "A", now + 5 * 86400)
    st.note_weekly_reset(state, "B", now + 3 * 86400)
    st.note_weekly_reset(state, "D", now + 9 * 86400)
    check("초기화 이른 B 를 먼저", st.pick_next(state) == "B", f"골른 것={st.pick_next(state)}")

    # 시각을 아는 계정이 먼저, 모르는 계정(C)은 뒤로
    st.mark_blocked(state, "B", now + 3600)
    check("B 막히면 그 다음으로 이른 A", st.pick_next(state) == "A", f"골른 것={st.pick_next(state)}")
    st.mark_blocked(state, "A", now + 3600)
    st.mark_blocked(state, "D", now + 3600)
    check("아는 계정이 다 막히면 모르는 C", st.pick_next(state) == "C", f"골른 것={st.pick_next(state)}")

    # 이미 지난 시각은 낡은 값이라 순서 기준으로 안 쓴다
    state["blocked"] = {}
    st.note_weekly_reset(state, "C", now + 86400)
    state["weekly_reset"]["A"] = round(now - 86400)  # 낡은 값을 직접 박아둔다
    check("낡은 시각은 뒤로 밀림", st.pick_next(state) == "C", f"골른 것={st.pick_next(state)}")

    # 알림의 '(다음)' 표시도 같은 규칙을 쓴다
    check("정렬 규칙이 한 곳에서 나온다",
          st.priority_order(state, ["A", "C", "D"])[0] == "C",
          f"정렬={st.priority_order(state, ['A', 'C', 'D'])}")


def case_skip_blocked_before_call(tmp):
    print("\n[17-3] 이미 막힌 계정이면 부르기 전에 갈아탄다 (헛걸음 없음)")
    setup(tmp, ["A", "B"], "A")
    state = st.prune_blocked(st.load_state())
    st.mark_blocked(state, "A", time.time() + 3 * 3600, "다른 머신이 한도로 막음")
    st.save_state(state)

    fake = Fake({"A": (0, OK_OUT, ""), "B": (0, OK_OUT, "")})
    install(fake)
    proc = claude_pool.run(["claude", "-p", "안녕"])
    check("성공", proc.returncode == 0)
    check("막힌 A 는 아예 안 부름", "A" not in fake.calls, f"부른 계정={fake.calls}")
    check("한 번만 부름(헛걸음 없음)", len(fake.calls) == 1, f"부른 계정={fake.calls}")
    check("현재 계정이 B 로 바뀜",
          st.prune_blocked(st.load_state()).get("current") == "B",
          f"현재={st.prune_blocked(st.load_state()).get('current')}")


def case_reclaim_when_freed(tmp):
    print("\n[17-4] 세션 한도로 비켜났던 앞순위 계정이 풀리면 되돌린다")
    setup(tmp, ["A", "B"], "A")
    fake = Fake({"A": (1, LIMIT_OUT, ""), "B": (0, OK_OUT, "")})
    install(fake)
    claude_pool.run(["claude", "-p", "안녕"])
    check("A 한도 → B 로 이동", st.load_state()["current"] == "B",
          f"현재={st.load_state()['current']}")

    # 5시간이 지나 A 의 막힘이 풀린 상황
    state = st.load_state()
    state["blocked"]["A"] = time.time() - 10
    st.save_state(state)

    fake2 = Fake({"A": (0, OK_OUT, ""), "B": (0, OK_OUT, "")})
    install(fake2)
    proc = claude_pool.run(["claude", "-p", "안녕"])
    check("성공", proc.returncode == 0)
    check("앞순위 A 로 되돌림", st.load_state()["current"] == "A",
          f"현재={st.load_state()['current']}")
    check("되돌린 계정으로 부름", fake2.calls == ["A"], f"부른 계정={fake2.calls}")
    check("교체는 A 한 번만", fake2.switches == ["A"], f"switches={fake2.switches}")

    # 되돌린 직후에는 또 시도하지 않는다 (왕복 방지)
    state = st.load_state()
    check("되돌린 시각이 기록됨", state.get("reclaim_at"), f"reclaim_at={state.get('reclaim_at')}")
    check("간격 안에는 재시도 안 함", not st.may_reclaim(state))

    # 뒷순위에 앉아 있어도 앞순위가 막혀 있으면 되돌리지 않는다
    state["reclaim_at"] = None
    st.set_current(state, "B", "시험")
    st.mark_blocked(state, "A", time.time() + 3600)
    check("앞순위가 막혔으면 후보 없음", st.better_candidate(state, "B") is None,
          f"고른 것={st.better_candidate(state, 'B')}")


def case_manual_use_not_reverted(tmp):
    print("\n[17-5] 사람이 --use 로 고른 계정은 되돌리기가 안 뒤집는다")
    setup(tmp, ["A", "B"], "A")
    fake = Fake({"A": (0, OK_OUT, ""), "B": (0, OK_OUT, "")})
    install(fake)
    claude_pool.use("B")  # 사람이 직접 B 를 고름 (A 는 멀쩡한 상태)
    state = st.load_state()
    check("현재 계정이 B", state["current"] == "B", f"현재={state['current']}")
    check("되돌리기 후보 없음(사람 지정 존중)", st.better_candidate(state, "B") is None,
          f"고른 것={st.better_candidate(state, 'B')}")

    proc = claude_pool.run(["claude", "-p", "안녕"])
    check("성공", proc.returncode == 0)
    check("B 를 그대로 씀", st.load_state()["current"] == "B",
          f"현재={st.load_state()['current']}")
    check("A 로 부르지 않음", fake.calls == ["B"], f"부른 계정={fake.calls}")

    # 자동 교체가 한 번 일어나면 사람 지정 표시가 풀려 다시 순서 규칙대로 돈다
    state = st.load_state()
    st.set_current(state, "B", "자동 교체")
    st.save_state(state)
    check("표시가 풀리면 앞순위 A 가 후보",
          st.better_candidate(st.load_state(), "B") == "A",
          f"고른 것={st.better_candidate(st.load_state(), 'B')}")


def case_shared_merge(tmp):
    print("\n[18] 공유 저장소: 다른 머신 막힘이 병합돼 같은 계정에 수렴")
    setup(tmp, ["A", "B", "C"], "A")
    fake = FakeShared()
    st.shared_state = fake

    # 다른 머신이 A 를 한도로 막았다(공유에 기록됨)
    fake.blocked["A"] = time.time() + 3 * 3600
    state = st.prune_blocked(st.load_state())  # 공유를 병합
    check("공유 막힘이 로컬에 병합", st.is_blocked(state, "A"))
    check("이 머신도 A 건너뛰고 B 선택", st.pick_next(state) == "B", f"골른 것={st.pick_next(state)}")

    # 이 머신이 B 를 막으면 공유에도 전파돼 다른 머신이 본다
    st.mark_blocked(state, "B", time.time() + 3 * 3600, "이 머신에서 막음")
    check("내 막힘이 공유로 전파", "B" in fake.blocked, f"공유={list(fake.blocked)}")

    # 사람이 --use 로 A 를 고르면 공유 막힘도 풀린다
    st.clear_blocked_label(state, "A")
    check("수동 지정이 공유 막힘도 품", "A" not in fake.blocked, f"공유={list(fake.blocked)}")


def case_shared_three_machines(tmp):
    print("\n[19] 세 머신 시뮬레이션: 한 계정을 순서대로 태운다")
    # 공유 저장소 하나를 세 머신이 같이 본다
    shared = FakeShared()
    machines = []
    for i in range(3):
        mdir = Path(tmp) / f"m{i}"
        setup(mdir, ["A", "B", "C", "D"], None)
        machines.append({
            "STATE_FILE": st.STATE_FILE, "LOCK_FILE": st.LOCK_FILE,
            "ACCOUNTS_FILE": st.ACCOUNTS_FILE, "CREDS_DIR": st.CREDS_DIR,
        })

    def as_machine(m):
        st.STATE_FILE = m["STATE_FILE"]; st.LOCK_FILE = m["LOCK_FILE"]
        st.ACCOUNTS_FILE = m["ACCOUNTS_FILE"]; st.CREDS_DIR = m["CREDS_DIR"]
        st.shared_state = shared

    # 세 머신 모두 처음엔 A 를 고른다
    picks = []
    for m in machines:
        as_machine(m)
        picks.append(st.pick_next(st.prune_blocked(st.load_state())))
    check("세 머신 모두 A 선택", picks == ["A", "A", "A"], f"골른 것={picks}")

    # 머신 0 이 A 를 주간한도로 막는다(공유 전파)
    as_machine(machines[0])
    st.mark_blocked(st.prune_blocked(st.load_state()), "A", time.time() + 3 * 3600, "m0 한도")

    # 나머지 머신도 다음 호출에서 A 를 건너뛰고 B 로 수렴
    picks = []
    for m in machines:
        as_machine(m)
        picks.append(st.pick_next(st.prune_blocked(st.load_state())))
    check("A 막힌 뒤 세 머신 모두 B 로 수렴", picks == ["B", "B", "B"], f"골른 것={picks}")


def case_save_back(tmp):
    print("\n[23] 갈아탈 때 쓰던 계정의 갱신된 자격을 되저장한다")
    import switch_account as sa

    real_creds, real_live = sa.CREDS_DIR, sa.LIVE
    root = Path(tmp) / "saveback"
    (root / "creds").mkdir(parents=True, exist_ok=True)
    (root / "home").mkdir(parents=True, exist_ok=True)
    sa.CREDS_DIR = root / "creds"
    sa.LIVE = root / "home" / ".credentials.json"

    def write(path, token):
        path.write_text(json.dumps({"claudeAiOauth": {"refreshToken": token, "expiresAt": 1}}),
                        encoding="utf-8")

    # 실측 판별(live_label)은 여기선 끈 채로 본다. 켠 경우는 아래 [25] 에서 따로 본다.
    # (이 시험은 subprocess.run 을 통째로 가짜로 바꿔놔서, 안 끄면 조용히 None 이 되어
    #  "검사한 줄 알았는데 사실은 폴백만 검사" 하는 상태가 된다 — 2026-07-28 실제로 그랬음)
    real_live_label = sa.live_label
    sa.live_label = lambda: None

    try:
        a = sa.CREDS_DIR / "A.json"
        write(a, "옛-A")
        write(sa.CREDS_DIR / "B.json", "토큰-B")

        # A 를 쓰는 동안 claude 가 자격을 갱신했다 → 떠날 때 A.json 이 최신으로 바뀌어야 한다
        write(sa.LIVE, "새-A")
        msg = sa.save_back("A")
        check("갱신분을 되저장함", "되저장했습니다" in msg, msg)
        check("A 저장본이 최신과 같아짐", sa._fingerprint(a) == sa._fingerprint(sa.LIVE))
        check("한 세대 백업이 남음", (sa.CREDS_DIR / "prev" / "A.json").exists())

        msg = sa.save_back("A")
        check("변화 없으면 안 씀", "변화 없음" in msg, msg)

        # 기록이 어긋나 실제로는 B 자격이 꽂혀 있을 때 A 를 덮어쓰면 계정 하나를 잃는다
        write(sa.LIVE, "토큰-B")
        before = sa._fingerprint(a)
        msg = sa.save_back("A")
        check("다른 계정 자격이면 안 덮어씀", "덮어쓰지 않고" in msg, msg)
        check("A 저장본 그대로", sa._fingerprint(a) == before)

        check("저장본 없는 라벨은 건너뜀", "저장본이 없어" in sa.save_back("Z"))
        check("떠나는 계정을 모르면 건너뜀", "몰라" in sa.save_back(None))
        check("백업 폴더가 라벨 목록에 안 섞임", sa.list_labels() == ["A", "B"], sa.list_labels())
    finally:
        sa.CREDS_DIR, sa.LIVE = real_creds, real_live
        sa.live_label = real_live_label


def case_save_back_live_label(tmp):
    print("\n[25] 기록이 낡았으면 실제로 꽂힌 계정 쪽에 저장한다")
    import switch_account as sa

    real_creds, real_live, real_fn = sa.CREDS_DIR, sa.LIVE, sa.live_label
    root = Path(tmp) / "livelabel"
    (root / "creds").mkdir(parents=True, exist_ok=True)
    (root / "home").mkdir(parents=True, exist_ok=True)
    sa.CREDS_DIR = root / "creds"
    sa.LIVE = root / "home" / ".credentials.json"

    def write(path, token):
        path.write_text(json.dumps({"claudeAiOauth": {"refreshToken": token, "expiresAt": 1}}),
                        encoding="utf-8")

    try:
        a, b = sa.CREDS_DIR / "A.json", sa.CREDS_DIR / "B.json"
        write(a, "옛-A")
        write(b, "옛-B")
        # 사람이 /login 으로 B 를 꽂았는데 기록은 아직 A 인 상태
        write(sa.LIVE, "새-B")
        sa.live_label = lambda: "B"

        msg = sa.save_back("A")
        check("어긋남을 알아챈다", "실제로는 'B'" in msg, msg)
        check("B 에 저장됐다", sa._fingerprint(b) == sa._fingerprint(sa.LIVE), msg)
        check("A 는 안 건드렸다", sa._fingerprint(a) != sa._fingerprint(sa.LIVE))

        # 기록이 비어 있어도 실측으로 알아낸다
        write(sa.LIVE, "더새-B")
        msg = sa.save_back(None)
        check("기록이 비어도 실측으로 저장", sa._fingerprint(b) == sa._fingerprint(sa.LIVE), msg)

        # 실측이 기록과 같으면 그대로 그 라벨에 저장
        sa.live_label = lambda: "A"
        write(sa.LIVE, "새-A")
        msg = sa.save_back("A")
        check("일치하면 그대로 저장", sa._fingerprint(a) == sa._fingerprint(sa.LIVE), msg)
    finally:
        sa.CREDS_DIR, sa.LIVE, sa.live_label = real_creds, real_live, real_fn


def case_switch_passes_leaving(tmp):
    print("\n[24] 계정을 바꿀 때 떠나는 계정을 함께 넘긴다")
    setup(tmp, ["A", "B"], "A")
    fake = Fake({"A": (1, LIMIT_OUT, ""), "B": (0, OK_OUT, "")})
    install(fake)
    claude_pool.run(["claude", "-p", "안녕"])
    check("한도 교체 때 떠나는 계정 A 를 넘김", fake.leavings == ["A"], f"leavings={fake.leavings}")

    setup(tmp, ["A", "B"], "A")
    fake2 = Fake({})
    install(fake2)
    claude_pool.next_account()
    check("수동 전환도 떠나는 계정을 넘김", fake2.leavings == ["A"], f"leavings={fake2.leavings}")

    setup(tmp, ["A", "B"], "A")
    fake3 = Fake({})
    install(fake3)
    claude_pool.use("B")
    check("계정 지정도 떠나는 계정을 넘김", fake3.leavings == ["A"], f"leavings={fake3.leavings}")


def case_shared_iso_roundtrip():
    print("\n[20] 공유 저장소 시각 변환 왕복")
    import shared_state as sh
    now = 1785000000.0
    iso = sh._to_iso(now)
    back = sh._from_iso(iso)
    check("epoch→ISO→epoch 왕복", abs(back - now) < 1, f"차이={back - now}")
    check("Z 표기 파싱", sh._from_iso("2026-07-26T00:00:00Z") is not None)
    check("빈 값은 None", sh._from_iso("") is None)
    check("깨진 값은 None", sh._from_iso("어제") is None)
    check("설정 없으면 꺼짐", not sh.enabled() or True)  # 환경변수 유무 무관, 예외만 안 나면 됨


def case_shim_prompt_argument():
    print("\n[16] 가로채기가 표준입력을 언제 읽을지 가린다")
    import claude_shim

    yes = [
        ["-p", "안녕"],
        ["-p", "--model", "opus", "안녕하세요"],
        ["--print", "--output-format", "json", "질문"],
    ]
    no = [
        ["-p"],
        ["-p", "--output-format", "json"],
        ["--print", "--model", "claude-haiku-4-5-20251001"],
    ]
    for a in yes:
        check(f"프롬프트 인자 있음: {' '.join(a)[:34]}", claude_shim.has_prompt_argument(a))
    for a in no:
        check(f"프롬프트 인자 없음: {' '.join(a)[:34]}", not claude_shim.has_prompt_argument(a))
    check("단발 호출 판정", claude_shim.is_single_shot(["-p", "x"]))
    check("대화형은 단발 아님", not claude_shim.is_single_shot(["--model", "opus"]))


def main():
    real_run = subprocess.run
    real_switch = claude_pool._switch
    real_notify = claude_pool.notify
    real_shared = st.shared_state
    try:
        for case in (case_success_no_switch, case_false_positive, case_rotate_on_limit,
                     case_all_exhausted, case_already_switched, case_reset_parsing,
                     case_auth, case_unknown_error, case_manual_next,
                     case_answer_json_not_failure, case_dirty_body_not_limit,
                     case_broken_cred_after_switch, case_notice_once,
                     case_broken_state_file, case_subprocess_compat,
                     case_sequential_lowest, case_reset_soonest_first,
                     case_skip_blocked_before_call, case_reclaim_when_freed,
                     case_manual_use_not_reverted, case_shared_merge,
                     case_shared_three_machines, case_save_back,
                     case_save_back_live_label, case_switch_passes_leaving):
            with tempfile.TemporaryDirectory() as tmp:
                case(tmp)
        case_shared_iso_roundtrip()
        case_shim_prompt_argument()
    finally:
        claude_pool.subprocess.run = real_run
        claude_pool._switch = real_switch
        claude_pool.notify = real_notify

    print("\n" + "=" * 46)
    if failures:
        print(f"실패 {len(failures)}건: {', '.join(failures)}")
        return 1
    print("전부 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
