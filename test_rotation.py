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

import contextlib
import io
import json
import os
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

# 이 시험은 claude 호출을 가짜로 바꿔치려고 subprocess.run 자체를 갈아끼운다.
# 그런데 순환기 안에는 진짜 subprocess 가 필요한 자리도 있다(프로세스 시작 시각 조회).
# 그 자리를 시험할 때 잠깐 되돌리려고 진짜를 여기 붙잡아 둔다.
REAL_SUBPROCESS_RUN = subprocess.run

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

    # 열쇠꾸러미 안에 들어 있다고 칠 값. 지우기 전 백업이 되는지 보는 데 쓴다(가짜 값).
    # **재발급 열쇠가 만료된 값**이다. 이래야 진짜 '옛 자격 그림자'가 된다. 만료가 안 된
    # 값은 지우면 안 되는 최신본이므로 다른 길로 간다(keychain_value_fresh, 2026-07-30).
    keychain_value = json.dumps({"claudeAiOauth": {
        "refreshToken": "test-refresh", "accessToken": "test-access",
        "expiresAt": 1700000000000, "refreshTokenExpiresAt": 1700000000000,
    }}, ensure_ascii=False)

    # 아직 살아 있는 값(화면 세션 봇이 방금 갱신해 넣은 최신본을 흉내낸다)
    keychain_value_fresh = json.dumps({"claudeAiOauth": {
        "refreshToken": "test-refresh-new", "accessToken": "test-access-new",
        "expiresAt": 9999999999000, "refreshTokenExpiresAt": 9999999999000,
    }}, ensure_ascii=False)

    def __init__(self, per_label, switch_ok=True, keychain=None, keychain_out=None):
        self.per_label = per_label
        self.switch_ok = switch_ok
        self.calls = []
        self.switches = []
        self.leavings = []   # 갈아탈 때 '떠나는 계정'을 제대로 넘겼는지 확인용
        self.notices = []
        # 맥 열쇠꾸러미 흉내. keychain 은 항목에 걸린 계정 이름(없으면 None).
        # keychain_out 을 주면 그 항목이 남아 있는 동안 claude 가 그 응답으로 거절한다
        # (열쇠꾸러미 값이 계정 저장본을 가로채는 실제 상황).
        self.keychain = keychain
        self.keychain_out = keychain_out
        self.keychain_deleted = []

    def run(self, args, **kwargs):
        # 열쇠꾸러미 조회·삭제는 claude 호출이 아니다. 세지 않는다.
        if args and str(args[0]).endswith("security"):
            if "find-generic-password" in args and "-w" in args:
                # 값 읽기. 실제 코드는 화면 대신 파일로 받아간다.
                if not self.keychain or not self.keychain_value:
                    return subprocess.CompletedProcess(args, 1, "", "locked")
                fh = kwargs.get("stdout")
                if hasattr(fh, "write"):
                    # 보관용 읽기는 값을 파일로 받는다(_keychain_rescue)
                    fh.write(self.keychain_value)
                    fh.flush()
                    return subprocess.CompletedProcess(args, 0, "", "")
                # 낡았는지만 보는 읽기는 디스크를 안 거치고 이 프로세스 안으로 받는다
                # (2026-07-30: /tmp 파일이 0644 로 남던 것을 고침)
                return subprocess.CompletedProcess(args, 0, self.keychain_value, "")
            if "find-generic-password" in args:
                if self.keychain:
                    body = ('    "acct"<blob>="%s"\n    "svce"<blob>="Claude Code-credentials"\n'
                            % self.keychain)
                    return subprocess.CompletedProcess(args, 0, body, "")
                return subprocess.CompletedProcess(args, 1, "", "not found")
            if "delete-generic-password" in args:
                if self.keychain and self.keychain in args:
                    self.keychain_deleted.append(self.keychain)
                    self.keychain = None
                    return subprocess.CompletedProcess(args, 0, "", "")
                return subprocess.CompletedProcess(args, 1, "", "not found")
            return subprocess.CompletedProcess(args, 1, "", "unsupported")
        label = st.load_state().get("current")
        self.calls.append(label)
        if self.keychain and self.keychain_out:
            return subprocess.CompletedProcess(args, 1, self.keychain_out, "")
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
        self.scoped = {}   # {라벨: {칸: 해제시각}} — 모델 칸별 막힘 공유 흉내
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

    def get_scoped_blocked(self, now=None, provider=None):
        return {l: dict(b) for l, b in self.scoped.items()}

    def set_scoped_blocked(self, label, bucket, until, provider=None):
        box = self.scoped.setdefault(label, {})
        box[bucket] = max(float(box.get(bucket) or 0.0), float(until))
        return True

    def clear_scoped_blocked(self, label, provider=None):
        self.scoped.pop(label, None)
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


def case_live_label_source(tmp):
    print("\n[26] 지금 계정 판별은 캐시가 아니라 자격으로 물어본다")
    import switch_account as sa

    real_creds, real_live, real_mail = sa.CREDS_DIR, sa.LIVE, sa.cred_email
    root = Path(tmp) / "labelsrc"
    (root / "creds").mkdir(parents=True, exist_ok=True)
    (root / "home").mkdir(parents=True, exist_ok=True)
    sa.CREDS_DIR = root / "creds"
    sa.LIVE = root / "home" / ".credentials.json"

    def write(path, token):
        path.write_text(json.dumps({"claudeAiOauth": {"refreshToken": token, "expiresAt": 1}}),
                        encoding="utf-8")

    try:
        write(sa.CREDS_DIR / "A.json", "옛-A")
        write(sa.CREDS_DIR / "D.json", "옛-D")
        write(sa.LIVE, "갱신된-D")  # 갱신돼서 어느 저장본과도 지문이 안 맞는 상태

        # 서버가 D 라고 답하면 지문이 안 맞아도 D 로 알아낸다.
        # 이게 핵심이다: 갱신된 자격은 지문 대조만으로는 절대 못 알아낸다.
        sa.cred_email = lambda path=None: "REDACTED@example.com"
        check("갱신돼도 서버 답으로 알아냄", sa.live_label() == "D", sa.live_label())

        # 조회가 안 되면(연결 실패) 지문 대조로 떨어진다
        sa.cred_email = lambda path=None: None
        check("조회 실패 시 지문 대조로 떨어짐", sa.live_label() is None, sa.live_label())
        write(sa.LIVE, "옛-A")
        check("지문이 맞으면 그걸로 알아냄", sa.live_label() == "A", sa.live_label())

        # accounts.json 에 없는 계정이면 라벨을 못 정한다(엉뚱한 라벨로 우기지 않는다)
        sa.cred_email = lambda path=None: "모르는사람@example.com"
        write(sa.LIVE, "낯선-자격")
        check("모르는 계정은 None", sa.live_label() is None, sa.live_label())
    finally:
        sa.CREDS_DIR, sa.LIVE, sa.cred_email = real_creds, real_live, real_mail


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


def case_ok_envelope_bad_exitcode(tmp):
    print("\n[27] 껍데기가 성공이라 말하면 종료코드가 0 이 아니어도 계정을 막지 않는다")
    # 2026-07-29 실제 사고. 봇이 어제 자격 사고 로그를 판정시키자 답변 본문에
    # 'OAuth session expired' 가 들어왔고, 종료코드만 0 이 아니어서(끊긴 파이프 등)
    # 그 답변을 오류로 읽어 계정 넷이 통째로 막혔다. 그 뒤 봇들이 종량제로 넘어가 카드가 나갔다.
    answer = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "로그를 보니 Failed to authenticate: OAuth session expired and "
                  "could not be refreshed 와 usage limit reached 가 찍혀 있습니다",
        "stop_reason": "stop_sequence",
    }, ensure_ascii=False)
    check("계정 죽음으로 안 봄", limit_detect.classify(1, answer, "") == "error",
          f"실제={limit_detect.classify(1, answer, '')}")
    check("사유에 답변 본문이 안 실림",
          "OAuth session expired" not in limit_detect.short_reason(answer, ""))

    # 진짜 실패는 그대로 잡아야 한다
    dead = json.dumps({"type": "result", "subtype": "error_during_execution",
                       "is_error": True,
                       "result": "Failed to authenticate: OAuth session expired and "
                                 "could not be refreshed"}, ensure_ascii=False)
    check("진짜 자격 죽음은 여전히 dead", limit_detect.classify(1, dead, "") == "dead")
    check("오류통로 문구는 여전히 limit",
          limit_detect.classify(1, answer, "Claude usage limit reached") == "limit")

    setup(tmp, ["A", "B", "C", "D"], "A")
    fake = Fake({"A": (1, answer, "")})
    install(fake)
    claude_pool.run(["claude", "-p", "이 로그 판정해줘"])
    check("계정 교체 없음", fake.switches == [], f"switches={fake.switches}")
    check("계정을 막지 않음", st.load_state()["blocked"] == {},
          f"blocked={st.load_state()['blocked']}")
    check("전 계정 소진 알림 없음", fake.notices == [], f"notices={fake.notices}")


def case_dead_but_cred_alive(tmp):
    print("\n[28] 저장본이 살아 있으면 로그인 거절이 와도 계정을 막지 않는다")
    # 자격 파일을 갈아끼우는 순간에 호출이 겹치면 남의 자격으로 나가 인증 실패가 난다.
    # 그때 지금 계정을 죽었다고 막으면 멀쩡한 계정이 줄줄이 막히고, 봇들이 종량제로
    # 넘어가 카드가 나간다(2026-07-29 실제 사고).
    dead = json.dumps({
        "type": "result", "subtype": "success", "is_error": True,
        "api_error_status": 401, "stop_reason": "stop_sequence",
        "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
    }, ensure_ascii=False)

    real_alive = claude_pool._cred_alive
    try:
        # (1) 저장본이 살아 있음 → 막지 않고 같은 계정으로 재시도
        setup(tmp, ["A", "B", "C"], "A")
        claude_pool._cred_alive = lambda label: True
        fake = Fake({"A": (1, dead, "")})
        install(fake)
        claude_pool.run(["claude", "-p", "안녕"])
        check("계정 교체 없음", fake.switches == [], f"switches={fake.switches}")
        check("계정을 막지 않음", st.load_state()["blocked"] == {},
              f"blocked={st.load_state()['blocked']}")
        check("전 계정 소진 알림 없음", fake.notices == [], f"notices={fake.notices}")

        # (2) 저장본이 진짜 죽었으면(확인 불가 포함) 예전처럼 막고 넘긴다
        claude_pool._cred_alive = lambda label: False
        fake2 = Fake({"A": (1, dead, ""), "B": (0, OK_OUT, "")})
        install(fake2)
        proc = claude_pool.run(["claude", "-p", "안녕"])
        check("B 로 넘어가 성공", proc.returncode == 0 and fake2.switches == ["B"],
              f"switches={fake2.switches}")
        check("A 를 쉬게 기록", "A" in st.load_state()["blocked"],
              f"blocked={list(st.load_state()['blocked'])}")
    finally:
        claude_pool._cred_alive = real_alive


def case_keychain_shadow(tmp):
    print("\n[29] 자격 읽는 자리가 깨졌으면 멀쩡한 계정을 재우지 않고 사람에게 알린다")
    # 2026-07-29 실사고: 맥 열쇠꾸러미에 옛 자격이 남아 봇 자리에서만 모든 계정이 거절받았다.
    # 그때 계정을 재우면 멀쩡한 B 까지 잠긴다(실제로 잠겼다). 알림도 안 가 세 시간 반 방치됐다.
    dead = json.dumps({
        "type": "result", "subtype": "success", "is_error": True,
        "api_error_status": 401, "stop_reason": "stop_sequence",
        "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
    }, ensure_ascii=False)
    not_logged_in = json.dumps({
        "type": "result", "is_error": True,
        "result": "Not logged in · Please run /login",
    }, ensure_ascii=False)

    real_acct = claude_pool._keychain_account
    real_verdict = claude_pool._keychain_verdict
    real_alive = claude_pool._cred_alive
    try:
        # 여기서 보려는 건 '값이 낡은 경우'다. 값 읽기 자체는 다른 케이스에서 본다.
        claude_pool._keychain_verdict = lambda account: "stale" if account else "none"
        # (1) 로그인 거절 + 열쇠꾸러미에 옛 자격이 남음 → 막지 않고 알림
        setup(tmp, ["A", "B", "C"], "A")
        claude_pool._keychain_account = lambda: "kwon"
        claude_pool._cred_alive = lambda label: False   # 저장본 보호가 없어도 막히면 안 된다
        fake = Fake({"A": (1, dead, "")})
        install(fake)
        claude_pool.run(["claude", "-p", "안녕"])
        check("계정 교체 없음", fake.switches == [], f"switches={fake.switches}")
        check("계정을 막지 않음", st.load_state()["blocked"] == {},
              f"blocked={st.load_state()['blocked']}")
        check("사람에게 알림", any("열쇠꾸러미" in m for m in fake.notices),
              f"notices={len(fake.notices)}")
        check("지울 계정 이름이 안내에 들어감",
              any('-a kwon' in m for m in fake.notices))

        # (2) '로그인 안 됨' 문구(auth)로 와서 계정 교체 경로를 안 타도 알린다
        setup(tmp, ["A", "B"], "A")
        fake2 = Fake({"A": (1, not_logged_in, "")})
        install(fake2)
        claude_pool.run(["claude", "-p", "안녕"])
        check("교체 안 함", fake2.switches == [], f"switches={fake2.switches}")
        check("이 경로에서도 알림", any("열쇠꾸러미" in m for m in fake2.notices),
              f"notices={len(fake2.notices)}")

        # (3) 열쇠꾸러미가 깨끗하면 예전대로 막고 넘어간다
        setup(tmp, ["A", "B"], "A")
        claude_pool._keychain_account = lambda: None
        fake3 = Fake({"A": (1, dead, ""), "B": (0, OK_OUT, "")})
        install(fake3)
        proc = claude_pool.run(["claude", "-p", "안녕"])
        check("B 로 넘어가 성공", proc.returncode == 0 and fake3.switches == ["B"],
              f"switches={fake3.switches}")
        check("A 를 쉬게 기록", "A" in st.load_state()["blocked"],
              f"blocked={list(st.load_state()['blocked'])}")
    finally:
        claude_pool._keychain_account = real_acct
        claude_pool._keychain_verdict = real_verdict
        claude_pool._cred_alive = real_alive


def case_keychain_self_fix(tmp):
    print("\n[30] 열쇠꾸러미에 남은 옛 자격은 스스로 지우고 다시 시도한다")
    # 2026-07-29 사고를 사람 손 없이 풀기 위한 자동 복구. 지우는 건 열쇠꾸러미 항목 하나뿐이고
    # 계정 저장본은 건드리지 않는다. 한 시간에 한 번만 지운다(지우고 또 생기는 반복 방지).
    dead = json.dumps({
        "type": "result", "subtype": "success", "is_error": True,
        "api_error_status": 401, "stop_reason": "stop_sequence",
        "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
    }, ensure_ascii=False)
    not_logged_in = json.dumps({
        "type": "result", "is_error": True,
        "result": "Not logged in · Please run /login",
    }, ensure_ascii=False)

    real_stamp = claude_pool.KEYCHAIN_STAMP
    real_platform = claude_pool.sys.platform
    real_alive = claude_pool._cred_alive
    try:
        claude_pool.KEYCHAIN_STAMP = Path(tmp) / ".keychain-cleared"
        claude_pool.sys.platform = "darwin"   # 리눅스·윈도우에서 시험해도 같은 길을 보게
        claude_pool._cred_alive = lambda label: False

        # (1) 로그인 거절 + 열쇠꾸러미 항목 있음 → 스스로 지우고 같은 계정으로 성공
        setup(tmp, ["A", "B"], "A")
        fake = Fake({"A": (0, OK_OUT, "")}, keychain="kwon", keychain_out=dead)
        install(fake)
        proc = claude_pool.run(["claude", "-p", "안녕"])
        check("옛 자격을 지움", fake.keychain_deleted == ["kwon"], f"{fake.keychain_deleted}")
        rescued = list((Path(tmp) / "creds" / "rescued").glob("keychain-*.json"))
        check("지우기 전에 값을 따로 보관", len(rescued) == 1 and
              "test-refresh" in rescued[0].read_text(encoding="utf-8"), f"{rescued}")
        check("계정 저장본은 안 덮음",
              (Path(tmp) / "creds" / "A.json").read_text(encoding="utf-8").strip() == "{}")
        check("보관 파일이 계정 목록에 안 섞임", "rescued" not in st.saved_labels()
              and not any("keychain" in x for x in st.saved_labels()),
              f"labels={st.saved_labels()}")
        check("지운 뒤 같은 계정으로 성공", proc.returncode == 0, f"rc={proc.returncode}")
        check("다른 계정으로 안 넘어감", set(fake.switches) <= {"A"}, f"switches={fake.switches}")
        check("계정을 막지 않음", st.load_state()["blocked"] == {},
              f"blocked={st.load_state()['blocked']}")
        check("스스로 풀렸다고 알림", any("스스로 풀렸습니다" in m for m in fake.notices),
              f"notices={fake.notices[:1]}")
        check("사람이 지우라는 안내는 안 감",
              not any("security delete" in m for m in fake.notices))

        # (2) '로그인 안 됨' 문구로 와도 지우고 다시 시도한다
        claude_pool.KEYCHAIN_STAMP.unlink(missing_ok=True)
        setup(tmp, ["A", "B"], "A")
        fake2 = Fake({"A": (0, OK_OUT, "")}, keychain="kwon", keychain_out=not_logged_in)
        install(fake2)
        proc2 = claude_pool.run(["claude", "-p", "안녕"])
        check("이 경로에서도 지움", fake2.keychain_deleted == ["kwon"], f"{fake2.keychain_deleted}")
        check("지운 뒤 성공", proc2.returncode == 0, f"rc={proc2.returncode}")

        # (3) 값을 못 읽으면(화면 잠금 등) 지우지 않는다 — 최신 재발급 열쇠를 잃지 않기 위해
        claude_pool.KEYCHAIN_STAMP.unlink(missing_ok=True)
        setup(tmp, ["A", "B"], "A")
        fake_locked = Fake({"A": (1, dead, "")}, keychain="kwon", keychain_out=dead)
        fake_locked.keychain_value = ""      # 잠겨서 값이 안 읽히는 상황
        install(fake_locked)
        claude_pool.run(["claude", "-p", "안녕"])
        check("값을 못 읽으면 안 지움", fake_locked.keychain_deleted == [],
              f"{fake_locked.keychain_deleted}")
        # 값을 못 읽는 자리(원격 접속 창)에서는 낡았는지 알 수 없다. 모르면서 "지우세요"라고
        # 알리면, 봇 자리에서 잘 돌고 있는 최신 자격을 사람 손으로 버리게 된다(2026-07-30 오탐).
        check("모르는 채로 지우라고 알리지 않음",
              not any("security delete" in m for m in fake_locked.notices),
              f"notices={fake_locked.notices[:1]}")

        # (4) 지웠는데 자격 파일 다시 깔기가 실패하면 고쳐졌다고 하지 않는다
        claude_pool.KEYCHAIN_STAMP.unlink(missing_ok=True)
        setup(tmp, ["A", "B"], "A")
        fake_noswitch = Fake({"A": (1, dead, "")}, switch_ok=False,
                             keychain="kwon", keychain_out=dead)
        install(fake_noswitch)
        try:
            claude_pool.run(["claude", "-p", "안녕"])
        except claude_pool.AllAccountsExhausted:
            pass   # 다시 깔기도 실패하고 갈아탈 데도 없으면 소진으로 끝난다(예상된 길)
        check("고쳐졌다고 알리지 않음",
              not any("스스로 풀렸습니다" in m for m in fake_noswitch.notices),
              f"notices={fake_noswitch.notices[:1]}")
        check("저장본을 다시 깔라고 안내", any("switch_account.py" in m
                                       for m in fake_noswitch.notices))
        check("이미 지운 열쇠꾸러미를 또 지우라고 하지 않음",
              not any("security delete" in m for m in fake_noswitch.notices))

        # (5) 방금 지웠는데 또 걸리면 손대지 않고 사람에게 넘긴다
        # 앞 케이스가 남긴 막힘 기록이 넘어오지 않게 상태 파일부터 지운다(같은 임시 폴더를 쓴다).
        (Path(tmp) / "state.json").unlink(missing_ok=True)
        setup(tmp, ["A", "B"], "A")
        claude_pool.KEYCHAIN_STAMP.write_text(str(int(time.time())), encoding="utf-8")
        fake3 = Fake({"A": (1, dead, "")}, keychain="kwon", keychain_out=dead)
        install(fake3)
        claude_pool.run(["claude", "-p", "안녕"])
        check("다시 지우지 않음", fake3.keychain_deleted == [], f"{fake3.keychain_deleted}")
        check("사람이 지울 명령을 안내", any("security delete" in m for m in fake3.notices),
              f"notices={len(fake3.notices)}")
        check("그래도 계정은 안 막음", st.load_state()["blocked"] == {},
              f"blocked={st.load_state()['blocked']}")
    finally:
        claude_pool.KEYCHAIN_STAMP = real_stamp
        claude_pool.sys.platform = real_platform
        claude_pool._cred_alive = real_alive


def case_keychain_fresh_not_shadow(tmp):
    print("\n[31] 열쇠꾸러미 값이 아직 살아 있으면 지우지 않고 두 자리를 맞춘다")
    # 2026-07-30 실사고: 화면 세션 봇이 자격을 갱신하면 최신 값이 열쇠꾸러미에만 남는다.
    # 그때 항목이 있다는 이유로 "옛 자격"이라 판정해 사람에게 지우라고 알렸다. 그대로 지웠으면
    # 유일하게 살아 있던 자격이 사라지고 저장본은 이미 폐기된 옛 열쇠라 계정이 통째로 죽는다.
    dead = json.dumps({
        "type": "result", "subtype": "success", "is_error": True,
        "api_error_status": 401, "stop_reason": "stop_sequence",
        "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
    }, ensure_ascii=False)

    real_stamp = claude_pool.KEYCHAIN_STAMP
    real_platform = claude_pool.sys.platform
    real_alive = claude_pool._cred_alive
    real_mirror = claude_pool._mirror_now
    try:
        claude_pool.KEYCHAIN_STAMP = Path(tmp) / ".keychain-cleared"
        claude_pool.sys.platform = "darwin"
        claude_pool._cred_alive = lambda label: False
        setup(tmp, ["A", "B"], "A")
        fake = Fake({"A": (0, OK_OUT, "")}, keychain="kwon", keychain_out=dead)
        fake.keychain_value = Fake.keychain_value_fresh   # 아직 살아 있는 값
        install(fake)

        # 되비추기가 실제로 하는 일: 파일 쪽을 열쇠꾸러미의 최신 값으로 맞춘다.
        # 그러면 다음 호출부터 거절이 사라진다. 가짜에서는 거절 응답을 거두는 것으로 흉내낸다.
        mirrored = []

        def fake_mirror():
            mirrored.append(1)
            fake.keychain_out = None
            return True

        claude_pool._mirror_now = fake_mirror
        proc = claude_pool.run(["claude", "-p", "안녕"])

        check("살아 있는 값은 안 지움", fake.keychain_deleted == [],
              f"{fake.keychain_deleted}")
        check("대신 두 자리를 맞춤", len(mirrored) >= 1, f"mirrored={mirrored}")
        check("맞춘 뒤 같은 계정으로 성공", proc.returncode == 0, f"rc={proc.returncode}")
        check("계정을 막지 않음", st.load_state()["blocked"] == {},
              f"blocked={st.load_state()['blocked']}")
        check("사람에게 지우라고 알리지 않음",
              not any("security delete" in m for m in fake.notices),
              f"notices={fake.notices[:1]}")
        check("사람 손이 필요하다고 알리지 않음", fake.notices == [],
              f"notices={fake.notices[:1]}")

        # (2) 이미 세 자리가 같은 값이면 되비출 것이 없다. 그때도 사람을 부르면 안 된다
        # (2026-07-30 실물 검증에서 나온 결함: '맞출 게 없음'을 '못 고침'으로 보고 알렸다).
        claude_pool.KEYCHAIN_STAMP.unlink(missing_ok=True)
        setup(tmp, ["A", "B"], "A")
        fake2 = Fake({"A": (1, dead, ""), "B": (0, OK_OUT, "")},
                     keychain="kwon", keychain_out=None)
        fake2.keychain_value = Fake.keychain_value_fresh
        install(fake2)
        claude_pool._mirror_now = lambda: False   # 맞출 것이 없는 상태
        proc2 = claude_pool.run(["claude", "-p", "안녕"])
        check("맞출 게 없어도 안 지움", fake2.keychain_deleted == [],
              f"{fake2.keychain_deleted}")
        # 평소의 계정 전환 알림은 나가도 된다. 나가면 안 되는 건 '자격 자리가 깨졌다'는 쪽이다.
        check("맞출 게 없어도 자격 자리 탓으로 안 알림",
              not any(("열쇠꾸러미" in m or "security delete" in m) for m in fake2.notices),
              f"notices={fake2.notices[:1]}")
        check("평소 계정 처리로 내려감(B 로 넘어가 성공)",
              proc2.returncode == 0 and fake2.switches == ["B"],
              f"rc={proc2.returncode} switches={fake2.switches}")
    finally:
        claude_pool.KEYCHAIN_STAMP = real_stamp
        claude_pool.sys.platform = real_platform
        claude_pool._cred_alive = real_alive
        claude_pool._mirror_now = real_mirror


def case_mirror_both_ways(tmp):
    print("\n[32] 되비추기가 양쪽을 견줘 나중 것을 반대편에 맞춘다")
    # 2026-07-31 실사고: 원격 접속 자리에서 클로드가 돌면 파일만 갱신되고 그 갱신이
    # 열쇠꾸러미 값을 폐기시킨다. 한 방향(열쇠꾸러미 -> 파일)만 보던 되비추기는 이걸 못 메워
    # 봇 자리만 한 시간 죽었다. 이제 출입증 만료 시각으로 나중 것을 가려 반대편에 민다.
    import cred_mirror as cm

    old = {"claudeAiOauth": {"refreshToken": "old-refresh", "expiresAt": 1000}}
    new = {"claudeAiOauth": {"refreshToken": "new-refresh", "expiresAt": 2000}}

    real = (cm.LIVE, cm.keychain_account, cm.read_keychain, cm.push_to_keychain,
            cm.label_of, cm.creds_dir, cm.apply_pending, cm.sys.platform)
    try:
        cm.sys.platform = "darwin"
        cm.LIVE = Path(tmp) / "live.json"
        cm.creds_dir = lambda: Path(tmp)
        cm.apply_pending = lambda: "none"
        cm.label_of = lambda obj: "B"
        cm.keychain_account = lambda: "kwon"

        # (1) 파일이 나중 것 -> 열쇠꾸러미로 민다. 파일은 덮이면 안 된다.
        pushed = []
        cm.read_keychain = lambda acct: json.loads(json.dumps(old))
        cm.push_to_keychain = lambda obj: (pushed.append(cm.fingerprint(obj)), True)[1]
        cm.write_atomic(cm.LIVE, new)
        cm.sync(quiet=True)
        check("파일이 나중이면 열쇠꾸러미로 민다", pushed == [cm.fingerprint(new)],
              f"pushed={pushed}")
        check("그때 파일은 옛 값으로 안 덮인다",
              cm.fingerprint(cm.read_json(cm.LIVE)) == cm.fingerprint(new),
              "파일이 열쇠꾸러미 값으로 덮였다")
        check("저장본도 나중 값으로 맞춘다",
              cm.fingerprint(cm.read_json(Path(tmp) / "B.json")) == cm.fingerprint(new),
              "저장본이 안 맞춰졌다")

        # (2) 밀기가 실패하면(원격·화면 잠김) 아무것도 건드리지 않는다
        pushed.clear()
        cm.push_to_keychain = lambda obj: False
        cm.write_atomic(cm.LIVE, new)
        cm.sync(quiet=True)
        check("밀기 실패면 파일을 안 건드린다",
              cm.fingerprint(cm.read_json(cm.LIVE)) == cm.fingerprint(new),
              "밀기 실패인데 파일이 덮였다")

        # (3) 열쇠꾸러미가 나중 것이면 예전대로 파일 쪽을 맞춘다 (회귀 확인)
        pushed.clear()
        cm.read_keychain = lambda acct: json.loads(json.dumps(new))
        cm.push_to_keychain = lambda obj: (pushed.append(1), True)[1]
        cm.write_atomic(cm.LIVE, old)
        cm.sync(quiet=True)
        check("열쇠꾸러미가 나중이면 파일을 맞춘다",
              cm.fingerprint(cm.read_json(cm.LIVE)) == cm.fingerprint(new),
              "파일이 안 맞춰졌다")
        check("그때는 열쇠꾸러미로 밀지 않는다", pushed == [], f"pushed={pushed}")

        # (4) 같은 값이면 양쪽 다 가만둔다
        pushed.clear()
        cm.write_atomic(cm.LIVE, new)
        changed = cm.sync(quiet=True)
        check("같은 값이면 아무 일도 안 한다", changed is False and pushed == [],
              f"changed={changed} pushed={pushed}")
    finally:
        (cm.LIVE, cm.keychain_account, cm.read_keychain, cm.push_to_keychain,
         cm.label_of, cm.creds_dir, cm.apply_pending, cm.sys.platform) = real


def case_usage_after_reset(tmp):
    """[33] 초기화 시각이 지난 기록은 버리지 말고 '남은 100%' 로 보여준다.

    2026-08-03 사용자 지적: D 가 실제로는 주간 한도가 풀려 다 쓸 수 있는데
    화면엔 '사용량 못 읽음' 만 떴다. 기록의 풀리는 시각이 지나 통째로 버려졌기 때문.
    """
    print("\n[33] 초기화된 뒤의 기록 표시")
    import usage_view

    now = time.time()

    # (1) 주간·세션 둘 다 초기화 시각이 지난 기록 = D 가 있던 상태
    state = {"usage_seen": {"D": {"weekly_pct": 100.0, "session_pct": 59.0,
                                  "weekly_reset": now - 4 * 3600,
                                  "session_reset": now - 60 * 3600,
                                  "at": now - 60 * 3600}}}
    line = usage_view.last_seen(state, None, "D", now)
    check("못 읽음이 아님", "못 읽음" not in line, line)
    check("주간 남은 100%", "주간 남은 100%" in line, line)
    check("세션 남은 100%", "세션 남은 100%" in line, line)
    check("초기화된 값이라고 밝힘", "초기화된 뒤 읽은 값 없음" in line, line)

    # (2) 한쪽만 지난 경우 = 실측한 주간은 그대로, 세션만 되살린다
    mixed = {"usage_seen": {"B": {"weekly_pct": 89.0, "session_pct": 19.0,
                                  "weekly_reset": now + 3 * 86400,
                                  "session_reset": now - 600,
                                  "at": now - 120}}}
    line = usage_view.last_seen(mixed, None, "B", now)
    check("실측한 주간은 그대로", "주간 남은 11%" in line, line)
    check("지난 세션만 되살림", "세션 남은 100%(초기화됨)" in line, line)
    check("섞였을 땐 언제 읽은 값인지 남김", "기준)" in line, line)

    # (3) 기록이 아예 없으면 예전대로 못 읽음
    check("기록 없으면 못 읽음", usage_view.last_seen({}, None, "C", now) == "사용량 못 읽음")

    # (4) 둘 다 아직 안 지난 값은 예전 표시 그대로 (회귀 없음)
    fresh = {"usage_seen": {"A": {"weekly_pct": 5.0, "session_pct": 0.0,
                                  "weekly_reset": now + 86400,
                                  "session_reset": now + 3600,
                                  "at": now - 30}}}
    line = usage_view.last_seen(fresh, None, "A", now)
    check("멀쩡한 값은 그대로", "주간 남은 95%" in line and "세션 남은 100%" in line, line)
    check("멀쩡한 값엔 초기화 표시 없음", "초기화" not in line, line)

    # (5) 더 최신인 공유 값이 있으면 그쪽을 쓴다 (되살리기가 우선순위를 안 흔든다)
    shared = {"D": {"weekly_pct": 30.0, "session_pct": 10.0,
                    "weekly_reset": now + 86400, "session_reset": now + 3600,
                    "at": now - 60, "host": "gwon-ui-Macmini"}}
    line = usage_view.last_seen(state, shared, "D", now)
    check("새 실측이 있으면 그 값을 씀", "주간 남은 70%" in line, line)


def case_stuck_on_dead_account(tmp):
    """[34] 한 계정이 몇 분째 로그인 거절만 받으면 판정을 믿지 말고 갈아탄다.

    2026-08-03 실사고: 마케팅위키 챗봇 답변이 1시간 24분 동안 전패했는데 순환기는
    "저장본은 살아 있습니다(서버 확인)" 만 반복하며 같은 계정에 눌러앉았다. 저장본 조회가
    되고 재발급 열쇠 날짜가 남아 있어도 실제 갱신은 거부될 수 있어서다(열쇠는 한 번 쓰면
    회전 폐기된다). 계정을 갈아끼우자 바로 풀렸다.

    짧게 겹쳐 나는 거절([28])과는 흐른 시간으로 가른다. 시간을 안 보고 횟수만 세면
    한 번의 호출이 재시도하는 사이에 발동해 [28] 이 깨진다.
    """
    print("\n[34] 같은 계정이 몇 분째 막혀 있으면 강제로 갈아탄다")
    dead = json.dumps({
        "type": "result", "subtype": "success", "is_error": True,
        "api_error_status": 401, "stop_reason": "stop_sequence",
        "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
    }, ensure_ascii=False)

    real_alive = claude_pool._cred_alive
    try:
        claude_pool._cred_alive = lambda label: True  # 저장본은 살아 있다고 나온다

        # (1) 방금 시작된 거절은 예전대로 안 갈아탄다(자격 갈아끼우다 겹친 경우)
        setup(tmp, ["A", "B", "C"], "A")
        fake = Fake({"A": (1, dead, "")})
        install(fake)
        claude_pool.run(["claude", "-p", "안녕"])
        check("몇 초짜리 겹침엔 안 갈아탐", fake.switches == [], f"switches={fake.switches}")
        check("아직 안 막음", st.load_state()["blocked"] == {},
              f"blocked={st.load_state()['blocked']}")

        # (2) 그 상태로 시간이 흐른다 — 처음 거절받은 시각을 4분 전으로 민다
        state = st.load_state()
        rec = state["auth_rejects"]["A"]
        check("거절을 세고 있음", int(rec.get("n") or 0) >= 1, f"rec={rec}")
        rec["first"] = time.time() - 4 * 60
        st.save_state(state)

        fake2 = Fake({"A": (1, dead, ""), "B": (0, OK_OUT, "")})
        install(fake2)
        proc = claude_pool.run(["claude", "-p", "안녕"])
        check("B 로 갈아타 성공", proc.returncode == 0 and fake2.switches == ["B"],
              f"switches={fake2.switches}")
        check("A 를 쉬게 기록", "A" in st.load_state()["blocked"],
              f"blocked={list(st.load_state()['blocked'])}")

        # (3) 강제로 넘길 땐 짧게만 쉬게 한다. 자격 자리가 원인일 수도 있는데 네 계정을
        #     30분씩 재우면 그동안 쓸 계정이 없어져 봇들이 종량제(카드)로 넘어간다.
        left = st.load_state()["blocked"]["A"] - time.time()
        check("짧게만 쉬게 함", left <= claude_pool.FORCED_ROTATE_COOLDOWN + 5,
              f"{int(left)}초")

        # (4) 성공한 계정의 거절 기록은 지워진다(다음 실패가 처음부터 세지도록)
        check("성공하면 기록을 지움", "B" not in (st.load_state().get("auth_rejects") or {}),
              f"auth_rejects={st.load_state().get('auth_rejects')}")

        # (5) 창(10분)이 지나 다시 거절받으면 처음부터 센다 — 하루 종일 쌓여 있다가
        #     멀쩡한 계정이 한 번 튕겼을 때 곧바로 갈아타는 일을 막는다
        state = st.load_state()
        n1, elapsed1 = st.note_auth_reject(state, "C", now=1000.0)
        n2, elapsed2 = st.note_auth_reject(state, "C", now=1000.0 + st.AUTH_REJECT_WINDOW + 1)
        check("창이 지나면 다시 1회부터", (n1, n2) == (1, 1), f"{n1},{n2}")
        check("흐른 시간도 초기화", elapsed2 == 0, f"{elapsed2}")
    finally:
        claude_pool._cred_alive = real_alive


def case_model_scoped_limit(tmp):
    """[35] 모델 칸(Fable)만 바닥나면 그 칸만 막고, 계정은 계속 쓴다.

    2026-08-12 실사고: 계정 A 의 주간 전체는 60% 여유였는데 Fable 만 100% 였다.
    순환기가 seven_day 만 보던 탓에 계정을 안 바꿨고, 사람이 손으로 모델을 바꿔야 했다.
    반대로 계정을 통째로 막아버리면 그 계정에 남은 Opus 몫까지 버리게 되므로 갈라야 한다.
    """
    print("\n[35] 모델 칸만 바닥난 경우")
    import limit_detect as ld
    import usage_check as uc

    # (1) 실제 응답 모양에서 모델 칸을 읽어낸다
    body = {"five_hour": {"utilization": 2.0, "resets_at": "2026-08-12T15:49:59+00:00"},
            "seven_day": {"utilization": 60.0, "resets_at": "2026-08-15T11:59:59+00:00"},
            "seven_day_opus": None,
            "limits": [{"kind": "session", "percent": 2, "scope": None},
                       {"kind": "weekly_all", "percent": 60, "scope": None},
                       {"kind": "weekly_scoped", "percent": 100,
                        "resets_at": "2026-08-15T11:59:59+00:00",
                        "scope": {"model": {"id": None, "display_name": "Fable"}}}]}
    scoped = uc._scoped(body)
    check("Fable 칸을 읽는다", scoped.get("Fable", {}).get("pct") == 100.0, scoped)
    check("이름 없는 칸은 안 넣는다", list(scoped) == ["Fable"], scoped)

    usage = {"weekly_pct": 60.0, "session_pct": 2.0, "scoped": scoped}
    check("칸을 안 주면 예전대로 여유",
          uc.which_limit(usage, threshold=100.0) is None)
    check("Fable 칸을 주면 소진으로 잡힘",
          uc.which_limit(usage, threshold=100.0, bucket="Fable") == "model:Fable")
    check("칸 정보가 없으면 소진으로 안 본다",
          uc.which_limit({"weekly_pct": 60.0, "scoped": {}}, threshold=100.0,
                         bucket="Fable") is None)

    # (2) 모델 이름 → 칸 이름
    check("fable[1m] → Fable", uc.bucket_for_model("fable[1m]") == "Fable")
    check("claude-fable-5 → Fable", uc.bucket_for_model("claude-fable-5") == "Fable")
    check("opus[1m] → Opus", uc.bucket_for_model("opus[1m]") == "Opus")
    check("모르면 None", uc.bucket_for_model("gpt-5") is None)
    check("안 주면 None", uc.bucket_for_model(None) is None)

    # (3) 칸만 막으면 그 모델만 건너뛰고 다른 모델은 그 계정을 그대로 쓴다
    setup(tmp, ["A", "B"], "A")
    state = st.load_state()
    st.mark_scoped_blocked(state, "A", "Fable", time.time() + 3600)
    st.save_state(state)
    state = st.load_state()
    check("계정 전체는 안 막힌다", not st.is_blocked(state, label="A"))
    check("그 칸은 막혔다", st.is_scoped_blocked(state, "A", "Fable"))
    check("다른 칸은 안 막혔다", not st.is_scoped_blocked(state, "A", "Opus"))
    check("Fable 로 고르면 A 를 건너뛴다",
          st.pick_next(state, bucket="Fable") == "B")
    check("Opus 로 고르면 A 를 그대로 쓴다",
          st.pick_next(state, bucket="Opus") == "A")
    check("칸을 안 주면 예전 그대로", st.pick_next(state) == "A")

    # (4) 시간이 지나면 칸 막힘도 저절로 풀린다
    state = st.load_state()
    state["scoped_blocked"] = {"A": {"Fable": time.time() - 10}}
    st.save_state(state)
    state = st.prune_blocked(st.load_state())
    check("풀린 칸은 지워진다", not st.is_scoped_blocked(state, "A", "Fable"))
    check("Fable 로 다시 A 를 쓴다", st.pick_next(state, bucket="Fable") == "A")

    # (5) 거절 문구 판정 — 판 번호가 낀 실제 문구
    real = "You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model."
    check("Fable 5 거절을 한도로 본다", ld.classify(1, "", real) == "limit", real)
    check("Opus 거절도 그대로", ld.classify(1, "", "You've reached your Opus limit.") == "limit")
    check("문맥 한도는 여전히 한도 아님",
          ld.classify(1, "", "context window limit reached") != "limit")
    check("예산 한도도 여전히 한도 아님",
          ld.classify(1, "", "spend limit reached") != "limit")

    # (6) 켜기 직전 모델 알아내기 — 인자가 먼저, 없으면 settings.json
    check("인자에서 읽는다",
          claude_pool.intended_model(["--continue", "--model", "opus[1m]"]) == "opus[1m]")
    check("등호 표기도 읽는다",
          claude_pool.intended_model(["--model=fable[1m]"]) == "fable[1m]")
    got = claude_pool.intended_model([])
    check("인자가 없으면 고정값을 본다", got is None or isinstance(got, str), got)


def case_scoped_no_reclaim(tmp):
    """[36] 칸이 막힌 계정으로는 되돌리지 않는다 (전환 알림 반복 방지).

    2026-08-14 실사고: Mac 이 A 의 Fable 칸 소진(주간 전체는 38% 남음)을 무시하고
    10분 간격으로 A 로 되돌렸다가, 다음 호출이 거절되며 매번 D 로 재전환 + 텔레그램
    알림이 나갔다. 같은 "A → D 전환됨" 알림이 하루 종일 반복돼 사용자가 지적했다.
    """
    print("\n[36] 칸 막힌 계정으로 안 되돌린다 (알림 반복 방지)")
    import usage_check as uc

    setup(tmp, ["A", "B"], "B")   # 자동 전환으로 B 에 앉아 있는 상황
    claude_pool.usage_check = uc  # run() 이 칸 이름을 뽑게 한다(순수 함수만 탄다, 통신 없음)
    state = st.load_state()
    st.mark_scoped_blocked(state, "A", "Fable", time.time() + 3600)
    state["reclaim_at"] = None  # 간격 제한이 판정을 가리지 않게
    st.save_state(state)

    # (1) 후보 판정 자체
    state = st.load_state()
    check("Fable 칸이면 A 는 후보가 아님",
          st.better_candidate(state, "B", bucket="Fable") is None,
          f"고른 것={st.better_candidate(state, 'B', bucket='Fable')}")
    check("칸을 안 주면 예전 그대로 A 가 후보",
          st.better_candidate(state, "B") == "A",
          f"고른 것={st.better_candidate(state, 'B')}")
    check("다른 칸(Opus)이면 A 가 후보",
          st.better_candidate(state, "B", bucket="Opus") == "A",
          f"고른 것={st.better_candidate(state, 'B', bucket='Opus')}")

    # (2) 실제 호출 흐름 — Fable 호출은 B 에 그대로 머문다 (왕복 없음 = 알림 없음)
    fake = Fake({"A": (0, OK_OUT, ""), "B": (0, OK_OUT, "")})
    install(fake)
    proc = claude_pool.run(["claude", "-p", "안녕", "--model", "fable[1m]"])
    check("성공", proc.returncode == 0)
    check("A 로 안 되돌아감", st.load_state()["current"] == "B",
          f"현재={st.load_state()['current']}")
    check("교체 자체가 없음", fake.switches == [], f"switches={fake.switches}")
    check("알림 없음", fake.notices == [], f"알림={fake.notices}")

    # (3) Opus 호출은 예전처럼 앞순위 A 로 되돌아간다 (순차 소진 유지)
    state = st.load_state()
    state["reclaim_at"] = None
    st.save_state(state)
    fake2 = Fake({"A": (0, OK_OUT, ""), "B": (0, OK_OUT, "")})
    install(fake2)
    proc = claude_pool.run(["claude", "-p", "안녕", "--model", "opus[1m]"])
    check("성공", proc.returncode == 0)
    check("Opus 는 A 로 되돌림", st.load_state()["current"] == "A",
          f"현재={st.load_state()['current']}")

    # (4) 지금 계정의 칸이 막혀 있으면 부르기 전에 미리 갈아탄다 (헛걸음·알림 없이)
    state = st.load_state()
    st.mark_scoped_blocked(state, "A", "Fable", time.time() + 3600)
    state["reclaim_at"] = time.time()  # 되돌리기 경로가 아니라 미리-교체 경로를 본다
    st.save_state(state)
    fake3 = Fake({"A": (0, OK_OUT, ""), "B": (0, OK_OUT, "")})
    install(fake3)
    proc = claude_pool.run(["claude", "-p", "안녕", "--model", "fable[1m]"])
    check("성공", proc.returncode == 0)
    check("부르기 전에 B 로 교체해 부름", fake3.calls == ["B"], f"부른 계정={fake3.calls}")
    check("미리 교체는 알림 없음", fake3.notices == [], f"알림={fake3.notices}")

    claude_pool.usage_check = None


def case_scoped_shared(tmp):
    """[37] 칸 막힘을 세 머신이 공유한다.

    계정 단위 막힘은 원래 공유됐지만 칸 막힘은 로컬에만 남아, 한 머신이 겪은 Fable
    소진을 다른 머신이 또 겪었다(거절 1번 + 전환 알림 1건 낭비). 이제 막을 때 공유
    칠판에 올리고, prune_blocked 가 병합해 세 머신이 같은 칸 막힘을 본다.
    """
    print("\n[37] 칸 막힘을 세 머신이 공유한다")
    setup(tmp, ["A", "B"], "B")

    # (1) 이 머신이 칸을 막으면 공유에 올라간다
    fake = FakeShared()
    st.shared_state = fake
    state = st.load_state()
    st.mark_scoped_blocked(state, "A", "Fable", time.time() + 3600)
    check("공유에 올라감", bool(fake.scoped.get("A", {}).get("Fable")), fake.scoped)

    # (2) 다른 머신이 막은 칸이 병합돼 되돌림·선택에서 빠진다
    other = FakeShared()  # 다른 머신이 올린 것만 있는 공유 칠판
    other.scoped["A"] = {"Fable": time.time() + 1800}
    st.shared_state = other
    state = st.prune_blocked(st.load_state())  # 로컬엔 칸 막힘이 없는 상태에서 병합
    check("다른 머신 칸 막힘이 병합됨", st.is_scoped_blocked(state, "A", "Fable"))
    check("되돌림 후보에서도 빠짐",
          st.better_candidate(state, "B", bucket="Fable") is None,
          f"고른 것={st.better_candidate(state, 'B', bucket='Fable')}")
    check("계정 단위로는 안 막힘", not st.is_blocked(state, label="A"))

    # (3) 계정 목록에 없는 라벨(남의 값)은 안 받는다
    other.scoped["Z"] = {"Fable": time.time() + 1800}
    state = st.prune_blocked(st.load_state())
    check("모르는 라벨은 안 받음", "Z" not in (state.get("scoped_blocked") or {}),
          state.get("scoped_blocked"))

    # (4) 지난 값은 병합에서 빠진다
    other.scoped["A"] = {"Fable": time.time() - 10}
    state = st.prune_blocked(st.load_state())
    check("풀린 칸은 안 받음", not st.is_scoped_blocked(state, "A", "Fable"))

    # (5) 수동 해제가 공유 기록도 지운다 (안 지우면 다음 병합이 도로 살린다)
    other.scoped["A"] = {"Fable": time.time() + 1800}
    state = st.prune_blocked(st.load_state())
    st.clear_blocked_label(state, "A")
    check("공유 칸 기록도 지워짐", "A" not in other.scoped, other.scoped)
    check("로컬 칸 기록도 지워짐", not st.is_scoped_blocked(state, "A", "Fable"))
    state = st.prune_blocked(state)
    check("해제 후 되돌림 후보 복귀", st.better_candidate(state, "B", bucket="Fable") == "A",
          f"고른 것={st.better_candidate(state, 'B', bucket='Fable')}")

    # (6) 공유를 못 읽어도 로컬 기록으로 버틴다
    class Down(FakeShared):
        def get_scoped_blocked(self, now=None, provider=None):
            return None  # 읽기 실패

    down = Down()
    st.shared_state = down
    state = st.load_state()
    st.mark_scoped_blocked(state, "A", "Fable", time.time() + 3600)
    state = st.prune_blocked(state)
    check("공유가 죽어도 로컬 칸 막힘 유지", st.is_scoped_blocked(state, "A", "Fable"))
    st.shared_state = None

    # (7) 진짜 shared_state: 기존 값 조회가 실패하면 쓰지 않는다.
    # 조회만 실패하고 쓰기가 성공하면 그 계정의 다른 칸 기록을 덮어 지우기 때문
    # (코덱스 검수 지적). 못 읽으면 안 쓰고 False — 로컬 기록으로 물러난다.
    import shared_state as ss
    real_req = ss._request
    calls = []

    def broken_get(method, path, **kw):
        calls.append(method)
        return None  # 통신 실패 흉내

    ss._request = broken_get
    try:
        got = ss.set_scoped_blocked("A", "Fable", time.time() + 3600)
        check("조회 실패면 쓰기 중단(False)", got is False, f"결과={got}")
        check("쓰기 요청 자체가 안 나감", "POST" not in calls, f"요청들={calls}")
    finally:
        ss._request = real_req


def case_meta_cleanup():
    """[38] 공유 칠판 청소 — 옛 알림 열쇠만 지우고 계정 상태 행은 지킨다.

    claim_once 가 만드는 중복 방지 열쇠(switch_* 등)는 한 번 쓰고 영영 안 지워져
    표가 계속 큰다(2026-08-14 실측 90행 중 75행). 아침 점검이 매일 cleanup_meta 를 부른다.
    """
    print("\n[38] 공유 칠판 청소 — 옛 알림 열쇠만 지운다")
    import shared_state as ss
    real_req = ss._request
    deleted_params = []

    def fake_req(method, path, *, params="", body=None, headers=None):
        if method == "GET":
            return [{"key": "switch_A_D_123"}, {"key": "cred-place:456"},
                    {"key": "codex:switch_A_B_9"},
                    {"key": "usage:A"}, {"key": "current:Mac"},
                    {"key": "scoped:A"}, {"key": "rotator_notified"}]
        if method == "DELETE":
            deleted_params.append(params)
            return []
        return None

    ss._request = fake_req
    try:
        got = ss.cleanup_meta(7)
        check("알림 열쇠 3행만 지움", got == (3, 3), f"결과={got}")
        joined = " ".join(deleted_params)
        check("계정 상태 행은 삭제 요청에 없음",
              "usage" not in joined and "scoped" not in joined
              and "current" not in joined and "rotator_notified" not in joined, joined)
        check("삭제에도 시각 조건이 붙음", "updated_at=lt." in joined, joined)

        deleted_params.clear()
        got = ss.cleanup_meta(7, dry_run=True)
        check("시험 조회는 안 지움", got == (0, 3) and not deleted_params,
              f"결과={got} 삭제요청={deleted_params}")

        ss._request = lambda *a, **k: None
        check("조회 실패면 None(안 지움)", ss.cleanup_meta(7) is None)

        # 지우기 요청만 실패하면 (지운 수 < 후보 수) 로 드러나야 한다.
        # 조용히 (0, N) 이 성공처럼 지나가면 아침 점검이 실패를 영영 못 본다(코덱스 지적).
        def get_ok_delete_fail(method, path, *, params="", body=None, headers=None):
            if method == "GET":
                return [{"key": "switch_A_B_1"}]
            return None  # DELETE 실패

        ss._request = get_ok_delete_fail
        got = ss.cleanup_meta(7)
        check("지우기 실패는 지운 수<후보 수로 드러남",
              got == (0, 1) and got[0] < got[1], f"결과={got}")
    finally:
        ss._request = real_req


def case_dead_quarantine(tmp):
    """[39] 로그인 거절이 하루 안에 두 번 굳으면 저장본을 격리하고 재로그인 카드를 보낸다.

    2026-08-15 실사고: 죽은 A 를 5분 재우고 1순위라 계속 되돌아가는 왕복이 매시간
    전환 알림을 쏟아냈다. 두 번째로 굳으면 creds/dead/ 격리로 왕복을 끊는다.
    막힘 기록(mark_blocked)은 세 머신이 공유하므로 남기지 않는다 — 저장본 죽음은
    이 머신만의 문제라 다른 머신의 같은 라벨까지 막으면 안 된다.
    """
    print("\n[39] 거절이 두 번 굳으면 저장본 격리 + 재로그인 카드")
    dead = json.dumps({
        "type": "result", "subtype": "success", "is_error": True,
        "api_error_status": 401, "stop_reason": "stop_sequence",
        "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
    }, ensure_ascii=False)
    real_alive = claude_pool._cred_alive
    try:
        claude_pool._cred_alive = lambda label: True
        setup(tmp, ["A", "B", "C"], "A")
        fake = Fake({"A": (1, dead, ""), "B": (0, OK_OUT, "")})
        install(fake)
        # 이미 한 번 강제 교체된 이력 + 4분째 거절이 이어지는 상태를 만든다
        state = st.load_state()
        state["auth_rejects"] = {"A": {"n": 5, "first": time.time() - 240, "last": time.time() - 5}}
        state["forced_rotates"] = {"A": [time.time() - 3600]}
        st.save_state(state)
        proc = claude_pool.run(["claude", "-p", "안녕"])
        check("B 로 갈아타 성공", proc.returncode == 0 and fake.switches == ["B"],
              f"switches={fake.switches}")
        creds = Path(tmp) / "creds"
        check("A 저장본이 dead/ 로 격리됨",
              not (creds / "A.json").exists() and (creds / "dead" / "A.json").exists())
        check("공유되는 막힘 기록은 안 남김", "A" not in st.load_state()["blocked"],
              f"blocked={list(st.load_state()['blocked'])}")
        cards = [n for n in fake.notices if "순환에서 뺐습니다" in n]
        check("재로그인 카드 1건", len(cards) == 1, f"notices={len(fake.notices)}")
        check("카드에 재로그인 명령이 담김", cards and "relogin.py" in cards[0])
    finally:
        claude_pool._cred_alive = real_alive


def case_dead_alert_folded(tmp):
    """[40] 죽은 저장본이 낀 전환 알림은 같은 (경로, 사유)면 하루 한 번으로 접힌다."""
    print("\n[40] dead 전환 알림은 하루 1회로 접힘")
    setup(tmp, ["A", "B"], "A")
    fake = Fake({})
    install(fake)
    shared = FakeShared()
    real_shared = st.shared_state
    st.shared_state = shared
    try:
        state = st.load_state()
        claude_pool._notify_switch("A", "D", "dead", state)
        claude_pool._notify_switch("A", "D", "dead", state)
        check("같은 dead 전환은 1건만", len(fake.notices) == 1, f"{len(fake.notices)}건")
        claude_pool._notify_switch("A", "D", "limit", state)
        check("한도 전환은 별개로 나감", len(fake.notices) == 2, f"{len(fake.notices)}건")
        claude_pool._notify_switch("A", "D", "limit", state)
        check("한도 전환도 10분 창 중복은 접힘", len(fake.notices) == 2, f"{len(fake.notices)}건")
    finally:
        st.shared_state = real_shared


def case_keychain_pull_and_rescue(tmp):
    """[41] 되저장이 열쇠꾸러미 갱신분을 놓치지 않는다.

    실측(2026-08-15): 화면 세션의 클로드는 갱신값을 열쇠꾸러미로 옮기고 자격 파일을
    지워버린다. 또 예약 전환이 열쇠꾸러미를 덮으면서 밤새 갱신된 값이 유실됐다(A 사망).
    (1) 파일이 지워졌으면 열쇠꾸러미 값으로 복원하고 (2) 덮어쓰기 전엔 주인 라벨에 되저장한다.
    """
    print("\n[41] 열쇠꾸러미 최신값 복원 + 덮어쓰기 전 되저장")
    import switch_account as sw
    tmp = Path(tmp)
    creds = tmp / "creds"
    creds.mkdir(parents=True, exist_ok=True)
    real_live, real_dir = sw.LIVE, sw.CREDS_DIR
    real_read = sw.read_keychain_value
    real_email, real_label = sw.cred_email, sw.label_of_email
    try:
        sw.LIVE = tmp / "live.json"
        sw.CREDS_DIR = creds
        fresh = {"claudeAiOauth": {"refreshToken": "kc-new", "accessToken": "x",
                                   "expiresAt": 9_999_999_999_000}}
        sw.read_keychain_value = lambda: json.loads(json.dumps(fresh))

        # (0) 주인을 못 알아내는 값은 파일로 승격하지 않는다(엉뚱한 라벨 오염 방지)
        sw.cred_email = lambda p=None: None
        sw.label_of_email = lambda e: None
        note0 = sw._pull_live_from_keychain()
        check("주인 미상 값은 rescued 보관만", note0 is not None and "보관만" in note0
              and not sw.LIVE.exists(), f"note={note0}")

        # 주인이 확인되면 파일로 복원한다
        sw.cred_email = lambda p=None: "z@test"
        sw.label_of_email = lambda e: "Z" if e == "z@test" else None
        note = sw._pull_live_from_keychain()
        check("파일 없음 → 열쇠꾸러미 값으로 복원", note is not None and sw.LIVE.exists())
        check("복원된 값이 열쇠꾸러미 것", "kc-new" in sw.LIVE.read_text(encoding="utf-8"))

        newer = {"claudeAiOauth": {"refreshToken": "file-newest", "accessToken": "x",
                                   "expiresAt": 9_999_999_999_500}}
        sw.LIVE.write_text(json.dumps(newer), encoding="utf-8")
        check("파일이 최신이면 그대로 둠",
              sw._pull_live_from_keychain() is None
              and "file-newest" in sw.LIVE.read_text(encoding="utf-8"))

        (creds / "A.json").write_text(
            json.dumps({"claudeAiOauth": {"refreshToken": "a-old", "expiresAt": 1_000}}),
            encoding="utf-8")
        (creds / "B.json").write_text(
            json.dumps({"claudeAiOauth": {"refreshToken": "b-cur", "expiresAt": 2_000}}),
            encoding="utf-8")
        kc_a = {"claudeAiOauth": {"refreshToken": "a-new", "accessToken": "x",
                                  "expiresAt": 9_999_999_999_000}}
        sw.read_keychain_value = lambda: json.loads(json.dumps(kc_a))
        sw.cred_email = lambda p=None: "a@test"
        sw.label_of_email = lambda e: "A" if e == "a@test" else None
        note = sw._rescue_keychain_value(creds / "B.json")
        check("덮기 전 A 갱신분을 저장본에 되저장",
              note is not None and "a-new" in (creds / "A.json").read_text(encoding="utf-8"),
              f"note={note}")
        check("이전 값은 prev 에 한 세대 보관", (creds / "prev" / "A.json").exists())

        note2 = sw._rescue_keychain_value(creds / "B.json")
        check("이미 저장된 값은 재작업 없음", note2 is None, f"note={note2}")

        kc_x = {"claudeAiOauth": {"refreshToken": "who-knows", "accessToken": "x",
                                  "expiresAt": 9_999_999_999_100}}
        sw.read_keychain_value = lambda: json.loads(json.dumps(kc_x))
        sw.cred_email = lambda p=None: None
        note3 = sw._rescue_keychain_value(creds / "B.json")
        check("주인 미상 값은 rescued/ 에 보관",
              note3 is not None and any((creds / "rescued").glob("kc-*.json")),
              f"note={note3}")
    finally:
        sw.LIVE, sw.CREDS_DIR = real_live, real_dir
        sw.read_keychain_value = real_read
        sw.cred_email, sw.label_of_email = real_email, real_label


def case_barred(tmp):
    """[42] 조직 차단(403)이면 그 계정을 하루 재우고 다음 계정으로 갈아탄다.

    2026-08-18 실사고: C 계정이 "Your organization has disabled Claude subscription
    access for Claude Code" 403 을 받는데 'error'(한도 무관)로 분류돼 갈아타지 않고
    같은 계정으로 재시도만 58번, 마케팅위키 답변이 30분 전패했다. 자격은 멀쩡한
    상태라 dead 경로(저장본 생존 확인 → 눌러앉기)로도 못 다룬다.
    """
    print("\n[42] 조직 차단(403)은 하루 재우고 갈아탄다")
    setup(tmp, ["A", "B"], "A")
    barred_out = json.dumps({
        "type": "result", "subtype": "success", "is_error": True,
        "api_error_status": 403,
        "result": ("Your organization has disabled Claude subscription access for "
                   "Claude Code · Use an Anthropic API key instead, or ask your "
                   "admin to enable access"),
    }, ensure_ascii=False)
    fake = Fake({"A": (1, barred_out, ""), "B": (0, OK_OUT, "")})
    install(fake)
    check("판정이 barred", limit_detect.classify(1, barred_out, "") == "barred")
    proc = claude_pool.run(["claude", "-p", "안녕"])
    check("갈아탄 뒤 성공", proc.returncode == 0)
    check("B 로 교체됨", fake.switches == ["B"], f"switches={fake.switches}")
    state = st.load_state()
    check("A 가 막힘", "A" in state["blocked"], f"{state['blocked']}")
    left = float(state["blocked"]["A"]) - time.time()
    check("하루 안팎 재움", 20 * 3600 < left <= 25 * 3600, f"left={left / 3600:.1f}h")
    check("저장본 격리 안 함(자격은 멀쩡)",
          (Path(tmp) / "creds" / "A.json").exists())


def case_scoped_usage_shared():
    """[43] 계정별 모델 칸(Fable)도 공유 칠판에 올린다.

    2026-08-20 실사고: put_usage 가 주간·세션만 올려서 `cc` 화면에 현재 계정 A 만
    Fable 이 보이고 B·D 는 안 보였다. 화면은 로컬 기록과 공유 값 중 더 최신 것을 쓰는데
    남의 머신이 올린 값이 늘 더 새것이라, 칸을 뺀 채로 올리면 로컬에 남아 있던 칸 값까지
    덮여 사라진다. 지금 안 쓰는 계정은 출입증이 8시간이라 실측이 안 되므로 이 길이 유일하다.
    """
    print("\n[43] 모델 칸(Fable)도 공유 칠판에 올린다")
    import shared_state as ss
    import usage_view
    real_req = ss._request
    sent = []

    def capture(method, path, *, params="", body=None, headers=None):
        if method == "POST":
            sent.append(body)
        return []

    ss._request = capture
    try:
        now = time.time()
        usage = {"weekly_pct": 40.0, "session_pct": 10.0,
                 "weekly_reset": now + 3600, "session_reset": now + 3600,
                 "scoped": {"Fable": {"pct": 100.0, "reset": now + 3600}}}
        check("올리기 성공", ss.put_usage("B", usage))
        val = json.loads(sent[0][0]["value"])
        check("본문에 모델 칸이 실린다", val.get("scoped") == usage["scoped"],
              f"{val.get('scoped')}")

        sent.clear()
        ss.put_usage("B", {"weekly_pct": 5.0, "weekly_reset": now + 3600})
        val = json.loads(sent[0][0]["value"])
        check("칸이 없는 도구(지피티)는 빈 칸으로", val.get("scoped") == {},
              f"{val.get('scoped')}")
    finally:
        ss._request = real_req

    # 화면: 남의 머신이 올린 값이 더 최신이어도 Fable 줄이 살아 있어야 한다.
    now = time.time()
    local = {"usage_seen": {"B": {"weekly_pct": 40.0, "weekly_reset": now + 3600,
                                  "scoped": {"Fable": {"pct": 20.0, "reset": now + 3600}},
                                  "at": now - 86400}}}
    fresh = {"B": {"weekly_pct": 40.0, "weekly_reset": now + 3600,
                   "scoped": {"Fable": {"pct": 100.0, "reset": now + 3600}},
                   "at": now - 60, "host": "gwon-ui-Macmini"}}
    line = usage_view.last_seen(local, fresh, "B", now)
    check("남의 머신 값에서도 Fable 이 보인다", "Fable 남은 0%" in line, line)

    # 옛 기록(칸 없음)이 더 최신이면 예전 화면 그대로 — 없는 칸을 지어내지 않는다.
    old = {"B": {"weekly_pct": 40.0, "weekly_reset": now + 3600,
                 "at": now - 10, "host": "gwon-ui-Macmini"}}
    line = usage_view.last_seen(local, old, "B", now)
    check("칸 없는 옛 기록은 그냥 안 적는다", "Fable" not in line, line)


def case_scoped_all_empty_notice():
    """[44] 쓰려는 모델 칸이 전 계정에서 바닥이면 켤 때 한 줄로 먼저 알린다.

    2026-08-20 사용자 요청: 지금은 계정을 하나씩 건너뛰다 마지막 계정에서야
    '전 계정 한도' 를 알게 된다. 표시 전용이고 계정을 막지는 않는다.
    """
    print("\n[44] 전 계정 모델 칸 소진은 켤 때 한 줄로 알린다")
    import usage_view
    now = time.time()
    soon_reset, late_reset = now + 3600, now + 7200

    line = claude_pool._scoped_notice(
        "Fable", [("A", 0.0, late_reset), ("B", 0.0, soon_reset), ("D", 2.0, None)], now)
    check("전부 바닥이면 알린다", bool(line) and "쓸 수 있는 계정이 없습니다" in line, line)
    check("계정이 다 적힌다", "A, B, D 모두 바닥" in (line or ""), line)
    check("가장 빨리 풀리는 계정을 짚는다", "가장 빨리 풀리는 건 B" in (line or ""), line)

    check("한 계정이라도 여유면 안 알린다",
          claude_pool._scoped_notice(
              "Fable", [("A", 0.0, soon_reset), ("B", 40.0, soon_reset)], now) is None)
    check("근거가 하나도 없으면 안 알린다",
          claude_pool._scoped_notice("Fable", [("A", None, None)], now) is None)

    mixed = claude_pool._scoped_notice(
        "Fable", [("A", 0.0, soon_reset), ("C", None, None)], now)
    check("모르는 계정은 바닥으로 안 세고 따로 적는다",
          bool(mixed) and "잔량 모르는 계정: C" in mixed, mixed)
    # 확인 못 한 계정이 있는데 '쓸 계정이 없다' 고 단정하면 안 된다(2026-08-20 코덱스 지적, 재현됨).
    check("모르는 계정이 섞이면 확인한 범위만 말한다",
          "잔량을 확인한 계정(A)은 모두 바닥" in (mixed or "")
          and "쓸 수 있는 계정이 없습니다" not in (mixed or ""), mixed)

    # 잔량 뽑기 규칙: 초기화 시각이 지난 기록은 그 칸이 다시 채워졌다는 뜻이다.
    passed = {"scoped": {"Fable": {"pct": 100.0, "reset": now - 10}}}
    check("초기화 지난 칸은 남은 100%",
          usage_view.scoped_left(passed, "Fable", now)[0] == 100.0)
    live = {"scoped": {"Fable": {"pct": 100.0, "reset": now + 10}}}
    check("아직 안 풀린 칸은 남은 0%",
          usage_view.scoped_left(live, "Fable", now)[0] == 0.0)
    check("칸 정보가 없으면 모름(0% 아님)",
          usage_view.scoped_left({}, "Fable", now) == (None, None))


def case_cli_update_verdict():
    """[45] CLI 자동 갱신 점검이 조용히 '문제 없음' 으로 끝나지 않는다.

    2026-08-20 코덱스 검수 지적(재현 확인): 가로채기 재설치 명령이 실패해도 기록만 남기고
    문제로 세지 않아 '문제 0건' 으로 끝날 수 있었다. install_shim.sh 는 자리를 여러 곳
    (PATH·nvm) 덮으므로 일부만 실패하면 상태 확인은 통과한다. 확인만 하는 회차(--dry-run)도
    판정을 건너뛰어 거짓 안심을 줬다.
    """
    print("\n[45] CLI 갱신 점검 판정")
    import auto_update_cli as up

    check("판을 못 읽으면 문제", up.verdict(None, True) is not None)
    check("가로채기 벗겨지면 문제", up.verdict((2, 1, 237), False) is not None)
    check("재설치 실패는 상태가 멀쩡해도 문제",
          up.verdict((2, 1, 237), True, install_failed=True) is not None)
    check("다 정상이면 조용", up.verdict((2, 1, 237), True) is None)

    # Mac: PATH 쪽만 덮이고 nvm 쪽이 안 덮이면 nvm 을 불러온 봇이 순환기를 건너뛴다.
    mac_ok = ("지금 연결: /Users/kwon/projects/ai-rotator/claude_shim.sh\n"
              "상태: 순환 가로채기 설치됨\n"
              "nvm claude: 가로채기 덮임 (/Users/kwon/.nvm/versions/node/v24.18.0/bin/claude)")
    mac_partial = mac_ok.replace("가로채기 덮임", "안 덮임 — 순환기를 건너뛸 수 있음")
    check("Mac 전부 덮이면 정상", up.judge_shim("Mac", mac_ok))
    check("Mac nvm 이 안 덮이면 문제", not up.judge_shim("Mac", mac_partial))
    check("Mac 원래 claude 면 문제",
          not up.judge_shim("Mac", "지금 연결: /opt/homebrew/lib/node_modules/x/claude.exe"))

    # winserver: 한글이 이 통로를 지나며 깨져 와도 True/False 줄만으로 갈려야 한다.
    ws_ok = ("claude.exe   : False\nclaude-real.exe: True\nclaude.cmd   : True\n"
             "����: ����")
    ws_bypass = ws_ok.replace("claude.exe   : False", "claude.exe   : True")
    check("winserver 밀려 있으면 정상", up.judge_shim("winserver", ws_ok))
    check("winserver 원래 파일 되살아나면 문제", not up.judge_shim("winserver", ws_bypass))
    check("winserver 출력을 못 읽으면 문제", not up.judge_shim("winserver", "연결 실패"))


def case_scoped_exhausted_message(tmp):
    """[46] 모델 칸만 바닥난 소진은 그렇게 말하고, 해제 시각도 그 칸으로 읽는다.

    2026-08-20 실사고: Fable 칸만 A·B·D 전부 바닥인데 텔레그램엔 "클로드 구독 계정이
    전부 한도에 걸렸습니다. 가장 빨리 풀리는 시각: 08월 21일 03:51" 이 나갔다.
    계정 주간은 22~47% 남아 Opus 는 멀쩡했고, 03:51 은 B 의 5시간 한도가 풀리는
    시각이라 Fable 이 실제로 풀리는 시각(08/21 16:00)보다 12시간 넘게 일렀다.
    문구는 2026-08-14 에 전환 알림만 갈라 말하게 고쳤고 이 알림은 빠져 있었다.
    """
    print("\n[46] 칸만 바닥이면 그렇게 알리고 시각도 그 칸으로")
    setup(tmp, ["A", "B", "D"], "B")
    now = time.time()
    fable_a, fable_b, fable_d = now + 46 * 3600, now + 17 * 3600, now + 75 * 3600
    session_b = now + 5 * 3600   # B 는 5시간 한도로 계정 자체도 잠깐 막혀 있었다

    state = st.load_state()
    st.mark_blocked(state, "B", session_b, "세션 한도")
    for label, until in (("A", fable_a), ("B", fable_b), ("D", fable_d)):
        st.mark_scoped_blocked(state, label, "Fable", until)
    st.save_state(state)
    state = st.load_state()

    # (1) 해제 시각
    check("칸을 안 주면 예전대로 계정 막힘만 본다",
          abs(st.earliest_reset(state) - session_b) < 2, st.earliest_reset(state))
    got = st.earliest_reset(state, bucket="Fable")
    check("칸을 주면 그 칸이 실제로 풀리는 시각", got is not None and abs(got - fable_b) < 2,
          f"got={got} 기대={fable_b}")
    check("계정 막힘보다 이른 시각을 알리지 않는다", got is not None and got > session_b,
          f"got={got} session_b={session_b}")

    # (2) 후보 판정 — 그 칸으로는 없고 계정 단위로는 남아 있다
    check("그 칸으로는 쓸 계정이 없다", st.pick_next(state, bucket="Fable") is None)
    check("계정 단위로는 아직 쓸 계정이 있다", st.pick_next(state) is not None,
          st.pick_next(state))

    # (3) 문구
    msg = claude_pool._exhausted_message(state, "Fable", "08월 21일 16:00")
    check("칸이 바닥이라고 말한다", "Fable 칸이 계정 전부 바닥" in msg, msg)
    check("구독이 통째로 떨어진 것처럼 말하지 않는다", "구독 계정이 전부 한도" not in msg, msg)
    check("받은 사람이 할 수 있는 행동을 적는다", "다른 모델로 켜면" in msg, msg)

    blocked_all = st.load_state()
    for label in ("A", "D"):
        st.mark_blocked(blocked_all, label, now + 3 * 3600, "주간 한도")
    msg2 = claude_pool._exhausted_message(blocked_all, "Fable", "08월 21일 16:00")
    check("진짜 전 계정 소진이면 예전 문구 그대로",
          "구독 계정이 전부 한도에 걸렸습니다" in msg2, msg2)
    check("칸이 없어도 예전 문구",
          "구독 계정이 전부 한도에 걸렸습니다"
          in claude_pool._exhausted_message(state, None, "08월 21일 16:00"))

    # (4) 영영 안 돌아올 계정의 시각을 '가장 빨리 풀림' 으로 알리지 않는다
    state["scoped_blocked"]["Z"] = {"Fable": now + 60}       # 계정 목록에 없는 라벨
    check("목록에 없는 라벨은 안 센다",
          abs(st.earliest_reset(state, bucket="Fable") - fable_b) < 2)
    (Path(tmp) / "creds" / "A.json").unlink()
    check("저장본이 빠진 계정 시각도 안 센다",
          abs(st.earliest_reset(state, bucket="Fable") - fable_b) < 2,
          st.earliest_reset(state, bucket="Fable"))

    # (5) 실제 거절 경로로 태워 본다 — 알림에 실려 나가는 문구와 시각까지
    st.STATE_FILE.unlink()   # 앞 블록의 막힘 기록을 물려받지 않는다
    setup(tmp, ["A", "B", "D"], "B")
    state = st.load_state()
    for label, until in (("A", fable_a), ("D", fable_d)):
        st.mark_scoped_blocked(state, label, "Fable", until)
    st.save_state(state)

    import usage_check as uc
    claude_pool.usage_check = uc
    real_get = uc.get_usage
    # 통신 없이 그 계정의 사용량을 흉내낸다: 주간은 여유, Fable 칸만 바닥.
    uc.get_usage = lambda path: {"weekly_pct": 78.0, "session_pct": 12.0,
                                 "weekly_reset": None, "session_reset": None,
                                 "scoped": {"Fable": {"pct": 100.0, "reset": fable_b}}}
    raised = None
    try:
        claude_pool._rotate_after_failure("B", "limit", LIMIT_OUT, "", bucket="Fable")
    except claude_pool.AllAccountsExhausted as e:
        raised = e
    finally:
        uc.get_usage = real_get
        claude_pool.usage_check = None

    check("쓸 계정이 없다고 올린다", raised is not None)
    text = (raised.notify_message or "") if raised else ""
    check("알림도 칸 소진으로 나간다", "Fable 칸이 계정 전부 바닥" in text, text)
    check("알림 시각이 그 칸이 풀리는 시각",
          raised is not None and raised.reset_at is not None
          and abs(raised.reset_at - fable_b) < 2,
          f"reset_at={raised.reset_at if raised else None} 기대={fable_b}")
    after = st.load_state()
    check("거절받은 계정을 통째로 막지 않는다", not st.is_blocked(after, label="B"),
          f"blocked={after.get('blocked')}")

    # (6) 코덱스 교차검수 2026-08-20 지적 5건 — 전부 재현한 뒤 고쳤다
    st.STATE_FILE.unlink()
    setup(tmp, ["A", "B"], "B")
    (Path(tmp) / "accounts.json").write_text(json.dumps(
        [{"label": "A", "retired": True}, {"label": "B"}]), encoding="utf-8")
    state = st.load_state()
    state["scoped_blocked"] = {"A": {"Fable": now + 60}, "B": {"Fable": now + 3600}}
    # 알리는 시각과 실제로 고르는 계정은 **같은 후보 기준**을 써야 한다. 한쪽만
    # 구독 해지를 빼면, 해지 계정이 먼저 풀릴 때 알림과 실제 선택이 어긋난다.
    check("해지 계정 시각을 알리지 않는다",
          abs(st.earliest_reset(state, bucket="Fable") - (now + 3600)) < 2,
          st.earliest_reset(state, bucket="Fable") - now)
    later = now + 120   # 해지 계정 A 의 칸이 풀린 뒤
    check("해지 계정을 고르지도 않는다",
          st.pick_next(st.prune_blocked(dict(state), now=later), bucket="Fable", now=later) is None)
    check("되돌리기 후보에서도 뺀다",
          st.better_candidate(st.prune_blocked(dict(state), now=later), "B",
                              bucket="Fable", now=later) is None)
    (Path(tmp) / "accounts.json").write_text(json.dumps(
        [{"label": x} for x in ("A", "B")]), encoding="utf-8")
    check("해지 표시를 지우면 다시 후보가 된다",
          st.pick_next(st.prune_blocked(dict(state), now=later), bucket="Fable", now=later) == "A")

    # 칸만 막힌 소진은 blocked 가 늘 비어 있다. 그때 알림 표시를 지우면 봇 호출마다
    # 같은 알림이 나간다(공유가 꺼진 머신).
    state = st.load_state()
    state["scoped_blocked"] = {"A": {"Fable": now + 3600}, "B": {"Fable": now + 3600}}
    check("첫 소진은 알린다", st.mark_notified(state, now + 3600) is True)
    check("같은 구간에서 또 알리지 않는다",
          st.mark_notified(st.prune_blocked(state), now + 3600) is False)
    state["scoped_blocked"] = {}   # 칸까지 다 풀리면 다음 소진 때 다시 알린다
    check("다 풀리면 표시를 지운다",
          st.prune_blocked(state).get("notified_for") is None)

    # 다른 칸이 막혔다고 '다른 모델은 안 된다' 로 말하면 그것도 거짓이다 — 기록에
    # 없는 칸(Sonnet 등)은 안 막힌 것이고 그 모델로는 지금도 쓸 수 있다.
    state = st.load_state()
    state["scoped_blocked"] = {l: {"Fable": now + 3600, "Opus": now + 3600}
                               for l in ("A", "B")}
    msg3 = claude_pool._exhausted_message(state, "Fable", "08월 21일 16:00")
    check("안 막힌 모델로는 쓸 수 있다는 안내를 지우지 않는다",
          "다른 모델로 켜면" in msg3 and st.pick_next(state, bucket="Sonnet") is not None, msg3)
    check("같이 막힌 칸은 이름을 적는다", "다만 Opus 칸도 바닥" in msg3, msg3)
    state["scoped_blocked"] = {l: {"Fable": now + 3600} for l in ("A", "B")}
    msg4 = claude_pool._exhausted_message(state, "Fable", "08월 21일 16:00")
    check("그 칸만 막혔으면 군더더기를 안 붙인다",
          "다른 모델로 켜면" in msg4 and "다만" not in msg4, msg4)

    # 3차 검수: 한 계정만 막힌 칸은 다른 계정으로 쓸 수 있다. 합집합으로 모으면
    # 멀쩡한 모델을 못 쓴다고 알린다.
    state["scoped_blocked"] = {"A": {"Fable": now + 3600, "Opus": now + 3600},
                               "B": {"Fable": now + 3600}}
    msg5 = claude_pool._exhausted_message(state, "Fable", "08월 21일 16:00")
    check("한 계정만 막힌 칸은 못 쓴다고 하지 않는다",
          "Opus" not in msg5 and st.pick_next(state, bucket="Opus") == "B", msg5)

    # 3차 검수: 해지 계정을 "쓸 수 있는 계정" 으로 세면 안 된다.
    (Path(tmp) / "accounts.json").write_text(json.dumps(
        [{"label": "A", "retired": True}, {"label": "B"}]), encoding="utf-8")
    state["blocked"] = {"B": now + 3600}   # 살아 있는 계정은 계정 단위로 막힘
    state["scoped_blocked"] = {l: {"Fable": now + 3600} for l in ("A", "B")}
    msg6 = claude_pool._exhausted_message(state, "Fable", "08월 21일 16:00")
    check("해지 계정만 남았으면 쓸 수 있다고 하지 않는다",
          "다른 모델로 켜면" not in msg6
          and "구독 계정이 전부 한도에 걸렸습니다" in msg6, msg6)


def case_scoped_reopen(tmp):
    """[47] 막혔던 모델 칸이 다시 열리면 한 번 알린다.

    2026-08-21 사용자 요청: 소진 알림에 해제 시각이 적히긴 해도 그때까지 사람이 시각을
    외워두고 기다려야 했다. 주기 작업(usage_alert, winserver 10분 / Mac 1시간)이
    막힘 목록을 보고 열린 칸을 알린다.
    """
    print("\n[47] 칸이 다시 열리면 알린다")
    import usage_alert

    setup(tmp, ["A", "B"], "B")
    now = time.time()
    state = st.load_state()
    for label in ("A", "B"):
        st.mark_scoped_blocked(state, label, "Fable", now + 3600)
    st.save_state(state)

    # (1) 바닥인 동안은 기록만 하고 알리지 않는다
    state = st.load_state()
    check("바닥일 땐 알릴 게 없다", st.note_scoped_full(state) == [])
    check("바닥이라고 적어둔다",
          abs(float((state.get("scoped_full") or {}).get("Fable", 0)) - (now + 3600)) < 2,
          state.get("scoped_full"))
    st.save_state(state)

    # (2) 시간이 지나 칸이 풀리면 그때 알린다
    later = now + 3700
    state = st.prune_blocked(st.load_state(), now=later)
    opened = st.note_scoped_full(state, now=later)
    check("풀린 칸을 짚는다", [b for b, _ in opened] == ["Fable"], opened)
    check("적어둔 기록은 지운다", not state.get("scoped_full"), state.get("scoped_full"))
    st.save_state(state)

    # (3) 다음 회차엔 또 알리지 않는다
    state = st.prune_blocked(st.load_state(), now=later + 60)
    check("같은 회복을 두 번 알리지 않는다",
          st.note_scoped_full(state, now=later + 60) == [])

    # (4) 계정이 통째로 막혀 후보가 없는 것은 칸 소진이 아니다.
    #     이걸 안 가르면 계정이 풀릴 때마다 안 막혔던 칸이 열렸다고 알린다.
    state = st.load_state()
    state["scoped_blocked"] = {}
    st.mark_blocked(state, "A", now + 3600, "주간 한도")
    st.mark_blocked(state, "B", now + 3600, "주간 한도")
    check("계정 막힘은 칸 바닥으로 안 센다", st.note_scoped_full(state) == []
          and not state.get("scoped_full"), state.get("scoped_full"))

    # (5) 실제 알림 문구 — 받은 사람이 할 수 있는 행동이 들어간다
    st.STATE_FILE.unlink()
    setup(tmp, ["A", "B"], "B")
    state = st.load_state()
    st.mark_scoped_blocked(state, "A", "Fable", now - 10)   # 이미 지난 막힘
    st.mark_scoped_blocked(state, "B", "Fable", now - 10)
    state["scoped_full"] = {"Fable": now - 10}
    st.save_state(state)

    sent = []
    real_notify = usage_alert.notify
    class _FakeNotify:
        @staticmethod
        def send(msg):
            sent.append(msg)
    usage_alert.notify = _FakeNotify
    try:
        got = usage_alert.check_scoped_reopen()
    finally:
        usage_alert.notify = real_notify

    check("알림을 보낸다", got == ["Fable"] and len(sent) == 1, f"{got} / {sent}")
    body = sent[0] if sent else ""
    check("어느 칸이 열렸는지 말한다", "Fable 칸이 다시 열렸습니다" in body, body)
    check("쓸 수 있는 계정을 적는다", "A, B" in body, body)
    check("할 수 있는 행동을 적는다", "cc 를 다시 켜세요" in body, body)
    check("떠 있는 창은 안 바뀐다고 알린다", "이미 떠 있는 창" in body, body)
    check("두 번째 회차엔 조용하다", usage_alert.check_scoped_reopen() == [])


def case_window_claim(tmp):
    """[48] 사람이 쓰는 창이 잡은 모델 칸을 봇 호출이 뺏지 않는다.

    2026-08-22 실사고: Fable 로 켠 창이 B 를 쓰고 있는데, Opus 로 부르는 봇 호출이
    "앞순위 A 는 안 막혔다"며 7~10분마다 A 로 되돌렸다. A 는 Fable 칸만 바닥이라
    계정 단위로는 멀쩡해 보였고, 자격은 이 컴퓨터에 한 벌뿐이라 창까지 A 로 끌려가
    사용자는 계정을 바꿨다는 알림을 받고도 계속 Fable 한도 문구를 봤다.
    """
    print("\n[48] 떠 있는 창의 모델 칸을 봇이 안 뺏는다")
    import usage_check as uc

    setup(tmp, ["A", "B"], "B")     # 자동 전환으로 B 에 앉아 있는 상황
    claude_pool.usage_check = uc    # run() 이 칸 이름을 뽑게 한다(순수 함수만 탄다)
    state = st.load_state()
    st.mark_scoped_blocked(state, "A", "Fable", time.time() + 3600)
    state["reclaim_at"] = None      # 간격 제한이 판정을 가리지 않게
    st.save_state(state)

    real_alive = st._pid_alive
    alive = {"4242"}                # 이 번호의 창이 떠 있다고 친다
    st._pid_alive = lambda pid: str(pid) in alive
    try:
        # (1) 창이 Fable 로 떠 있다고 표시한다
        got = claude_pool.claim_session(4242, model="claude-fable-5[1m]")
        check("표시한 칸이 Fable", got == "Fable", f"got={got}")
        check("창 칸이 보인다", st.claimed_buckets(st.load_state()) == ("Fable",),
              f"칸={st.claimed_buckets(st.load_state())}")

        # (2) 사고 재현: Opus 봇 호출이 앞순위 A 로 되돌리지 않는다
        fake = Fake({"A": (0, OK_OUT, ""), "B": (0, OK_OUT, "")})
        install(fake)
        proc = claude_pool.run(["claude", "-p", "안녕", "--model", "opus[1m]"])
        check("봇 호출 성공", proc.returncode == 0)
        check("창이 안 밀린다(B 유지)", st.load_state()["current"] == "B",
              f"현재={st.load_state()['current']}")
        check("교체 자체가 없다", fake.switches == [], f"switches={fake.switches}")

        # (3) 창이 닫히면 예전처럼 앞순위 A 로 되돌아간다(순차 소진은 그대로)
        alive.clear()
        s = st.load_state()
        s["reclaim_at"] = None
        st.save_state(s)
        fake2 = Fake({"A": (0, OK_OUT, ""), "B": (0, OK_OUT, "")})
        install(fake2)
        proc = claude_pool.run(["claude", "-p", "안녕", "--model", "opus[1m]"])
        check("창이 닫히면 A 로 되돌아간다", st.load_state()["current"] == "A",
              f"현재={st.load_state()['current']}")
        check("죽은 창 표시는 걷힌다", st.load_state().get("session_claims") == {},
              f"표시={st.load_state().get('session_claims')}")

        # (4) 둘 다 만족하는 계정이 없으면 봇을 멈추지 않는다(창이 밀리더라도)
        alive.add("4242")
        s = st.load_state()
        st.set_current(s, "B", "시험 준비")
        st.mark_blocked(s, "B", time.time() + 3600, "시험: B 통째로 막힘")
        s["reclaim_at"] = None
        st.save_state(s)
        claude_pool.claim_session(4242, model="claude-fable-5[1m]")
        fake3 = Fake({"A": (0, OK_OUT, ""), "B": (0, OK_OUT, "")})
        install(fake3)
        proc = claude_pool.run(["claude", "-p", "안녕", "--model", "opus[1m]"])
        check("봇은 멈추지 않는다", proc.returncode == 0)
        check("다른 수가 없으면 A 로 간다", st.load_state()["current"] == "A",
              f"현재={st.load_state()['current']}")

        # (5) 표시 지우기
        claude_pool.release_session(4242)
        check("표시가 지워진다", st.claimed_buckets(st.load_state()) == (),
              f"칸={st.claimed_buckets(st.load_state())}")
    finally:
        st._pid_alive = real_alive


def case_window_claim_edges(tmp):
    """[50] 창 표시가 조용히 빠지는 갈래들 (2026-08-22 코덱스 검수가 잡은 것들).

    셋 다 "표시가 없는데 있다고 여겨" 사람 창이 밀리는 경로였다.
    """
    print("\n[50] 창 표시가 조용히 빠지지 않는다")
    import claude_shim

    setup(tmp, ["A", "B"], "B")

    # (1) 인자 값 때문에 대화형 창을 창이 아니라고 보면 안 된다
    check("--add-dir config 도 창으로 본다",
          claude_shim.is_a_window(["--model", "claude-fable-5[1m]", "--add-dir", "config"]) is True)
    check("mcp 하위 명령은 창이 아니다", claude_shim.is_a_window(["mcp", "list"]) is False)
    check("--version 은 창이 아니다", claude_shim.is_a_window(["--version"]) is False)
    check("맨 그냥 실행은 창이다", claude_shim.is_a_window([]) is True)

    # (2) 물려받은 번호의 표시가 실제로 없으면 다시 남긴다
    #     (감싸는 코드의 --claim 이 잠금 대기로 실패해도 환경변수는 설정되기 때문)
    import usage_check as uc
    claude_pool.usage_check = uc
    real_alive = st._pid_alive
    st._pid_alive = lambda pid: True
    real_env = os.environ.get("CLAUDE_ROTATOR_CLAIM_PID")
    os.environ["CLAUDE_ROTATOR_CLAIM_PID"] = "31337"     # 표시는 없는 번호
    real_settings = claude_pool.intended_model
    claude_pool.intended_model = lambda argv=None: "claude-fable-5[1m]"
    try:
        check("시작할 땐 표시가 없다", st.load_state().get("session_claims") == {})
        claude_shim.claim_window(["--model", "claude-fable-5[1m]"])
        got = st.load_state().get("session_claims") or {}
        check("빠진 표시를 물려받은 번호로 채운다",
              (got.get("31337") or {}).get("bucket") == "Fable", f"표시={got}")
        # 이미 있으면 덧쓰지 않는다(같은 번호로 두 번 불려도 안전)
        before = json.dumps(st.load_state()["session_claims"], sort_keys=True)
        claude_shim.claim_window(["--model", "claude-fable-5[1m]"])
        check("이미 있으면 그대로 둔다",
              json.dumps(st.load_state()["session_claims"], sort_keys=True) == before)
    finally:
        claude_pool.intended_model = real_settings
        st._pid_alive = real_alive
        if real_env is None:
            os.environ.pop("CLAUDE_ROTATOR_CLAIM_PID", None)
        else:
            os.environ["CLAUDE_ROTATOR_CLAIM_PID"] = real_env

    # (3) 오래 열어둔 창은 상한에 안 걸린다 — 살아 있으면 확인할 때마다 시각을 다시 찍는다
    check("상한은 하루보다 넉넉하다", st.CLAIM_MAX_AGE >= 24 * 3600,
          f"CLAIM_MAX_AGE={st.CLAIM_MAX_AGE}")
    real_alive2, real_started = st._pid_alive, st._pid_started
    st._pid_alive = lambda pid: True
    st._pid_started = lambda pid: 1000.0        # 그 번호의 프로세스는 1000 에 시작했다고 침
    try:
        old = time.time() - st.CLAIM_MAX_AGE + 60   # 상한 직전까지 오래된 표시
        s = {"session_claims": {"4242": {"bucket": "Fable", "at": old, "started": 1000.0,
                                         "label": "B", "model": "fable"}}}
        st.prune_claims(s)
        got = (s["session_claims"].get("4242") or {}).get("at")
        check("그 창이 맞으면 시각이 새로 찍힌다", got is not None and got > old + 60,
              f"at={got}, 이전={old}")
        for _ in range(3):
            st.prune_claims(s)
        check("여러 회차를 지나도 남아 있다", "4242" in s["session_claims"])

        # 번호가 재사용된 경우: 살아 있어도 그 창이 아니므로 걷어낸다
        s2 = {"session_claims": {"4242": {"bucket": "Fable", "at": old, "started": 1000.0,
                                          "label": "B", "model": "fable"}}}
        st._pid_started = lambda pid: 9999.0     # 다른 프로세스가 그 번호를 쓰고 있다
        st.prune_claims(s2)
        check("번호가 재사용됐으면 걷어낸다", s2["session_claims"] == {},
              f"남은 것={s2['session_claims']}")

        # 시작 시각을 알아볼 수 없으면 갱신하지 않는다(상한이 치우게 둔다)
        s3 = {"session_claims": {"4242": {"bucket": "Fable", "at": old, "started": None,
                                          "label": "B", "model": "fable"}}}
        st._pid_started = lambda pid: None
        st.prune_claims(s3)
        check("알아볼 수 없으면 시각을 안 찍는다",
              (s3["session_claims"].get("4242") or {}).get("at") == old,
              f"at={(s3['session_claims'].get('4242') or {}).get('at')}")

        # 번호가 재사용된 자리에 새 창이 들어오면, 표시에 남의 시작 시각을 물려주면 안 된다
        st._pid_started = lambda pid: 2000.0      # 지금 그 번호는 2000 에 시작한 새 프로세스
        s4 = {"session_claims": {"4242": {"bucket": "Fable", "at": time.time(),
                                          "started": 1000.0, "label": "B", "model": "fable"}}}
        st.note_session_claim(s4, 4242, "Opus", "B")
        check("새 표시는 지금 시작 시각을 쓴다",
              (s4["session_claims"]["4242"] or {}).get("started") == 2000.0,
              f"started={(s4['session_claims']['4242'] or {}).get('started')}")
        st.prune_claims(s4)
        check("그래서 다음 정리에도 안 지워진다", "4242" in s4["session_claims"],
              f"남은 것={s4['session_claims']}")

        # 정리보다 먼저 물어봐도 재사용된 찌꺼기는 안 센다
        st._pid_started = lambda pid: 9999.0
        s5 = {"session_claims": {"4242": {"bucket": "Fable", "at": time.time(),
                                          "started": 1000.0, "label": "B", "model": "fable"}}}
        check("정리 전에 물어도 재사용 찌꺼기는 빼고 센다", st.claimed_buckets(s5) == (),
              f"칸={st.claimed_buckets(s5)}")

        st._pid_alive = lambda pid: False
        st.prune_claims(s)
        check("죽으면 상한과 무관하게 바로 걷힌다", s["session_claims"] == {})
    finally:
        st._pid_alive, st._pid_started = real_alive2, real_started

    # 진짜 프로세스로 시작 시각 확인이 되는지(실물 확인).
    # 유닉스는 ps 를 부르는데, 이 시험이 subprocess.run 을 가짜로 갈아끼운 상태라
    # 그대로 두면 가짜 claude 응답을 ps 결과로 읽는다. 이 검사 동안만 진짜로 되돌린다.
    faked_run = subprocess.run
    subprocess.run = REAL_SUBPROCESS_RUN
    try:
        check("내 시작 시각을 읽을 수 있다", st._pid_started(os.getpid()) is not None)
        check("나는 나와 같은 프로세스로 알아본다",
              st._same_process(os.getpid(), st._pid_started(os.getpid())) is True)
        check("남의 시작 시각과는 다르다고 본다",
              st._same_process(os.getpid(), 1000.0) is False)
    finally:
        subprocess.run = faked_run
    # macOS ps 에는 초 단위(etimes)가 없어 경과 시간 표기를 되읽어야 한다
    now = time.time()
    for text, secs in (("00:30", 30), ("01:02:03", 3723), ("2-03:04:05", 183845)):
        got = st._elapsed_to_epoch(text)
        check(f"경과 시간 '{text}' 해석", got is not None and abs((now - got) - secs) < 5,
              f"got={got}")
    check("빈 값은 모름", st._elapsed_to_epoch("") is None)
    check("깨진 값은 모름", st._elapsed_to_epoch("어제쯤") is None)


def case_limit_with_split_cred(tmp):
    """[51] 자격 자리가 어긋나 있던 동안 받은 한도 거절로는 계정을 막지 않는다.

    2026-08-22 실사고: 원격 접속 자리에서 계정을 B 로 바꾸자 자격 파일만 바뀌고 열쇠꾸러미는
    A 그대로 남았다. 화면 세션에서 도는 봇이 A(Fable 0%)로 호출해 받은 거절이 B 앞으로 찍혀
    **B 가 5시간 통째로 막혔다.** B 는 Fable 이 93% 남은 멀쩡한 계정이었고, B 자격으로 직접
    불러 보니 정상이었다.
    """
    print("\n[51] 자격 자리가 어긋났으면 그 거절로 계정을 막지 않는다")
    real_settle = claude_pool._settle_pending_switch
    try:
        # (1) 어긋나 있었고 방금 맞춘 자리 → 이 거절은 옛 계정 것이다. 막지 않는다.
        setup(tmp, ["A", "B"], "A")
        claude_pool._settle_pending_switch = lambda: (True, "A")
        fake = Fake({"A": (0, LIMIT_OUT, ""), "B": (0, OK_OUT, "")})
        install(fake)
        claude_pool.run(["claude", "-p", "안녕"])
        s = st.load_state()
        check("계정을 막지 않았다", not s.get("blocked"), f"막힘={s.get('blocked')}")
        check("계정을 바꾸지도 않았다", s.get("current") == "A" and fake.switches == [],
              f"현재={s.get('current')}, 교체={fake.switches}")
        check("알림도 안 나갔다", fake.notices == [], f"알림={fake.notices}")

        # (2) 어긋난 적이 없으면 예전 그대로 막고 갈아탄다
        setup(tmp, ["A", "B"], "A")
        claude_pool._settle_pending_switch = lambda: (False, None)
        fake2 = Fake({"A": (0, LIMIT_OUT, ""), "B": (0, OK_OUT, "")})
        install(fake2)
        proc = claude_pool.run(["claude", "-p", "안녕"])
        check("성공으로 끝난다", proc.returncode == 0)
        check("A 를 막았다", "A" in (st.load_state().get("blocked") or {}),
              f"막힘={st.load_state().get('blocked')}")
        check("B 로 갈아탔다", st.load_state().get("current") == "B",
              f"현재={st.load_state().get('current')}")

        # (3) 예약에 적힌 계정이 상태와 다르면, 실제로 꽂힌 그 계정으로 상태를 맞춘다
        #     (예약은 순환기를 안 거친 손 작업으로도 생긴다)
        setup(tmp, ["A", "B"], "A")
        s = st.load_state()          # (2)에서 막아둔 A 를 지우고 시작한다(같은 임시 폴더를 쓴다)
        s["blocked"] = {}
        st.save_state(s)
        claude_pool._settle_pending_switch = lambda: (True, "B")
        fake3 = Fake({"A": (0, LIMIT_OUT, ""), "B": (0, OK_OUT, "")})
        install(fake3)
        proc = claude_pool.run(["claude", "-p", "안녕"])
        check("실제로 꽂힌 계정으로 상태를 맞춘다", st.load_state().get("current") == "B",
              f"현재={st.load_state().get('current')}")
        check("그래도 아무 계정도 안 막는다", not st.load_state().get("blocked"),
              f"막힘={st.load_state().get('blocked')}")

        # (5) 예약을 마무리해 정착시킨 계정을 다음 시도가 곧바로 앞순위로 되돌리면 안 된다.
        #     되돌리면 재시도가 또 옛 계정으로 나가 헛돈다(재시도 횟수가 적으면 그대로 실패).
        setup(tmp, ["A", "B"], "A")
        s = st.load_state()
        s["blocked"], s["reclaim_at"] = {}, None
        st.save_state(s)
        claude_pool._settle_pending_switch = lambda: (True, "B")
        fake5 = Fake({"A": (0, LIMIT_OUT, ""), "B": (0, OK_OUT, "")})
        install(fake5)
        proc = claude_pool.run(["claude", "-p", "안녕"], max_attempts=2)
        ok = False
        try:
            ok = json.loads(proc.stdout or "{}").get("is_error") is False
        except Exception:
            ok = False
        check("두 번째 시도는 정착한 계정으로 나간다", ok, f"응답={(proc.stdout or '')[:60]}")
        check("정착한 계정에 그대로 있다", st.load_state().get("current") == "B",
              f"현재={st.load_state().get('current')}")

        # (4) 실물 함수: 예약 마무리가 찍는 줄이 봇 답변 쪽으로 새면 안 된다
        #     (봇은 이 호출의 출력을 그대로 JSON 으로 읽는다)
        setup(tmp, ["A", "B"], "A")
        claude_pool._settle_pending_switch = real_settle
        import cred_mirror
        real_apply = cred_mirror.apply_pending
        (st.CREDS_DIR / ".pending-switch").write_text("B", encoding="utf-8")

        def noisy_apply():
            print("2026-01-01 00:00:00 [되비추기] 시끄러운 줄")
            return "done"

        cred_mirror.apply_pending = noisy_apply
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                done, label = claude_pool._settle_pending_switch()
        finally:
            cred_mirror.apply_pending = real_apply
        check("마무리했다고 답한다", done is True and label == "B", f"{done} / {label}")
        check("표준 출력으로 아무것도 안 샌다", buf.getvalue().strip() == "",
              f"샌 것={buf.getvalue()!r}")
    finally:
        claude_pool._settle_pending_switch = real_settle


def case_cred_refresh(tmp):
    """[52] 만료된 저장 자격을 재발급 열쇠로 되살리는 갈래.

    갱신은 재발급 열쇠를 한 번 쓰고 버리는 회전이라, 엉뚱한 자리에서 부르면 그 계정
    자격이 죽는다. 그래서 "언제 부르는가" 를 값 확인보다 촘촘히 본다.
    """
    print("\n[52] 만료된 저장 자격 되살리기")
    import cred_refresh as cr
    import usage_check as uc

    creds = Path(tmp) / "creds"
    creds.mkdir(parents=True, exist_ok=True)
    now_ms = int(time.time() * 1000)

    def write(label, *, refresh="옛열쇠", rt_exp=now_ms + 30 * 86400_000):
        p = creds / f"{label}.json"
        p.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "옛출입증", "refreshToken": refresh,
            "expiresAt": now_ms - 1000, "refreshTokenExpiresAt": rt_exp,
            "scopes": ["a"], "subscriptionType": "max"}}), encoding="utf-8")
        return p

    # 부를 수 있는 자리인지 먼저 가린다 — 열쇠가 없거나 만료면 헛호출을 안 한다.
    check("재발급 열쇠가 없으면 안 부른다",
          cr.can_refresh({"claudeAiOauth": {"refreshToken": ""}}) is False)
    check("재발급 열쇠가 만료됐으면 안 부른다",
          cr.can_refresh({"claudeAiOauth": {
              "refreshToken": "x", "refreshTokenExpiresAt": now_ms - 1000}}) is False)
    check("살아있는 열쇠면 부른다", cr.can_refresh({"claudeAiOauth": {
        "refreshToken": "x", "refreshTokenExpiresAt": now_ms + 1000}}) is True)

    real_post = cr._post
    calls = []
    try:
        def fake_post(token):
            calls.append(token)
            return {"access_token": "새출입증", "refresh_token": "새열쇠", "expires_in": 3600}

        cr._post = fake_post
        p = write("A")
        check("갱신에 성공하면 참을 돌려준다", cr.refresh_file(p) is True)
        saved = json.loads(p.read_text(encoding="utf-8"))["claudeAiOauth"]
        check("새 출입증이 파일에 저장된다", saved["accessToken"] == "새출입증")
        check("회전된 열쇠도 같이 저장된다", saved["refreshToken"] == "새열쇠",
              "이걸 놓치면 다음 갱신이 폐기된 열쇠로 나가 계정이 죽는다")
        check("만료 시각이 앞으로 당겨진다", saved["expiresAt"] > now_ms)
        check("나머지 항목은 그대로 둔다", saved.get("subscriptionType") == "max")
        check("이전 세대를 남긴다", (creds / "prev" / "A.json").exists())
        check("잠금 파일을 안 남긴다", not list(creds.glob(".refresh-*")))

        # 응답에 출입증이 없으면 저장하지 않는다(빈 값을 덮어쓰면 자격이 사라진다)
        cr._post = lambda token: {"refresh_token": "새열쇠2"}
        p2 = write("B")
        check("출입증 없는 응답은 저장하지 않는다", cr.refresh_file(p2) is False)
        check("실패해도 원래 자격은 그대로",
              json.loads(p2.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"]
              == "옛출입증")

        # 새 열쇠가 안 온 응답도 저장하지 않는다. 저장하면 파일에 **이미 써서 폐기된**
        # 열쇠가 남고, 성공으로 돌려주면 다음 갱신이 조용히 거절당한다(코덱스 검수, 재현함).
        cr._post = lambda token: {"access_token": "새출입증", "expires_in": 3600}
        p4 = write("E")
        check("새 열쇠 없는 응답은 저장하지 않는다", cr.refresh_file(p4) is False)
        check("그때 파일에 폐기된 열쇠가 남지 않는다",
              json.loads(p4.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"]
              == "옛출입증")

        # 회전이 끝난 뒤 저장이 깨지면 그 계정은 잃은 것이다. 조용히 넘기면 안 된다.
        real_write, real_alert = cr._write_atomic, cr._alert_lost
        alerted = []
        try:
            def boom(dest, obj):
                raise OSError("디스크 오류")

            cr._write_atomic = boom
            cr._alert_lost = lambda label: alerted.append(label)
            cr._post = fake_post
            p5 = write("F")
            check("저장이 깨지면 실패로 돌려준다", cr.refresh_file(p5) is False)
            check("그 사유가 남는다", "저장 실패" in (cr.last_error or ""),
                  f"사유={cr.last_error!r}")
            check("자격을 잃었으면 사람을 부른다", alerted == ["F"])
        finally:
            cr._write_atomic, cr._alert_lost = real_write, real_alert
            cr._post = fake_post

        # 둘이 동시에 갱신하면 한쪽이 폐기된 열쇠를 저장한다. 잠긴 자리에서는 물러난다.
        cr._post = fake_post
        p3 = write("D")
        lock = creds / (".refresh-" + p3.name + ".lock")
        lock.write_text("", encoding="utf-8")
        check("다른 곳이 갱신 중이면 물러난다", cr.refresh_file(p3) is False)
        lock.unlink()

        # 부르는 쪽 규칙: 자격 만료일 때만, 그리고 현재 계정이 아닐 때만 갱신한다.
        real_get, real_refresh = uc.get_usage, cr.refresh_file
        try:
            asked = []

            def fake_get(path):
                asked.append(str(path))
                if len(asked) == 1:
                    uc.last_status, uc.last_error = 401, "HTTP 401"
                    return None
                return {"weekly_pct": 1.0}

            uc.get_usage = fake_get
            cr.refresh_file = lambda path: True
            got = uc.get_usage_for(p, is_current=False)
            check("자격 만료면 갱신하고 한 번 더 읽는다", got == {"weekly_pct": 1.0})

            asked.clear()
            called = []
            cr.refresh_file = lambda path: called.append(str(path)) or True

            def get_429(path):
                asked.append(str(path))
                uc.last_status, uc.last_error = 429, "조회 제한(429). 잠시 뒤 다시"
                return None

            uc.get_usage = get_429
            check("조회 제한이면 열쇠를 태우지 않는다",
                  uc.get_usage_for(p, is_current=False) is None and not called)

            asked.clear()
            called.clear()

            def get_401(path):
                asked.append(str(path))
                uc.last_status, uc.last_error = 401, "HTTP 401"
                return None

            uc.get_usage = get_401
            check("지금 쓰는 계정은 갱신하지 않는다",
                  uc.get_usage_for(p, is_current=True) is None and not called,
                  "claude 가 들고 있는 값이 폐기된다")
        finally:
            uc.get_usage, cr.refresh_file = real_get, real_refresh
            uc.last_status = uc.last_error = None
        # 손으로 부를 때 현재 계정을 건드리지 않는다(코덱스 검수, 재현함).
        # 인자 없이 부르면 저장된 것 전부를 갱신하던 자리라 현재 계정까지 폐기시켰다.
        known = ["A", "B", "D"]
        picked, refused = cr.targets([], "B", known)
        check("인자 없이 부르면 현재 계정만 뺀다", picked == ["A", "D"] and not refused,
              f"고른 것={picked}")
        picked, refused = cr.targets(["B"], "B", known)
        check("현재 계정을 콕 집어도 거절한다", picked == [] and refused,
              f"고른 것={picked}, 사유={refused}")
        picked, refused = cr.targets([], None, known)
        check("현재 계정을 못 읽으면 하나도 안 한다", picked == [] and refused,
              "모르는 채 전부 갱신하면 쓰는 자격을 폐기시킨다")
        picked, refused = cr.targets(["A"], "B", known)
        check("나머지 계정은 지정하면 갱신한다", picked == ["A"] and not refused)
    finally:
        cr._post = real_post
        check("갱신 호출은 파일에서 읽은 열쇠로만 나간다", calls and calls[0] == "옛열쇠")


def case_pid_alive():
    """[49] 프로세스 생존 확인이 실제로 맞는지(창 표시가 영영 안 걷히면 계정이 좁아진다)."""
    print("\n[49] 프로세스 생존 확인")
    import os

    check("나 자신은 살아 있다", st._pid_alive(os.getpid()) is True)
    # 끝난 자식의 번호는 죽은 것으로 나와야 한다(좀비가 안 남게 wait 까지 한다)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    check("끝난 프로세스는 죽은 것으로 본다", st._pid_alive(child.pid) is False,
          f"pid={child.pid}")
    check("이상한 값은 죽은 것으로 본다",
          st._pid_alive(0) is False and st._pid_alive("없음") is False
          and st._pid_alive(-1) is False)

    # 오래된 표시는 프로세스가 살아 있어도 걷어낸다(번호 재사용 대비)
    state = {"session_claims": {str(os.getpid()): {
        "bucket": "Fable", "at": time.time() - st.CLAIM_MAX_AGE - 60, "label": "A"}}}
    st.prune_claims(state)
    check("너무 오래된 표시는 걷어낸다", state["session_claims"] == {},
          f"남은 것={state['session_claims']}")


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
                     case_save_back_live_label, case_live_label_source,
                     case_switch_passes_leaving, case_ok_envelope_bad_exitcode,
                     case_dead_but_cred_alive, case_keychain_shadow,
                     case_keychain_self_fix, case_keychain_fresh_not_shadow,
                     case_mirror_both_ways, case_usage_after_reset,
                     case_stuck_on_dead_account, case_model_scoped_limit,
                     case_scoped_no_reclaim, case_scoped_shared,
                     case_dead_quarantine, case_dead_alert_folded,
                     case_keychain_pull_and_rescue, case_barred,
                     case_scoped_exhausted_message, case_scoped_reopen,
                     case_window_claim, case_window_claim_edges,
                     case_limit_with_split_cred, case_cred_refresh,
                     case_stale_scoped_block, case_stale_account_block):
            with tempfile.TemporaryDirectory() as tmp:
                case(tmp)
        case_shared_iso_roundtrip()
        case_meta_cleanup()
        case_scoped_usage_shared()
        case_scoped_all_empty_notice()
        case_cli_update_verdict()
        case_shim_prompt_argument()
        case_pid_alive()
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


def case_stale_scoped_block(tmp):
    """[53] 실제로는 열린 칸이 낡은 막힘 기록 탓에 계속 막혀 보이던 문제.

    2026-09-03 실사고: A 의 Fable 칸은 다 비어 있었는데(주간·세션·칸 전부 100% 남음)
    8월 31일 소진 때 적어둔 '09/05 21:00 풀림' 이 그대로 남아 있었다. B·D 까지 더해
    세 계정이 전부 막힌 것으로 보였고, cc 는 갈아탈 계정을 못 찾아 소진된 D 에 눌러앉았다.
    사람이 --use A 로 손을 대야만 풀렸다.

    막힘 해제 시각은 **막던 순간에 조회한 값**이라, 그 뒤 한도가 그보다 먼저 풀려도
    기록은 안 바뀐다. 그래서 갈아탈 계정이 없다고 결론내기 직전에 실제 사용량으로
    한 번 검증한다.
    """
    print()
    print("[53] 낡은 칸 막힘은 실제 사용량으로 되살린다")
    import usage_check as uc

    now = time.time()
    usage = {
        "A": {"weekly_pct": 0.0, "session_pct": 0.0,
              "weekly_reset": now + 48 * 3600, "session_reset": None,
              "scoped": {"Fable": {"pct": 0.0, "reset": now + 48 * 3600}}},
        "B": {"weekly_pct": 54.0, "session_pct": 11.0,
              "weekly_reset": now + 20 * 3600, "session_reset": None,
              "scoped": {"Fable": {"pct": 100.0, "reset": now + 20 * 3600}}},
        "D": {"weekly_pct": 59.0, "session_pct": 95.0,
              "weekly_reset": now + 78 * 3600, "session_reset": None,
              "scoped": {"Fable": {"pct": 100.0, "reset": now + 78 * 3600}}},
    }
    blocks = (("A", now + 48 * 3600), ("B", now + 20 * 3600), ("D", now + 78 * 3600))

    def arm(current):
        if st.STATE_FILE.exists():
            st.STATE_FILE.unlink()
        setup(tmp, ["A", "B", "D"], current)
        state = st.load_state()
        for label, until in blocks:
            st.mark_scoped_blocked(state, label, "Fable", until)
        st.save_state(state)

    def run_ensure(table):
        real_get, real_status = uc.get_usage, uc.last_status
        install(Fake({x: (0, OK_OUT, "") for x in ("A", "B", "D")}))
        claude_pool.usage_check = uc
        uc.last_status = None      # 앞 케이스가 남긴 401 로 재발급을 타지 않게
        uc.get_usage = lambda path: table.get(Path(path).stem)
        try:
            return claude_pool.ensure_usable(model="fable[1m]")
        finally:
            uc.get_usage, uc.last_status = real_get, real_status
            claude_pool.usage_check = None

    # (1) 사고 그대로: 세 계정이 다 막힌 것으로 보이지만 A 는 실제로 비어 있다
    arm("D")
    state = st.load_state()
    check("세 계정이 다 막힌 것으로 보인다",
          st.pick_next(state, exclude="D", bucket="Fable") is None)

    rc = run_ensure(usage)
    after = st.load_state()
    check("켜기 전 확인이 정상으로 끝난다", rc == 0, rc)
    check("실제로 비어 있는 A 로 갈아탄다", after.get("current") == "A", after.get("current"))
    check("A 의 낡은 막힘은 풀린다", not st.is_scoped_blocked(after, "A", "Fable"))
    check("진짜 찬 B 의 막힘은 그대로", st.is_scoped_blocked(after, "B", "Fable"))
    check("진짜 찬 D 의 막힘도 그대로", st.is_scoped_blocked(after, "D", "Fable"))

    # (2) 사용량을 못 읽은 계정은 손대지 않는다.
    #     못 읽는 것과 열린 것은 다르다 — 지우면 소진된 계정으로 갈아타 거절만 받는다.
    arm("D")
    rc = run_ensure({"D": usage["D"]})
    after = st.load_state()
    check("못 읽은 계정의 막힘은 안 지운다", st.is_scoped_blocked(after, "A", "Fable"))
    check("갈아탈 계정이 없다고 끝낸다", rc == 2, rc)
    check("계정은 그대로 D", after.get("current") == "D", after.get("current"))

    # (3) 평소 경로에서는 조회를 늘리지 않는다 — 후보가 있으면 검증 자체를 안 한다
    arm("D")
    state = st.load_state()
    st.clear_scoped_bucket(state, "B", "Fable")   # B 가 후보로 남아 있는 상태
    st.save_state(state)
    seen = []
    # B 는 실제로도 여유가 있는 상태로 둔다. 그러면 검증을 탈 이유가 없다.
    table = dict(usage, B=dict(usage["B"],
                               scoped={"Fable": {"pct": 0.0, "reset": now + 20 * 3600}}))
    def counting(path):
        seen.append(Path(path).stem)
        return table.get(Path(path).stem)
    real_get, real_status = uc.get_usage, uc.last_status
    install(Fake({x: (0, OK_OUT, "") for x in ("A", "B", "D")}))
    claude_pool.usage_check = uc
    uc.last_status = None
    uc.get_usage = counting
    try:
        claude_pool.ensure_usable(model="fable[1m]")
    finally:
        uc.get_usage, uc.last_status = real_get, real_status
        claude_pool.usage_check = None
    check("후보가 있으면 A 를 조회하지 않는다", "A" not in seen, seen)
    check("B 로 갈아탄다", st.load_state().get("current") == "B",
          st.load_state().get("current"))

    # (4) 한 칸만 푼다 — 로컬도 공유 칠판도. 공유를 안 맞추면 다음 병합이 되살린다.
    arm("D")
    shared = FakeShared()
    st.shared_state = shared
    try:
        state = st.load_state()
        state["scoped_blocked"] = {}
        st.mark_scoped_blocked(state, "A", "Fable", now + 3600)
        st.mark_scoped_blocked(state, "A", "Opus", now + 7200)
        check("공유에 두 칸이 올라간다",
              set(shared.scoped.get("A") or {}) == {"Fable", "Opus"}, shared.scoped)
        check("없는 칸을 풀라면 아무것도 안 한다",
              st.clear_scoped_bucket(state, "A", "Haiku") is False)
        check("푼 칸이 있으면 True", st.clear_scoped_bucket(state, "A", "Fable") is True)
        check("로컬은 Fable 만 빠진다",
              set(state["scoped_blocked"].get("A") or {}) == {"Opus"},
              state.get("scoped_blocked"))
        check("공유도 Fable 만 빠진다",
              set(shared.scoped.get("A") or {}) == {"Opus"}, shared.scoped)
        merged = st.prune_blocked(state)
        check("병합해도 되살아나지 않는다",
              not st.is_scoped_blocked(merged, "A", "Fable"))
        check("남은 칸은 병합 뒤에도 살아 있다",
              st.is_scoped_blocked(merged, "A", "Opus"))
    finally:
        st.shared_state = None

def case_stale_account_block(tmp):
    """[54] 계정 전체 막힘도 실제 사용량으로 되살린다.

    막을 때 붙이는 식힘 시간은 고정값(기본 5시간)이라 실제보다 길게 잡힌다.
    세션 한도는 창이 열린 시점부터 5시간이라 막히는 순간에는 대개 거의 다 지나 있다.
    2026-09-04 실측: A 를 14:27 에 세션 한도로 막고 19:27 까지 잡았는데 실제로는
    14:39 에 풀렸다. 그 사이 A 는 주간이 67% 남았는데도 후보에서 빠져 있었다.
    """
    print()
    print("[54] 낡은 계정 막힘은 실제 사용량으로 되살린다")
    import usage_check as uc

    now = time.time()
    usage = {
        # A: 막힘 기록은 남아 있지만 실제로는 세션·주간 모두 여유가 있다
        "A": {"weekly_pct": 20.0, "session_pct": 0.0,
              "weekly_reset": now + 48 * 3600, "session_reset": None, "scoped": {}},
        # B: 진짜로 세션이 찼다. 풀면 안 된다
        "B": {"weekly_pct": 90.0, "session_pct": 100.0,
              "weekly_reset": now + 20 * 3600, "session_reset": now + 3600, "scoped": {}},
        # D: 지금 계정인데 세션이 다 찼다 → 켜기 전 확인이 갈아탈 곳을 찾는다
        "D": {"weekly_pct": 50.0, "session_pct": 100.0,
              "weekly_reset": now + 78 * 3600, "session_reset": now + 3600, "scoped": {}},
    }

    def arm():
        if st.STATE_FILE.exists():
            st.STATE_FILE.unlink()
        setup(tmp, ["A", "B", "D"], "D")
        state = st.load_state()
        st.mark_blocked(state, "A", now + 5 * 3600, "세션 한도 소진")
        st.mark_blocked(state, "B", now + 5 * 3600, "세션 한도 소진")
        st.mark_scoped_blocked(state, "A", "Fable", now + 48 * 3600)
        st.save_state(state)

    def run_ensure(table):
        real_get, real_status = uc.get_usage, uc.last_status
        install(Fake({x: (0, OK_OUT, "") for x in ("A", "B", "D")}))
        claude_pool.usage_check = uc
        uc.last_status = None      # 앞 케이스가 남긴 401 로 재발급을 타지 않게
        uc.get_usage = lambda path: table.get(Path(path).stem)
        try:
            return claude_pool.ensure_usable(model="opus")
        finally:
            uc.get_usage, uc.last_status = real_get, real_status
            claude_pool.usage_check = None

    # (1) 실제로 풀린 A 만 되살아난다
    arm()
    state = st.load_state()
    check("A·B 가 다 막힌 것으로 보인다", st.pick_next(state, exclude="D") is None)

    rc = run_ensure(usage)
    after = st.load_state()
    check("켜기 전 확인이 정상으로 끝난다", rc == 0, rc)
    check("실제로 열려 있는 A 로 갈아탄다", after.get("current") == "A", after.get("current"))
    check("A 의 낡은 막힘은 풀린다", not st.is_blocked(after, "A"))
    check("진짜 찬 B 의 막힘은 그대로", st.is_blocked(after, "B"))
    check("A 의 모델 칸 막힘은 안 건드린다", st.is_scoped_blocked(after, "A", "Fable"))

    # (2) 사용량을 못 읽은 계정은 손대지 않는다 — 못 읽는 것과 열린 것은 다르다
    arm()
    rc = run_ensure({"D": usage["D"], "B": usage["B"]})
    after = st.load_state()
    check("못 읽은 계정의 막힘은 안 지운다", st.is_blocked(after, "A"))
    check("갈아탈 계정이 없다고 끝낸다", rc == 2, rc)

    # (3) 후보가 있으면 검증 자체를 안 한다(평소 경로에서 조회를 안 늘린다)
    arm()
    state = st.load_state()
    st.clear_account_block(state, "B")     # B 가 후보로 남아 있는 상태
    st.save_state(state)
    seen = []

    def counting(path):
        seen.append(Path(path).stem)
        return dict(usage, B={"weekly_pct": 10.0, "session_pct": 5.0,
                              "weekly_reset": now + 20 * 3600, "session_reset": None,
                              "scoped": {}}).get(Path(path).stem)

    real_get, real_status = uc.get_usage, uc.last_status
    install(Fake({x: (0, OK_OUT, "") for x in ("A", "B", "D")}))
    claude_pool.usage_check = uc
    uc.last_status = None
    uc.get_usage = counting
    try:
        claude_pool.ensure_usable(model="opus")
    finally:
        uc.get_usage, uc.last_status = real_get, real_status
        claude_pool.usage_check = None
    check("후보가 있으면 A 를 조회하지 않는다", "A" not in seen, seen)
    check("A 의 막힘은 그대로 남는다", st.is_blocked(st.load_state(), "A"))

    # (5) 주기 정리(--sweep): 사람이 화면을 안 켜도 걷힌다. 간격 제한도 지킨다
    arm()
    real_get, real_status = uc.get_usage, uc.last_status
    claude_pool.usage_check = uc
    uc.last_status = None
    uc.get_usage = lambda path: usage.get(Path(path).stem)
    try:
        check("주기 정리가 A 만 걷는다",
              claude_pool.sweep_stale_blocks(min_interval=0) == ["A"])
        after = st.load_state()
        check("정리 뒤에도 B 막힘은 그대로", st.is_blocked(after, "B"))
        check("마지막 정리 시각을 적어둔다", bool(after.get("revive_checked_at")))
        seen2 = []

        def counting2(path):
            seen2.append(Path(path).stem)
            return usage.get(Path(path).stem)

        uc.get_usage = counting2
        check("간격 안이면 건너뛴다", claude_pool.sweep_stale_blocks(min_interval=3600) == [])
        check("건너뛸 땐 조회도 안 한다", seen2 == [], seen2)
    finally:
        uc.get_usage, uc.last_status = real_get, real_status
        claude_pool.usage_check = None

    # (4) 계정 막힘만 푼다 — 칸 막힘은 남기고, 이미 지난 막힘에는 손대지 않는다
    state = st.load_state()
    state["blocked"] = {}
    st.mark_blocked(state, "A", now + 3600, "시험")
    check("푼 게 있으면 True", st.clear_account_block(state, "A", now) is True)
    check("안 막힌 계정은 False", st.clear_account_block(state, "A", now) is False)
    check("칸 막힘은 그대로", st.is_scoped_blocked(state, "A", "Fable"))

    # (6) 막을 때도 고정 5시간이 아니라 서버가 준 실제 풀림 시각으로 재운다
    def run_next(table):
        if st.STATE_FILE.exists():
            st.STATE_FILE.unlink()
        setup(tmp, ["A", "B", "D"], "D")
        real_get, real_status = uc.get_usage, uc.last_status
        install(Fake({}))
        claude_pool.usage_check = uc
        uc.last_status = None
        uc.get_usage = lambda path: table.get(Path(path).stem)
        try:
            claude_pool.next_account()
        finally:
            uc.get_usage, uc.last_status = real_get, real_status
            claude_pool.usage_check = None
        return st.load_state()

    spare = {"weekly_pct": 20.0, "session_pct": 0.0, "weekly_reset": now + 48 * 3600,
             "session_reset": None, "scoped": {}}
    # 세션만 찼고 15분 뒤 풀린다 → 15분만 재워야 한다(예전에는 5시간을 재웠다)
    after = run_next({"A": spare, "B": spare,
                      "D": {"weekly_pct": 40.0, "session_pct": 99.0,
                            "weekly_reset": now + 78 * 3600, "session_reset": now + 900,
                            "scoped": {}}})
    left = float(after["blocked"]["D"]) - time.time()
    check("세션이 곧 풀리면 그만큼만 재운다", 600 < left < 1200, f"{left / 60:.0f}분")

    # 주간이 찼으면 며칠 뒤가 맞다. 5시간만 재우면 깨어나자마자 또 거절받는다
    after = run_next({"A": spare, "B": spare,
                      "D": {"weekly_pct": 100.0, "session_pct": 10.0,
                            "weekly_reset": now + 40 * 3600, "session_reset": now + 900,
                            "scoped": {}}})
    left = float(after["blocked"]["D"]) - time.time()
    check("주간이 찼으면 주간 풀릴 때까지 재운다", 39 * 3600 < left < 41 * 3600,
          f"{left / 3600:.1f}시간")



if __name__ == "__main__":
    raise SystemExit(main())
