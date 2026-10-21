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
import switch_account as sa  # noqa: E402

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
    real_lbl = sa.label_of_email
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
        sa.cred_email = lambda path=None: "d@test"
        sa.label_of_email = lambda e: "D" if e == "d@test" else None
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
        sa.label_of_email = real_lbl


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

    # 기록 파일도 임시 폴더로 돌린다. 안 그러면 이 시험이 맥인 척하며 찍는 줄이 **진짜**
    # rotator.log 에 쌓여, 나중에 그걸 본 사람이 운영 사고로 오해한다(2026-09-04 실제로
    # "자격을 열쇠꾸러미에 못 밀고 있다" 를 몇 분 간격으로 12번 남겼다).
    real = (cm.LIVE, cm.keychain_account, cm.read_keychain, cm.push_to_keychain,
            cm.label_of, cm.creds_dir, cm.apply_pending, cm.sys.platform, cm.LOG)
    try:
        cm.LOG = Path(tmp) / "rotator.log"
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
         cm.label_of, cm.creds_dir, cm.apply_pending, cm.sys.platform, cm.LOG) = real


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
    check("칸이 바닥이라고 말한다", "Fable 칸이 이 컴퓨터가 쓸 수 있는 계정 전부에서 바닥" in msg, msg)
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
    check("알림도 칸 소진으로 나간다", "Fable 칸이 이 컴퓨터가 쓸 수 있는 계정 전부에서 바닥" in text, text)
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


def case_revive_clears_shared(tmp):
    """[61] 실제로 열린 칸을 풀 때 공유 칠판까지 지운다 (2026-10-21).

    실사고: 자리 되살리기가 "D 는 실제로 열려 있어 막힘을 풉니다" 를 두 번 찍고도 이 머신
    상태만 지워, 다음 병합(prune_blocked)이 공유 칠판의 막힘을 도로 살렸다. 그래서 Opus 자리가
    Opus·Fable 둘 다 되는 D 를 못 고르고 Fable 100% 인 A 로 가 Fable 창이 멈췄다.
    """
    print("\n[61] 되살린 칸 막힘이 병합에서 되살아나지 않는다")
    import slot_pool as sp
    import usage_check as uc
    setup(tmp, ["A", "B", "C", "D"], "A")
    now = time.time()
    fake = FakeShared()
    real = (st.shared_state, sp.stored_labels, sp._read_usage, sp.usage_check)
    st.shared_state = fake
    sp.usage_check = uc
    sp.stored_labels = lambda b: ["A", "D"]
    usage = {"A": {"weekly_pct": 90, "session_pct": 10, "scoped": {"Fable": {"pct": 100.0}}},
             "D": {"weekly_pct": 4, "session_pct": 10, "scoped": {"Fable": {"pct": 8.0}}}}
    sp._read_usage = lambda b, l: usage.get(l)
    try:
        state = st.load_state()
        st.mark_scoped_blocked(state, "A", "Fable", now + 3 * 86400)
        st.mark_scoped_blocked(state, "D", "Fable", now + 5 * 86400)
        st.save_state(state)
        freed = sp._revive(state, "Opus", 97, ("Fable",))
        st.save_state(state)
        check("열린 D 만 푼다", freed == ["D"], freed)
        check("공유 칠판에서도 D 가 지워진다", not (fake.scoped.get("D") or {}).get("Fable"), fake.scoped)
        merged = st.prune_blocked(st.load_state())
        check("병합 뒤에도 D 는 안 막혀 있다", not st.is_scoped_blocked(merged, "D", "Fable"))
        check("바닥인 A 는 그대로 막혀 있다", st.is_scoped_blocked(merged, "A", "Fable"))
    finally:
        st.shared_state, sp.stored_labels, sp._read_usage, sp.usage_check = real


def main():
    # 자리(slot) 창 안에서 돌리면 CLAUDE_CONFIG_DIR 이 살아 있어 claim_session 이 모든 표시에
    # slot 을 붙인다. 그러면 공용 계정 선택 시험 18건이 통째로 깨진다(2026-09-12 실측).
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    # 자리(slot) 기능은 기본적으로 꿄다. case_slot_pool 만 켜서 자기 임시 폴더로 돌린다.
    # 안 그랬다가 case_tend_windows 같은 다른 시험이 진짜 자리 폴더와 rotator.log 를
    # 건드렸다(2026-09-12 실측: 시험 한 번에 Opus 자리 자격이 B 로 바뀐 채 기록과 엇갈렸다).
    os.environ["AIROTATOR_NO_SLOTS"] = "1"
    os.environ["AIROTATOR_NO_NUDGE"] = "1"   # 창 지키기가 실제 창에 이어가기 줄을 넣지 않게
    os.environ["AIROTATOR_NO_ALERT"] = "1"   # 자리 로그인 알림(slot_pool._login_alert)이 진짜 알림·표시를 안 건드리게
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
                     case_stale_scoped_block, case_stale_account_block,
                     case_push_fail_alert, case_tend_windows,
                     case_usage_cache_backoff, case_nudge_stalled,
                     case_window_limit_from_screen, case_slot_pool, case_slot_fresh_switch,
                     case_last_resort_shared, case_revive_clears_shared):
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


def case_usage_cache_backoff(tmp):
    """[57] 사용량 조회에 짧은 TTL 캐시 + 429 백오프가 걸려 잇단 조회를 줄인다 (2026-09-06).

    --tend(3분)·봇 shim·cs 가 같은 계정을 잇달아 조회하면 서버가 429 로 막았다.
    상태줄 조회기처럼 최근 성공값을 다시 쓰고, 429 를 받으면 잠시 안 두드린다.
    통신 자체를 가짜로 바꿔 조회 횟수를 센다(진짜 서버·자격은 안 건드린다).
    """
    print("[57] 사용량 조회 캐시 + 429 백오프")
    import io as _io, urllib.request as _ur, urllib.error as _ue, json as _json
    from pathlib import Path as _P
    import usage_check as uc

    real_open = _ur.urlopen
    real_cache = uc._CACHE_FILE
    real_probe = uc._PROBE_CACHE
    uc._CACHE_FILE = _P(tmp) / ".rotator-usage-cache.json"
    uc._PROBE_CACHE = _P(tmp) / ".no-probe.json"   # 프로브 재사용 기본 OFF(초반 검사를 결정적으로)
    cred = _P(tmp) / "A.json"
    cred.write_text(_json.dumps({"claudeAiOauth": {"accessToken": "x"}}), encoding="utf-8")

    calls = {"n": 0}
    mode = {"raise": None}
    body = _json.dumps({
        "five_hour": {"utilization": 15.0, "resets_at": "2026-09-07T00:00:00Z"},
        "seven_day": {"utilization": 40.0, "resets_at": "2026-09-11T00:00:00Z"},
        "limits": [{"kind": "weekly_scoped",
                    "scope": {"model": {"display_name": "Fable"}},
                    "percent": 6.0, "resets_at": "2026-09-11T00:00:00Z"}],
    }).encode()

    class _Resp:
        def read(self): return body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=None):
        calls["n"] += 1
        if mode["raise"] == "429":
            raise _ue.HTTPError(uc.USAGE_URL, 429, "Too Many", {}, _io.BytesIO(b""))
        return _Resp()

    _ur.urlopen = fake_open
    try:
        u = uc.get_usage(cred)
        check("첫 조회는 성공값을 준다", bool(u) and u["session_pct"] == 15.0, u)
        check("첫 조회는 통신 1회", calls["n"] == 1, calls)

        uc.get_usage(cred)
        check("TTL 안 재조회는 통신하지 않는다", calls["n"] == 1, calls)

        # TTL 을 강제로 늙히고 429 를 받게 한다
        c = _json.loads(uc._CACHE_FILE.read_text(encoding="utf-8"))
        c["A"]["at"] -= 999
        uc._CACHE_FILE.write_text(_json.dumps(c), encoding="utf-8")
        mode["raise"] = "429"
        u3 = uc.get_usage(cred)
        check("429 면 None + last_status 429", u3 is None and uc.last_status == 429, uc.last_status)
        check("429 는 통신을 한 번 했다", calls["n"] == 2, calls)

        u4 = uc.get_usage(cred)
        check("백오프 중에는 통신하지 않는다", calls["n"] == 2 and u4 is None, calls)

        # 백오프 풀고 정상화
        c = _json.loads(uc._CACHE_FILE.read_text(encoding="utf-8"))
        c["A"]["backoff_until"] = 0
        uc._CACHE_FILE.write_text(_json.dumps(c), encoding="utf-8")
        mode["raise"] = None
        u5 = uc.get_usage(cred)
        check("백오프 풀리면 다시 성공값", bool(u5) and u5["session_pct"] == 15.0, u5)
        c = _json.loads(uc._CACHE_FILE.read_text(encoding="utf-8"))
        check("성공 시 백오프가 지워진다", not c["A"].get("backoff_until"), c["A"])
        check("자격 값은 캐시에 안 담긴다",
              "accessToken" not in uc._CACHE_FILE.read_text(encoding="utf-8"))

        # is_current 갈래: 지금 계정(A)이 429 백오프면, 저장 자격이 낡아 살아있는 자격으로
        # 넘어가더라도 계정 라벨 백오프를 먼저 봐서 통신하지 않는다(폭주 절반 방지 수정).
        c = _json.loads(uc._CACHE_FILE.read_text(encoding="utf-8"))
        c["A"]["backoff_until"] = 9_999_999_999
        c["A"].pop("usage", None)   # 저장 자격이 낡아 살아있는 자격 갈래로 내려가게 한다
        uc._CACHE_FILE.write_text(_json.dumps(c), encoding="utf-8")
        before = calls["n"]
        u6 = uc.get_usage_for(cred, is_current=True)
        check("A 백오프 중엔 살아있는 자격 갈래도 통신 안 함",
              u6 is None and calls["n"] == before, (u6, before, calls["n"]))

        # 조회기(usage_probe) 캐시 재사용: 그 파일에 현재 계정이 신선하게 적혀 있으면
        # 순환기는 제 조회를 하지 않고 그 값을 쓴다(같은 창구 두 번 안 두드림 = 429 폭주 제거).
        uc._PROBE_CACHE = _P(tmp) / ".usage-cache.json"
        uc._PROBE_CACHE.write_text(_json.dumps({
            "at": __import__("time").time(), "account": "P",
            "scoped": [{"name": "Fable", "used_pct": 12.0, "resets_at": None}],
            "session": {"pct": 22.0, "reset": None}, "weekly": {"pct": 33.0, "reset": None},
        }), encoding="utf-8")
        # 순환기 자기 캐시에는 P 가 없어 미스 → 조회기 캐시로 떨어져야 한다
        c = _json.loads(uc._CACHE_FILE.read_text(encoding="utf-8"))
        c.pop("P", None)
        uc._CACHE_FILE.write_text(_json.dumps(c), encoding="utf-8")
        pcred = _P(tmp) / "P.json"
        pcred.write_text(_json.dumps({"claudeAiOauth": {"accessToken": "z"}}), encoding="utf-8")
        before = calls["n"]
        up = uc.get_usage(pcred)
        check("조회기 캐시가 신선하면 통신 없이 그 값을 쓴다",
              up is not None and up["session_pct"] == 22.0 and up["weekly_pct"] == 33.0
              and calls["n"] == before, (up, before, calls["n"]))

        # 2026-10-21: 낡은 기본 저장본(creds/D.json)의 401 이 같은 계정의 멀쩡한 자리 저장본까지 막았다.
        # 거절은 파일 칸('D', 'D@Fable')에 따로 적혀 자리 저장본은 그대로 읽혀야 한다.
        uc._PROBE_CACHE = _P(tmp) / ".no-probe.json"
        dmain = _P(tmp) / "D.json"
        dmain.write_text(_json.dumps({"claudeAiOauth": {"accessToken": "old"}}), encoding="utf-8")
        dslot = _P(tmp) / "slots" / "Fable" / "D.json"
        dslot.parent.mkdir(parents=True, exist_ok=True)
        dslot.write_text(_json.dumps({"claudeAiOauth": {"accessToken": "new"}}), encoding="utf-8")
        c = _json.loads(uc._CACHE_FILE.read_text(encoding="utf-8"))
        c.pop("D", None)
        uc._CACHE_FILE.write_text(_json.dumps(c), encoding="utf-8")
        mode["raise"] = "401"
        real_fake = _ur.urlopen

        def by_token(req, timeout=None):
            if req.get_header("Authorization") == "Bearer old":
                calls["n"] += 1
                raise _ue.HTTPError(uc.USAGE_URL, 401, "Unauthorized", {}, _io.BytesIO(b""))
            mode["raise"] = None
            return real_fake(req, timeout)

        _ur.urlopen = by_token
        u7 = uc.get_usage(dmain)
        check("낡은 기본 저장본은 401 로 None", u7 is None and uc.last_status == 401, uc.last_status)
        u8 = uc.get_usage(dslot)
        check("같은 계정 자리 저장본은 기본 저장본의 401 에 안 묶인다",
              u8 is not None and u8["weekly_pct"] == 40.0, (u8, uc.last_error))
        c = _json.loads(uc._CACHE_FILE.read_text(encoding="utf-8"))
        check("401 기억은 기본 저장본 칸에만 남는다",
              c.get("D@main", {}).get("fail_why") == "HTTP 401" and "D@Fable" not in c
              and not c.get("D", {}).get("fail_until"), c)
    finally:
        _ur.urlopen = real_open
        uc._CACHE_FILE = real_cache
        uc._PROBE_CACHE = real_probe


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
    check("켜기 전 확인이 갈아탔다고 알린다(종료코드 3)", rc == claude_pool.ENSURE_SWITCHED, rc)
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

def case_push_fail_alert(tmp):
    """[55] 열쇠꾸러미에 두 번 연달아 못 넣으면 사람에게 한 번 알린다.

    화면이 잠긴 맥에서는 자격을 저장하는 진짜 자리에 새 값이 안 들어간다. 그대로 두면
    맥 봇들이 어느 순간 로그인 만료로 멈춘다. 예전에는 기록에만 남아 사람이 로그를
    열어봐야 알았다(2026-09-04 사용자 요구로 알림 추가).
    """
    print()
    print("[55] 열쇠꾸러미 밀기가 연달아 막히면 한 번 알린다")
    import types

    import cred_mirror as cm

    sent = []
    fake = types.SimpleNamespace(send=lambda msg, **kw: sent.append(msg))
    real_creds, real_mod = cm.creds_dir, sys.modules.get("notify")
    sys.modules["notify"] = fake
    cm.creds_dir = lambda: Path(tmp)
    try:
        cm._note_push_fail("화면이 잠긴 자리")
        check("한 번은 넘어간다(갈아끼우는 중일 수 있다)", sent == [], sent)
        cm._note_push_fail("화면이 잠긴 자리")
        check("두 번째에 알린다", len(sent) == 1, sent)
        check("무엇을 해야 하는지 적는다", "맥 본체 화면" in (sent[0] if sent else ""))
        cm._note_push_fail("화면이 잠긴 자리")
        check("이어지는 실패에는 다시 안 알린다", len(sent) == 1, len(sent))

        cm._clear_push_fail()          # 다시 밀 수 있게 됐다
        cm._note_push_fail("또 막힘")
        check("풀린 뒤에는 처음부터 센다", len(sent) == 1, len(sent))
        cm._note_push_fail("또 막힘")
        check("다음 사고는 다시 알린다", len(sent) == 2, len(sent))
    finally:
        cm.creds_dir = real_creds
        if real_mod is None:
            sys.modules.pop("notify", None)
        else:
            sys.modules["notify"] = real_mod


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
    check("켜기 전 확인이 갈아탔다고 알린다(종료코드 3)", rc == claude_pool.ENSURE_SWITCHED, rc)
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


def case_tend_windows(tmp):
    """[56] 떠 있는 창의 칸이 대화 중에 바닥나면 주기 작업이 갈아끼운다 (2026-09-06).

    그동안은 창이 켜질 때(--ensure)와 끝난 뒤(ccauto)만 봐서, 대화 중 Fable 이 바닥나면
    사람이 새 창에서 cc 를 다시 쳐야 했다. --tend 가 창 표시(session_claims)의 칸을 보고
    지금 계정에서 그 칸이 100% 면 갈아끼운다. 창이 없으면 조회도 안 한다.
    """
    print()
    print("[56] 떠 있는 창의 칸이 바닥나면 주기 작업이 갈아끼운다")
    import usage_check as uc

    now = time.time()
    fable_full = {"weekly_pct": 45.0, "session_pct": 30.0,
                  "weekly_reset": now + 48 * 3600, "session_reset": now + 3600,
                  "scoped": {"Fable": {"pct": 100.0, "reset": now + 48 * 3600}}}
    fable_free = {"weekly_pct": 20.0, "session_pct": 5.0,
                  "weekly_reset": now + 60 * 3600, "session_reset": None,
                  "scoped": {"Fable": {"pct": 10.0, "reset": now + 60 * 3600}}}
    weekly_full = dict(fable_free, weekly_pct=100.0)

    real_alive = st._pid_alive
    alive = set()
    st._pid_alive = lambda pid: str(pid) in alive

    def run_tend(table, current):
        if st.STATE_FILE.exists():
            st.STATE_FILE.unlink()
        setup(tmp, ["A", "B"], current)
        s = st.load_state()
        s["reclaim_at"] = None
        st.save_state(s)
        seen = []
        real_get, real_status = uc.get_usage, uc.last_status
        install(Fake({x: (0, OK_OUT, "") for x in ("A", "B")}))
        claude_pool.usage_check = uc
        uc.last_status = None
        def get(path):
            seen.append(Path(path).stem)
            return table.get(Path(path).stem)
        uc.get_usage = get
        try:
            if alive:
                claude_pool.claim_session(4242, model="claude-fable-5-1")
            return claude_pool.tend_windows(), seen
        finally:
            uc.get_usage, uc.last_status = real_get, real_status
            claude_pool.usage_check = None

    try:
        # (1) 떠 있는 창이 없으면 조회도 교체도 없다
        rc, seen = run_tend({"A": fable_full, "B": fable_free}, "A")
        check("창이 없으면 그대로(0)", rc == 0, rc)
        check("창이 없으면 사용량을 안 읽는다", seen == [], seen)
        check("계정도 그대로 A", st.load_state().get("current") == "A")

        # (2) Fable 창이 떠 있고 A 의 Fable 칸이 바닥 → B 로 갈아끼운다
        alive.add("4242")
        rc, seen = run_tend({"A": fable_full, "B": fable_free}, "A")
        after = st.load_state()
        check("갈아탔다고 알린다(3)", rc == claude_pool.ENSURE_SWITCHED, rc)
        check("Fable 이 남은 B 로 간다", after.get("current") == "B", after.get("current"))
        check("A 는 Fable 칸만 막는다", st.is_scoped_blocked(after, "A", "Fable")
              and not st.is_blocked(after, "A"))
        check("창 표시는 남아 있다", st.claimed_buckets(after) == ("Fable",),
              st.claimed_buckets(after))

        # (3) 문턱(97%) 아래면 아무것도 안 한다
        rc, seen = run_tend({"A": dict(fable_full, scoped={"Fable": {"pct": 96.0, "reset": now + 48 * 3600}}),
                             "B": fable_free}, "A")
        check("96% 면 그대로(0)", rc == 0, rc)
        check("계정 그대로 A", st.load_state().get("current") == "A")
        check("조회는 지금 계정 한 번", seen == ["A"], seen)

        # (3-1) 97% 면 거절받기 전에 미리 넘긴다(2026-09-10 사용자 결정: 창은 거절받으면 작업이 멈춘다)
        rc, seen = run_tend({"A": dict(fable_full, scoped={"Fable": {"pct": 97.0, "reset": now + 48 * 3600}}),
                             "B": fable_free}, "A")
        after = st.load_state()
        check("97% 면 미리 넘긴다(3)", rc == claude_pool.ENSURE_SWITCHED, rc)
        check("미리 넘겨도 B 로 간다", after.get("current") == "B", after.get("current"))
        # 세션 97% 로 미리 넘긴 계정은 낡은 막힘 풀기가 되살리면 안 된다(되살리면 되돌림이 끌고 와 왕복)
        rc, seen = run_tend({"A": dict(fable_free, session_pct=98.0, session_reset=now + 3600),
                             "B": fable_free}, "A")
        after = st.load_state()
        check("세션 98% 도 미리 넘긴다", rc == claude_pool.ENSURE_SWITCHED and after.get("current") == "B",
              (rc, after.get("current")))
        claude_pool.usage_check = uc   # run_tend 가 끝나며 비워 두므로 되살리기 판정에 다시 꽂는다
        try:
            freed = claude_pool._revive_account_blocks(
                after, {"A": dict(fable_free, session_pct=98.0)}, probe=False)
            check("문턱 위 계정은 되살리지 않는다", freed == [] and st.is_blocked(after, "A"), freed)
            freed = claude_pool._revive_account_blocks(
                after, {"A": dict(fable_free, session_pct=10.0)}, probe=False)
            check("세션이 풀리면 되살린다", freed == ["A"], freed)
        finally:
            claude_pool.usage_check = None

        # (3-2) 97~100% 인데 갈 곳이 없으면 두 번째 회차부터는 지금 계정만 읽고 끝낸다
        #       (검수 반영: 3분마다 막음 기록·전 계정 조회를 되풀이했다)
        if st.STATE_FILE.exists():
            st.STATE_FILE.unlink()
        setup(tmp, ["A", "B"], "A")
        s = st.load_state()
        s["reclaim_at"] = None
        st.save_state(s)
        seen2 = []
        full98 = dict(fable_free, session_pct=98.0, session_reset=now + 3600)
        real_get, real_status = uc.get_usage, uc.last_status
        install(Fake({x: (0, OK_OUT, "") for x in ("A", "B")}))
        claude_pool.usage_check = uc
        uc.last_status = None
        uc.get_usage = lambda path: (seen2.append(Path(path).stem), full98)[1]
        try:
            claude_pool.claim_session(4242, model="claude-fable-5-1")
            r1 = claude_pool.tend_windows()
            seen2.clear()
            r2 = claude_pool.tend_windows()
        finally:
            uc.get_usage, uc.last_status = real_get, real_status
            claude_pool.usage_check = None
        check("둘 다 97% 넘으면 갈 곳 없음으로 조용히 끝난다", r1 == 0 and r2 == 0, (r1, r2))
        check("두 번째 회차는 지금 계정 한 번만 읽는다",
              seen2 == [st.load_state().get("current")], seen2)

        # (3-3) 이미 막힌 계정에 앉아 있어도 30분에 한 번은 다른 계정의 낡은 칸 막힘을 풀어 갈아탄다
        #       (재검수: 되살리기를 통째로 빼자 이 PC 에선 낡은 칸 막힘을 푸는 곳이 없어졌다)
        a98 = dict(fable_free, scoped={"Fable": {"pct": 98.0, "reset": now + 3600}})

        def stale_run(revive_at):
            if st.STATE_FILE.exists():
                st.STATE_FILE.unlink()
            setup(tmp, ["A", "B"], "A")
            s = st.load_state()
            s["reclaim_at"] = None
            st.mark_scoped_blocked(s, "A", "Fable", now + 3600)   # 지난 회차가 막아 둠
            st.mark_scoped_blocked(s, "B", "Fable", now + 7200)   # 낡은 막힘(실제로는 풀려 있음)
            s["tend_revive_at"] = revive_at
            st.save_state(s)
            got_seen = []
            real_get, real_status = uc.get_usage, uc.last_status
            install(Fake({x: (0, OK_OUT, "") for x in ("A", "B")}))
            claude_pool.usage_check = uc
            uc.last_status = None
            uc.get_usage = lambda path: (got_seen.append(Path(path).stem),
                                         {"A": a98, "B": fable_free}[Path(path).stem])[1]
            try:
                claude_pool.claim_session(4242, model="claude-fable-5-1")
                return claude_pool.tend_windows(), st.load_state().get("current"), got_seen
            finally:
                uc.get_usage, uc.last_status = real_get, real_status
                claude_pool.usage_check = None

        rc3, cur3, _ = stale_run(now - 3600)
        check("30분 지났으면 낡은 칸 막힘을 풀고 갈아탄다",
              rc3 == claude_pool.ENSURE_SWITCHED and cur3 == "B", (rc3, cur3))
        rc3, cur3, seen3 = stale_run(now)
        check("30분 안이면 조회 없이 그대로 둔다", rc3 == 0 and cur3 == "A" and "B" not in seen3,
              (rc3, cur3, seen3))

        # (4) 계정 전체 주간이 바닥나도 갈아끼운다(칸보다 먼저 본다)
        rc, seen = run_tend({"A": weekly_full, "B": fable_free}, "A")
        after = st.load_state()
        check("주간 소진도 갈아탄다(3)", rc == claude_pool.ENSURE_SWITCHED, rc)
        check("A 는 통째로 막힌다", st.is_blocked(after, "A"))
        check("B 로 간다", after.get("current") == "B", after.get("current"))

        # (5) 갈아탈 계정이 없으면 0 으로 조용히 끝난다(창은 그대로, 알림은 --ensure 몫)
        rc, seen = run_tend({"A": fable_full, "B": fable_full}, "A")
        check("갈아탈 데 없으면 0", rc == 0, rc)
        # 다른 계정의 칸은 미리 못 보므로 B 로 한 번 건너가 본 뒤 "없다" 로 끝난다(문서화된 동작).
        # 둘 다 바닥이라 어느 쪽에 앉든 같다 — 둘 다 Fable 막힘으로 기록됐는지가 핵심.
        after = st.load_state()
        check("두 계정 다 Fable 막힘으로 기록된다",
              st.is_scoped_blocked(after, "A", "Fable") and st.is_scoped_blocked(after, "B", "Fable"))
        check("계정은 통째로 막지 않는다", not st.is_blocked(after, "A") and not st.is_blocked(after, "B"))

        # (6) 창 두 개가 다른 칸을 잡았고 둘 다 만족하는 계정이 없으면 갈아끼우지 않는다.
        #     (검수 재현: 밀면 다음 회차가 되밀어 3분마다 B→A→B→A 왕복)
        opus_full_a = dict(fable_full, scoped={"Fable": {"pct": 100.0, "reset": now + 48 * 3600},
                                              "Opus": {"pct": 5.0, "reset": now + 48 * 3600}})
        opus_full_b = dict(fable_free, scoped={"Fable": {"pct": 10.0, "reset": now + 60 * 3600},
                                              "Opus": {"pct": 100.0, "reset": now + 60 * 3600}})
        if st.STATE_FILE.exists():
            st.STATE_FILE.unlink()
        setup(tmp, ["A", "B"], "A")
        s = st.load_state()
        s["reclaim_at"] = None
        st.mark_scoped_blocked(s, "B", "Opus", now + 60 * 3600)   # B 의 Opus 는 이미 알려진 바닥
        st.save_state(s)
        alive.add("4343")
        real_get, real_status = uc.get_usage, uc.last_status
        install(Fake({x: (0, OK_OUT, "") for x in ("A", "B")}))
        claude_pool.usage_check = uc
        uc.last_status = None
        uc.get_usage = lambda path: {"A": opus_full_a, "B": opus_full_b}[Path(path).stem]
        try:
            claude_pool.claim_session(4242, model="claude-fable-5-1")
            claude_pool.claim_session(4343, model="claude-opus-5")
            outs = []
            for _ in range(3):
                outs.append((claude_pool.tend_windows(), st.load_state().get("current")))
        finally:
            uc.get_usage, uc.last_status = real_get, real_status
            claude_pool.usage_check = None
            claude_pool.release_session(4343)
            alive.discard("4343")
        check("둘 다 만족하는 계정이 없으면 한 번도 안 갈아탄다",
              all(rc == 0 and cur == "A" for rc, cur in outs), outs)

        # (7) 사용량을 못 읽으면 아무것도 안 하고 흔적만 남긴다
        rc, seen = run_tend({}, "A")
        check("못 읽으면 그대로(0)", rc == 0, rc)
        check("못 읽어도 계정은 A", st.load_state().get("current") == "A")
        # 저장본이 안 읽히면 지금 꽂힌 자격 파일로 한 번 더 시도하는 갈래가 원래 있다(get_usage_for).
        # 여기서 보려는 건 "B 같은 다른 계정을 읽으러 가지 않는다" 이다.
        check("지금 계정만 읽고 다른 계정은 안 읽는다", seen[:1] == ["A"] and "B" not in seen, seen)

        # (8) 2026-09-12 실사고 1: 떠 있는 창의 칸 중 하나(Fable)가 **모든 계정**에서 바닥이면
        #     그 칸은 조건에서 빼고 나머지 칸(Opus)이 남은 계정으로 간다. 그동안은 "Fable 창까지
        #     만족하는 계정이 없다" 며 Opus 창들이 4시간 넘게 멈춰 있었다.
        opus_full_a2 = dict(fable_full, session_pct=98.0, session_reset=now + 3600)  # A: Fable 바닥 + 세션 98%
        opus_free_b2 = dict(fable_full, session_pct=3.0, session_reset=now + 3600)   # B: Fable 바닥, 세션 여유
        if st.STATE_FILE.exists():
            st.STATE_FILE.unlink()
        setup(tmp, ["A", "B"], "A")
        s = st.load_state()
        s["reclaim_at"] = None
        st.mark_scoped_blocked(s, "A", "Fable", now + 48 * 3600)
        st.mark_scoped_blocked(s, "B", "Fable", now + 48 * 3600)
        st.save_state(s)
        alive.add("4343")
        real_get, real_status = uc.get_usage, uc.last_status
        install(Fake({x: (0, OK_OUT, "") for x in ("A", "B")}))
        claude_pool.usage_check = uc
        uc.last_status = None
        uc.get_usage = lambda path: {"A": opus_full_a2, "B": opus_free_b2}[Path(path).stem]
        try:
            claude_pool.claim_session(4242, model="claude-fable-5-1")
            claude_pool.claim_session(4343, model="claude-opus-5")
            outs = []
            for _ in range(3):
                outs.append((claude_pool.tend_windows(), st.load_state().get("current")))
        finally:
            uc.get_usage, uc.last_status = real_get, real_status
            claude_pool.usage_check = None
            claude_pool.release_session(4343)
            alive.discard("4343")
        check("Fable 이 어디서도 안 되면 그 칸은 빼고 Opus 가 남은 B 로 간다",
              outs[0][0] == claude_pool.ENSURE_SWITCHED and outs[0][1] == "B", outs[0])
        check("그 뒤로는 B 에 머문다(왕복 없음)", all(cur == "B" and rc == 0 for rc, cur in outs[1:]), outs)

        # (9) 2026-09-12 실사고 2: 사용량 조회가 429 로 실패해도 **기록상 이미 막힌 계정**이면
        #     그대로 두지 않고 기록으로 갈아탄다(cc 가 막힌 B 를 그대로 켜 첫 요청부터 거절받았다).
        if st.STATE_FILE.exists():
            st.STATE_FILE.unlink()
        setup(tmp, ["A", "B"], "A")
        s = st.load_state()
        s["reclaim_at"] = None
        st.mark_blocked(s, "A", now + 3600, "세션 한도 소진(시험)")
        st.save_state(s)
        real_get, real_status = uc.get_usage, uc.last_status
        install(Fake({x: (0, OK_OUT, "") for x in ("A", "B")}))
        claude_pool.usage_check = uc
        uc.last_status = None
        uc.get_usage = lambda path: None   # 조회 실패
        try:
            rc9 = claude_pool.ensure_usable(model="claude-opus-5")
        finally:
            uc.get_usage, uc.last_status = real_get, real_status
            claude_pool.usage_check = None
        check("못 읽어도 기록상 막힌 계정이면 갈아탄다(3)", rc9 == claude_pool.ENSURE_SWITCHED, rc9)
        check("B 로 간다", st.load_state().get("current") == "B", st.load_state().get("current"))
    finally:
        st._pid_alive = real_alive
        claude_pool.release_session(4242)


def case_window_limit_from_screen(tmp):
    """[59] 창 화면의 한도 거절을 그 거절 당시 계정의 칸 막힘으로 적는다 (2026-09-14).

    실사고: 사용량 조회가 하루 종일 429 로 막혀 순환기가 어느 계정의 Fable 이 찼는지
    기록에 못 적었다. 그 사이 되돌리기가 주간 초기화만 보고 공용을 Fable 여유 계정(D)에서
    Fable 100% 계정(C)으로 옮겨, 그 위 공용 Fable 창들이 8시간 한도 거절을 받았다.
    막힘 기록이 없으니 폴백도 못 움직였다. 유일한 신호(창 화면의 거절)를 기록에 넣는다.
    """
    print("\n[59] 창 화면의 한도 거절을 기록으로 되먹인다")
    import usage_check as uc
    import session_nudge as sn

    setup(tmp, ["A", "B", "C", "D"], "A")
    now = time.time()
    real_cached = uc.cached_usage
    claude_pool.usage_check = uc
    try:
        # (A) 귀속: 거절 당시 계정을 교체 기록으로 되짚는다 (지금 계정이 아니라)
        state = st.load_state()
        state["history"] = [
            {"at": now - 500, "label": "D", "kind": "switched", "reason": ""},
            {"at": now - 300, "label": "C", "kind": "switched", "reason": ""},
            {"at": now - 100, "label": "A", "kind": "switched", "reason": ""},
            {"at": now - 250, "label": "C", "kind": "slot:Fable", "reason": ""},
        ]
        check("공용: 거절 시각의 계정(C)에 귀속(지금 계정 A 아님)",
              claude_pool._account_for_where_at(state, "shared", now - 200) == "C")
        check("공용: 더 이른 거절이면 그때 계정(D)",
              claude_pool._account_for_where_at(state, "shared", now - 400) == "D")
        check("기록 이전이면 넘겨짚지 않는다(None)",
              claude_pool._account_for_where_at(state, "shared", now - 900) is None)
        check("자리(slot:Fable) 거절은 그 자리 기록으로 귀속",
              claude_pool._account_for_where_at(state, "slot:Fable", now - 200) == "C")

        # (B) 기록: 거절 당시 계정 C 의 Fable 칸만 막고, 지금 계정 A 는 안 막는다
        uc.cached_usage = lambda label: None   # 캐시엔 리셋 없음 → 주간 리셋으로 떨어진다
        state["weekly_reset"] = {"C": now + 3 * 24 * 3600}
        st.save_state(state)
        claude_pool._record_window_limit("shared", "Fable", now - 200)
        state = st.load_state()
        check("C 의 Fable 칸이 막힘으로 적힌다", st.is_scoped_blocked(state, "C", "Fable"))
        check("지금 계정 A 는 안 막힌다", not st.is_scoped_blocked(state, "A", "Fable"))
        check("계정 전체 막힘은 아니다(Opus 는 그대로)", not st.is_scoped_blocked(state, "C", "Opus"))

        # 거절 뒤에 읽은 사용량이 남아 있으면 그 계정은 바닥이 아니었다 → 안 적는다 (2026-10-21)
        # 실사고: 멈춤을 처음 본 시각엔 자리가 이미 D 로 바뀌어 있어, Fable 7% 인 D 가 일주일 막혔다.
        state = st.load_state()
        state["scoped_blocked"] = {}
        state["usage_seen"] = {"C": {"at": now - 100, "scoped": {"Fable": {"pct": 8.0}}}}
        st.save_state(state)
        claude_pool._record_window_limit("shared", "Fable", now - 200)
        check("거절 뒤 읽은 값이 8% 면 안 막는다",
              not st.is_scoped_blocked(st.load_state(), "C", "Fable"))
        state = st.load_state()
        state["usage_seen"] = {"C": {"at": now - 300, "scoped": {"Fable": {"pct": 8.0}}}}
        st.save_state(state)
        claude_pool._record_window_limit("shared", "Fable", now - 200)
        check("거절 전에 읽은 값이면 근거가 안 돼 그대로 막는다",
              st.is_scoped_blocked(st.load_state(), "C", "Fable"))
        state = st.load_state()
        state["usage_seen"] = {}
        state["scoped_blocked"] = {}
        st.save_state(state)

        # 리셋 시각을 못 구하면 안 적는다(영영 막힘 방지)
        state["weekly_reset"] = {}
        state["scoped_blocked"] = {}
        st.save_state(state)
        claude_pool._record_window_limit("shared", "Fable", now - 200)
        check("리셋 시각을 못 구하면 안 적는다",
              not st.is_scoped_blocked(st.load_state(), "C", "Fable"))

        # (C) 고리가 닫힌다: 창 거절이 기록되면 되돌리기가 그 계정을 자동으로 뺀다.
        # 실사고의 되돌리기 D→C 는 C 의 Fable 소진이 기록에 없어서 일어났다. Fix B 가
        # 기록하면 기존 better_candidate 가 그 계정을 후보에서 뺀다(별도 조회 없이).
        setup(tmp, ["A", "B"], "B")
        state = st.load_state()
        state["weekly_reset"] = {"A": now + 1000, "B": now + 5 * 24 * 3600}  # A 가 앞순위
        state["reclaim_at"] = None
        st.save_state(state)
        state = st.load_state()
        check("기록 전엔 앞순위 A 가 Fable 되돌리기 후보",
              st.better_candidate(state, "B", bucket=("Fable",)) == "A")
        st.mark_scoped_blocked(state, "A", "Fable", now + 3 * 24 * 3600)  # 창 거절이 A:Fable 을 적었다고 침
        check("기록 뒤엔 A 가 Fable 되돌리기 후보에서 빠진다",
              st.better_candidate(state, "B", bucket=("Fable",)) is None)
        check("다른 칸(Opus)이면 A 는 여전히 후보(칸만 막았다)",
              st.better_candidate(state, "B", bucket=("Opus",)) == "A")

        # (D) nudge_stalled 가 Fable 한도 창에만 on_limit 을 부른다(세션 한도는 아님)
        home = Path(tmp) / "wl_home"
        (home / "sessions").mkdir(parents=True, exist_ok=True)
        rule = "─" * 100
        status = "  X | - | Fable 5.1 | [■□□□□□□□□□] 10%"
        fable_hit = ["> 해줘",
                     "  ⎿ \xa0You've reached your Fable limit. Run /usage-credits to continue or switch models with /model.",
                     "✻ done", rule, ">", rule, status]
        session_hit = ["● 이어서", "  ⎿  You've hit your session limit · resets 1am (Asia/Seoul)",
                       rule, ">", rule, status]
        calls = []
        sn.nudge_stalled(lambda m, where=None: False, log=lambda *_: None, now=now, home=home,
                         state_file=Path(tmp) / "wl.json", windows=lambda: [11, 12],
                         reader=lambda ps: {11: {"title": "공용 Fable 창", "lines": fable_hit},
                                            12: {"title": "세션 한도 창", "lines": session_hit}},
                         typer=lambda pid: {"ok": True}, start_of=lambda pid: 1,
                         places=lambda ps: {11: "shared", 12: "shared"},
                         on_limit=lambda where, bucket, at: calls.append((where, bucket, at)))
        check("Fable 한도 창은 on_limit 을 부른다(칸 이름·자격 전달)",
              (("shared", "Fable") in [(w, b) for w, b, _ in calls]), calls)
        check("세션(계정 전체) 한도 창은 on_limit 을 안 부른다",
              all(b == "Fable" for _, b, _ in calls), calls)
        check("귀속 시각은 멈춤을 처음 본 때다(화면이 바뀐 계정 오귀속 방지)",
              all(abs(at - now) < 5 for _, _, at in calls), calls)
    finally:
        uc.cached_usage = real_cached
        claude_pool.usage_check = None


def case_nudge_stalled(tmp):
    """[57] 한도로 멈춘 창에 이어가라는 줄을 넣는다 (2026-09-10).

    창은 한도 거절을 받으면 하던 작업을 멈추고 사람 입력을 기다린다. 계정을 갈아끼워도
    누가 "계속" 을 쳐야 이어졌다. 화면을 읽어 멈춘 창을 찾고 입력 칸에 한 줄을 넣는다.
    화면 예시는 2026-09-10 시험 창에서 실제로 읽은 글자를 본떴다.
    """
    print()
    print("[57] 한도로 멈춘 창에 이어가라는 줄을 넣는다")
    import session_nudge as sn

    rule = "─" * 100
    rule_t = "─" * 90 + " rotator-test ─"
    status_f = "  JUNSEONG | - | Fable 5.1 | [■□□□□□□□□□] 10% | 남은 5h 95% 내일 1시 재설정"
    status_o = "  JUNSEONG | - | Opus 5 (1M context) | [■■□□□□□□□□] 18% | 남은 5h 95%"
    stalled = ["✻ Baked for 1m 10s · done 오후 8:31", "> 안녕, 한 줄로 답해 줘",
               "  ⎿ \xa0You've reached your Fable limit. Run /usage-credits to continue or switch models with /model.",
               "✻ Churned for 0s · done 오후 8:32", rule_t, ">", rule, status_f,
               "  ⏵⏵ bypass permissions on (shift+tab to cycle)"]
    session_hit = ["● 렌더 이어서 돌립니다",
                   "  ⎿  You've hit your session limit · resets 1am (Asia/Seoul)",
                   "● Ran 7 stop hooks",
                   "  ⎿ \xa0Stop hook error: Failed with non-blocking status code: No stderr output",
                   "● Remote Control disconnected — run /remote-control to retry",
                   rule, "❯", rule, status_o]
    v = sn.parse_screen(stalled)
    check("Fable 한도 화면 = 멈춤", v["stalled"] and v["input_empty"] and not v["busy"], v)
    check("상태줄에서 모델을 읽는다", (v["model"] or "").startswith("Fable"), v["model"])
    v = sn.parse_screen(session_hit)
    check("세션 한도 + 뒤따르는 부속 줄도 멈춤", v["stalled"] and v["input_empty"], v)
    after_nudge = session_hit[:5] + ["> " + sn.NUDGE_LINE, rule, "❯", rule, status_o]
    check("이어가기 줄을 넣은 뒤엔 멈춤 아님(같은 멈춤에 두 번 안 넣는다)",
          not sn.parse_screen(after_nudge)["stalled"])
    resumed = session_hit[:5] + ["> " + sn.NUDGE_LINE, "● 12편부터 이어서 렌더합니다",
                                 rule, "❯", rule, status_o]
    check("다시 일한 뒤엔 멈춤 아님", not sn.parse_screen(resumed)["stalled"])
    busy = ["> 작업해", "  ⎿  You've hit your session limit · resets 1am",
            "* Inferring… (running stop hooks… 5/7 · 16s)", rule, ">", rule, status_o]
    vb = sn.parse_screen(busy)
    check("일하는 중이면 멈춤 아님", not vb["stalled"] and vb["busy"], vb)
    typing = session_hit[:5] + [rule, "❯ 사람이 치는 중", rule, status_o]
    check("입력 칸에 글자가 있으면 넣지 않는다", not sn.parse_screen(typing)["input_empty"])
    # 2026-09-12 실측: 새 판 claude 는 한도 뒤에 대기 표시를 ● 로 붙인다. 답변이 아니라 멈춤이다.
    waiting = ["  Ran 1 shell command",
               "  ⎿ \xa0You've hit your session limit · resets 1:10pm (Asia/Seoul)",
               "     Continuing automatically at 1:10pm · esc to cancel",
               "     /usage-credits to continue now", "",
               "● Usage limit reached · continuing automatically at 1:10pm · esc or type to cancel", "",
               "✻ Sautéed for 16m 54s · done 오후 12:52", rule, ">", rule,
               "  ⚠ Usage limit reached · continuing automatically at 1:10pm · esc to cancel",
               "  ⚠ /usage-credits to continue now", status_o]
    vw = sn.parse_screen(waiting)
    check("'continuing automatically' 대기 표시가 붙어도 멈춤", vw["stalled"] and vw["input_empty"], vw)
    normal = ["● 끝났습니다", "✻ Worked for 3m · done", rule, "❯", rule, status_o]
    check("평소 끝난 창은 멈춤 아님", not sn.parse_screen(normal)["stalled"])
    # 2026-10-14 실측 화면: 계정을 갈아끼우다 열쇠가 겹쳐 로그인 만료로 멈춘 창(4개)
    auth = ["  Ran 1 shell command", "", "● Login expired · Please run /login", "",
            "✻ Churned for 1m 5s · done 오전 8:24", rule, ">", rule, status_o]
    va = sn.parse_screen(auth)
    check("로그인 만료 화면 = 멈춤(모델 칸 한도 아님)",
          va["stalled"] and va["input_empty"] and va["limit_bucket"] is None, va)
    check("로그인 만료 뒤 이어가기를 넣었으면 멈춤 아님",
          not sn.parse_screen(auth[:3] + ["> " + sn.NUDGE_LINE, rule, ">", rule, status_o])["stalled"])
    quoted = ["● 화면에 Login expired · Please run /login 이 떴다고 하셨는데", rule, ">", rule, status_o]
    check("답변 속에 인용된 로그인 만료 문구는 멈춤 아님", not sn.parse_screen(quoted)["stalled"])
    check("기록이 로그인 만료(authentication_failed)로 끝나도 멈춤",
          sn.stall_info([{"type": "assistant", "message": {"model": "claude-opus-5-5"}},
                         {"type": "assistant", "isApiErrorMessage": True, "error": "authentication_failed",
                          "uuid": "u1", "timestamp": "2026-10-13T23:24:49Z",
                          "message": {"model": "<synthetic>"}}]) is not None)

    # 판단 흐름(화면 읽기·입력 넣기를 가짜로 바꿔 끼운다)
    home = Path(tmp) / "nudge_home"
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    state_file = Path(tmp) / "nudge_state.json"
    now = time.time()
    screens = {1: {"title": "마광수 강의 편집 변환", "lines": session_hit},
               2: {"title": "평소 창", "lines": normal},
               3: {"title": "일하는 창", "lines": busy}}
    typed = []

    def run(usable=lambda m: True, t=now, typer=None):
        return sn.nudge_stalled(usable, log=lambda *_: None, now=t, home=home, state_file=state_file,
                                windows=lambda: [1, 2, 3], reader=lambda ps: screens,
                                typer=typer or (lambda pid: typed.append(pid) or {"ok": True}),
                                start_of=lambda pid: 111)

    check("멈춘 창에만 넣는다", run() == ["마광수 강의 편집 변환"] and typed == [1], typed)
    check("10분 안엔 다시 안 넣는다", run(t=now + 300) == [])
    check("지금 계정도 막혀 있으면 안 넣는다", run(usable=lambda m: False, t=now + 700) == [])
    models = []
    run(usable=lambda m: models.append(m) or False, t=now + 700)
    check("모델은 화면 상태줄에서 읽어 넘긴다", bool(models) and models[0].startswith("Opus"), models)
    check("넣기 실패면 넣은 것으로 안 친다",
          run(t=now + 700, typer=lambda pid: {"ok": False, "why": "그 사이 상태가 바뀜"}) == [])
    check("다음 회차에 넣는다", run(t=now + 800) == ["마광수 강의 편집 변환"])
    for i in range(10):
        run(t=now + 800 + 700 * (i + 1))
    check(f"하루 {sn.DAY_CAP}통을 넘기지 않는다", typed.count(1) == sn.DAY_CAP, typed.count(1))

    # 등록된 창은 대화 기록도 한도 거절로 끝나 있어야 넣는다
    proj = home / "projects" / sn._slug("C:\\work")
    proj.mkdir(parents=True, exist_ok=True)
    (home / "sessions" / "9.json").write_text(
        json.dumps({"pid": 9, "sessionId": "s9", "cwd": "C:\\work"}), encoding="utf-8")
    tx = proj / "s9.jsonl"
    tx.write_text(json.dumps({"type": "assistant", "uuid": "a1",
                              "message": {"model": "claude-opus-5", "content": []}}) + "\n", encoding="utf-8")
    typed.clear()

    def run9(sf):
        return sn.nudge_stalled(lambda m: True, log=lambda *_: None, now=now, home=home,
                                state_file=Path(tmp) / sf, windows=lambda: [9],
                                reader=lambda ps: {9: {"title": "등록창", "lines": session_hit}},
                                typer=lambda pid: typed.append(pid) or {"ok": True}, start_of=lambda pid: 1)

    check("기록은 이어졌는데 화면에 옛 문구만 남았으면 안 넣는다", run9("n2.json") == [] and typed == [])
    tx.write_text(json.dumps({"type": "assistant", "uuid": "a2", "isApiErrorMessage": True,
                              "error": "rate_limit",
                              "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 60)),
                              "message": {"model": "<synthetic>", "content": []}}) + "\n", encoding="utf-8")
    check("기록도 한도로 끝났으면 넣는다", run9("n3.json") == ["등록창"])
    check("이어가기 줄은 한 줄이다(줄바꿈이면 중간에 먼저 보내진다)", "\n" not in sn.NUDGE_LINE)

    # 검수 반영: 한도 문구를 인용만 한 화면은 멈춤이 아니다
    quoted = ["● 로그를 보니 \"You've hit your session limit\" 문구가 찍혀 있습니다.",
              "  예전 기록이라 지금은 문제없습니다.", rule, "❯", rule, status_o]
    check("답변이 한도 문구를 인용하면 멈춤 아님", not sn.parse_screen(quoted)["stalled"])
    tool_quote = ["● Bash(tail rotator.log)",
                  "  ⎿  2026-09-10 20:33:03 이어가기: 멈춘 창 (You've reached your Fable limit. Run /usage-credits)",
                  rule, "❯", rule, status_o]
    check("도구 결과 한가운데 인용도 멈춤 아님", not sn.parse_screen(tool_quote)["stalled"])
    check("한도 문구에서 모델 이름을 뽑는다", sn.parse_screen(stalled)["limit_bucket"] == "Fable")
    check("세션 한도 문구엔 모델 이름이 없다", sn.parse_screen(session_hit)["limit_bucket"] is None)

    # 등록 파일은 창 시작 시각이 맞아야 믿는다(번호 재사용)
    (home / "sessions" / "9.json").write_text(json.dumps(
        {"pid": 9, "sessionId": "s9", "cwd": "C:\\work", "procStart": 5_000_000_000}), encoding="utf-8")
    tx.write_text(json.dumps({"type": "assistant", "uuid": "a3",
                              "message": {"model": "claude-opus-5", "content": []}}) + "\n", encoding="utf-8")

    def run_reg(sf, start):
        typed.clear()
        return sn.nudge_stalled(lambda m: True, log=lambda *_: None, now=now, home=home,
                                state_file=Path(tmp) / sf, windows=lambda: [9],
                                reader=lambda ps: {9: {"title": "재사용", "lines": session_hit}},
                                typer=lambda pid: typed.append(pid) or {"ok": True}, start_of=lambda pid: start)

    check("시작 시각이 다른 등록 파일은 남의 것으로 보고 화면만으로 판단한다", run_reg("n4.json", 1) == ["재사용"])
    check("시작 시각이 맞으면 기록으로 한 번 더 확인해 막는다", run_reg("n5.json", 5_000_000_000) == [])

    # 12시간 제한은 멈춤을 처음 본 때부터 센다(등록 안 된 창에도 걸린다)
    sf6 = Path(tmp) / "n6.json"
    typed.clear()

    def run6(t, usable=lambda m: True):
        return sn.nudge_stalled(usable, log=lambda *_: None, now=t, home=home, state_file=sf6,
                                windows=lambda: [7],
                                reader=lambda ps: {7: {"title": "오래 멈춘 창", "lines": stalled}},
                                typer=lambda pid: typed.append(pid) or {"ok": True}, start_of=lambda pid: 1)

    run6(now, usable=lambda m: False)      # 처음 본다(계정이 막혀 있어 넣지 않음)
    check("처음 본 지 12시간 넘은 멈춤엔 안 넣는다", run6(now + 13 * 3600) == [] and typed == [], typed)
    check("이틀이 지나도 계속 멈춰 있으면 처음 본 시각을 지우지 않는다(다시 넣지 않는다)",
          run6(now + 50 * 3600) == [] and run6(now + 50 * 3600 + 700) == [] and typed == [], typed)

    # 멈춤 서명은 창 크기가 바뀌어 앞 줄이 달라져도 같고, 멈춤이 풀리면 처음 본 시각을 지운다
    rewrapped = ["● 창 폭이 바뀌어 다르게 접힌 줄", "  이어지는 줄"] + stalled[2:]
    check("앞 줄이 달라져도 같은 멈춤 서명", sn.parse_screen(rewrapped)["sig"] == sn.parse_screen(stalled)["sig"])
    sf7 = Path(tmp) / "n7.json"
    scr7 = {7: {"title": "창7", "lines": stalled}}

    def run7(t, lines):
        scr7[7]["lines"] = lines
        return sn.nudge_stalled(lambda m: False, log=lambda *_: None, now=t, home=home, state_file=sf7,
                                windows=lambda: [7], reader=lambda ps: scr7,
                                typer=lambda pid: {"ok": True}, start_of=lambda pid: 1)

    run7(now, stalled)
    check("멈춘 창의 처음 본 시각을 적는다", len(json.loads(sf7.read_text(encoding="utf-8"))["seen"]) == 1)
    run7(now + 60, normal)
    check("멈춤이 풀리면 처음 본 시각을 지운다", json.loads(sf7.read_text(encoding="utf-8"))["seen"] == {})

    # 2026-09-13: 창 표시로 찾은 그 창의 자격을 usable 에 넘긴다. 모델이 같아도 창마다 계정이 다르다
    # (Fable 자리에서 Opus 로 바꾼 창은 Opus 자리 계정이 아니라 Fable 자리 계정을 쓴다).
    asked, typed8 = [], []

    def usable8(m, where=None):
        asked.append(where)
        return where == "shared"

    got8 = sn.nudge_stalled(usable8, log=lambda *_: None, now=now, home=home, state_file=Path(tmp) / "n8.json",
                            windows=lambda: [21, 22],
                            reader=lambda ps: {21: {"title": "Fable 자리 창", "lines": session_hit},
                                               22: {"title": "공용 창", "lines": session_hit}},
                            typer=lambda pid: typed8.append(pid) or {"ok": True}, start_of=lambda pid: 1,
                            places=lambda ps: {21: "slot:Fable", 22: "shared"})
    check("창마다 그 창의 자격으로 묻는다", sorted(asked) == ["shared", "slot:Fable"], asked)
    check("막힌 자리의 창엔 안 넣고 쓸 수 있는 창에만 넣는다", got8 == ["공용 창"] and typed8 == [22], (got8, typed8))
    fake_state = {"session_claims": {
        "50": {"bucket": "Opus", "at": now, "slot": "Fable", "label": "A", "model": "Opus 5 (1M context)"},
        "61": {"bucket": "Opus", "at": now, "slot": None, "label": "D"}}}
    wp = sn.window_places([60, 61, 62], parents={60: 50, 61: 40, 62: 45}, state=fake_state)
    check("셸 번호로 적힌 표시(cc) → 그 자리, 제 번호 표시(가로채기) → 공용, 표시 없음 → 공용(표시 없음)",
          wp == {60: {"where": "slot:Fable", "model": "Opus 5 (1M context)", "claimed": True},
                 61: {"where": "shared", "model": None, "claimed": True},
                 62: {"where": "shared", "model": None, "claimed": False}}, wp)
    # 상태줄이 안 읽힌 창은 대화 기록(거절 전 모델)보다 창 표시의 모델을 먼저 쓴다(2026-09-13 세일즈부스터 창)
    no_status = session_hit[:-1]
    asked9 = []
    sn.nudge_stalled(lambda m, where=None: asked9.append((m, where)) or False, log=lambda *_: None, now=now,
                     home=home, state_file=Path(tmp) / "n9.json", windows=lambda: [31],
                     reader=lambda ps: {31: {"title": "상태줄 없는 창", "lines": no_status}},
                     typer=lambda pid: {"ok": True}, start_of=lambda pid: 1,
                     places=lambda ps: {31: {"where": "slot:Fable", "model": "Opus 5 (1M context)"}})
    check("상태줄이 없으면 창 표시의 모델(Opus)로 묻는다",
          asked9 == [("Opus 5 (1M context)", "slot:Fable")], asked9)




def case_slot_pool(tmp):
    """[58] 모델별 자리(slot): 창마다 다른 계정 (2026-09-12).

    Opus 자리와 Fable 자리가 서로 다른 계정에 앉고, 한 자리가 바닥나도 다른 자리는 안 건드린다.
    자리 창의 표시는 공용 자격의 계정 선택(claimed_buckets)에 끼지 않는다.
    """
    print()
    print("[58] 모델별 자리: 창마다 다른 계정")
    import slot_pool as sp
    import usage_check as uc

    tmp = Path(tmp)
    setup(tmp, ["A", "B", "C"], "A")
    os.environ.pop("AIROTATOR_NO_SLOTS", None)   # main 이 꺼 둔 것을 이 케이스에서만 켠다
    if os.name != "nt":
        os.environ["AIROTATOR_NO_SLOTS"] = "1"
        check("Windows 가 아니면 자리 기능이 꺼진다", not sp.enabled())
        return
    real = (sp.POOL_ROOT, sp.STORE_ROOT, sp.CLAUDE_HOME, sp.LOG_FILE, uc.get_usage, uc.last_status,
            claude_pool.usage_check, st._pid_alive, sp._has_window)
    sp._has_window = lambda keys, procs=None: keys   # 가짜 번호(7001…)는 실제 프로세스 목록에 없다
    sp.POOL_ROOT, sp.STORE_ROOT, sp.LOG_FILE = tmp / "pool", tmp / "creds" / "slots", tmp / "rotator.log"
    sp.CLAUDE_HOME = tmp / "home"
    (sp.CLAUDE_HOME / "projects").mkdir(parents=True)
    (sp.CLAUDE_HOME / "settings.json").write_text('{"model":"opus"}', encoding="utf-8")
    now = time.time()

    def cred(label):
        return {"claudeAiOauth": {"accessToken": "x", "refreshToken": f"rt-{label}",
                                  "expiresAt": int((now + 3600) * 1000)}}

    for b in ("Opus", "Fable"):
        for l in ("A", "B", "C"):
            sa._write_owner_only(sp.store_path(b, l), cred(l))
    alive = set()
    st._pid_alive = lambda pid: str(pid) in alive
    table = {}
    uc.get_usage = lambda path: table.get(Path(path).stem)
    claude_pool.usage_check = uc
    free = {"weekly_pct": 10.0, "session_pct": 5.0, "weekly_reset": now + 48 * 3600, "session_reset": now + 3600,
            "scoped": {"Fable": {"pct": 5.0, "reset": now + 48 * 3600}, "Opus": {"pct": 5.0, "reset": now + 48 * 3600}}}
    fable_full = dict(free, scoped={"Fable": {"pct": 100.0, "reset": now + 48 * 3600},
                                    "Opus": {"pct": 5.0, "reset": now + 48 * 3600}})
    try:
        # (1) 처음 켤 때: 자리 폴더가 만들어지고 앞순위 계정이 앉는다
        table.update({"A": free, "B": free, "C": free})
        rc, path = sp.prepare("Opus")
        check("자리 준비: 갈아탐(3) + 폴더 경로", rc == 3 and path == str(sp.slot_dir("Opus")), (rc, path))
        import subprocess as _sub
        real_sub = getattr(_sub.run, "__module__", "") == "subprocess"   # 앞선 케이스가 가짜 run 을 꽂아 두면 junction 이 안 만들어진다
        check("자리 폴더에 자격·설정 사본·projects 연결이 있다",
              sp.live_path("Opus").exists() and (sp.slot_dir("Opus") / "settings.json").exists()
              and ((sp.slot_dir("Opus") / "projects").exists() or not real_sub))
        st_now = st.load_state()
        check("Opus 자리 계정 A", sp.slot_current(st_now, "Opus") == "A", sp.slot_current(st_now, "Opus"))
        # (2) Fable 자리: A 는 Fable 이 바닥 → B 에 앉는다(Opus 자리는 그대로 A)
        table.update({"A": fable_full})
        rc, path = sp.prepare("Fable")
        st_now = st.load_state()
        check("Fable 자리는 Fable 이 남은 B", sp.slot_current(st_now, "Fable") == "B", sp.slot_current(st_now, "Fable"))
        check("Opus 자리는 그대로 A", sp.slot_current(st_now, "Opus") == "A")
        check("A 는 Fable 칸만 막힌다", st.is_scoped_blocked(st_now, "A", "Fable") and not st.is_blocked(st_now, "A"))
        # (3) 창 표시: 자리 창은 공용 계정 선택에 안 낀다
        os.environ["CLAUDE_CONFIG_DIR"] = str(sp.slot_dir("Fable"))
        alive.add("7001")
        claude_pool.claim_session(7001, model="claude-fable-5-1")
        del os.environ["CLAUDE_CONFIG_DIR"]
        st_now = st.load_state()
        cl = st_now["session_claims"]["7001"]
        check("표시에 자리·계정이 적힌다", cl.get("slot") == "Fable" and cl.get("label") == "B", cl)
        check("공용 창 목록에는 안 잡힌다", st.claimed_buckets(st_now) == (), st.claimed_buckets(st_now))
        check("자리 창 목록에는 잡힌다", sp.slot_claims(st_now, "Fable") == ["7001"], sp.slot_claims(st_now, "Fable"))
        # (4) 창 지키기: Fable 자리의 B 가 97% 넘으면 C 로, Opus 자리는 안 건드린다
        table.update({"B": dict(free, scoped={"Fable": {"pct": 98.0, "reset": now + 48 * 3600},
                                              "Opus": {"pct": 5.0, "reset": now + 48 * 3600}})})
        switched = sp.tend(claude_pool.PREEMPT_PCT)
        st_now = st.load_state()
        check("창 지키기가 Fable 자리를 C 로 갈아끼운다", switched == ["Fable"] and sp.slot_current(st_now, "Fable") == "C",
              (switched, sp.slot_current(st_now, "Fable")))
        check("Opus 자리는 그대로 A", sp.slot_current(st_now, "Opus") == "A")
        check("자리 자격 파일이 C 것", sa._obj_fingerprint(sp._load(sp.live_path("Fable"))) == sa._obj_fingerprint(cred("C")))
        # (5) 되저장: 자리 안에서 갱신된 자격은 저장본으로 돌아간다
        newer = cred("C"); newer["claudeAiOauth"]["expiresAt"] += 999_000; newer["claudeAiOauth"]["refreshToken"] = "rt-C2"
        sp._put_live("Fable", newer)
        note = sp.sync_creds("Fable", "C")
        check("갱신분 되저장", "되저장" in note and sa._obj_fingerprint(sp._load(sp.store_path("Fable", "C"))) == sa._obj_fingerprint(newer), note)
        # (6) 갈아탈 데 없으면 2, 그대로
        table.update({"A": fable_full, "B": fable_full, "C": fable_full})
        rc, cur = sp.ensure("Fable", threshold=100.0)
        check("전부 바닥이면 갈아탈 데 없음(2), 계정 그대로 C", rc == 2 and cur == "C", (rc, cur))
        # (7) 이어가기 판단은 자리 계정 기준
        check("자리 창의 모델 사용 가능 판단 = 자리 계정(C, Fable 바닥) → False",
              sp.usable_now("Fable", claude_pool.PREEMPT_PCT) is False)
        check("자리에 창이 없는 칸은 None(공용 판단으로)", sp.usable_now("Opus", claude_pool.PREEMPT_PCT) is None)
        # (8) 켤 때 되돌리기(2026-09-12): Opus 자리가 B 인데 A 의 주간 초기화가 더 이르고 Opus 가 남았으면 A 로
        a_soon = dict(free, weekly_reset=now + 6 * 3600)
        table.update({"A": a_soon, "B": dict(free, weekly_reset=now + 72 * 3600),
                      "C": dict(free, weekly_reset=now + 96 * 3600)})
        with st.with_lock():
            s2 = st.load_state()
            sp.switch(s2, "Opus", "B", "시험")
            st.save_state(s2)
        rc, _ = sp.prepare("Opus", reclaim=False)
        check("창이 끝난 뒤 확인(--slot-check)은 되돌리지 않는다",
              rc == 0 and sp.slot_current(st.load_state(), "Opus") == "B")
        rc, _ = sp.prepare("Opus")
        st_now = st.load_state()
        check("켤 때 초기화가 이른 A 로 되돌린다(종료코드 0)", rc == 0 and sp.slot_current(st_now, "Opus") == "A",
              (rc, sp.slot_current(st_now, "Opus")))
        check("자리 자격 파일이 A 것", sa._obj_fingerprint(sp._load(sp.live_path("Opus"))) == sa._obj_fingerprint(cred("A")))
        check("Fable 자리는 안 건드린다", sp.slot_current(st_now, "Fable") == "C")
        # (9) 초기화가 일러도 그 칸이 97% 이상이면 안 옮기고 기록에 막는다
        with st.with_lock():
            s2 = st.load_state()
            sp.switch(s2, "Opus", "B", "시험")
            st.save_state(s2)
        table.update({"A": dict(a_soon, scoped={"Fable": {"pct": 100.0, "reset": now + 48 * 3600},
                                                "Opus": {"pct": 98.0, "reset": now + 6 * 3600}})})
        rc, _ = sp.prepare("Opus")
        st_now = st.load_state()
        check("A 의 Opus 가 98% 면 B 그대로", rc == 0 and sp.slot_current(st_now, "Opus") == "B",
              (rc, sp.slot_current(st_now, "Opus")))
        check("A 의 Opus 칸을 기록에 막아 다음 켤 때는 안 읽는다", st.is_scoped_blocked(st_now, "A", "Opus"))
        # (10) 초기화가 이른 후보의 사용량을 못 읽으면 옮기지 않는다
        with st.with_lock():
            s2 = st.load_state()
            st.note_weekly_reset(s2, "C", now + 3 * 3600)
            st.save_state(s2)
        table.update({"C": None})
        rc, _ = sp.prepare("Opus")
        st_now = st.load_state()
        check("C 사용량을 못 읽으면 B 그대로", rc == 0 and sp.slot_current(st_now, "Opus") == "B",
              (rc, sp.slot_current(st_now, "Opus")))
        # (11) 지금 계정이 가장 이르면 아무것도 안 읽고 그대로
        table.update({"C": dict(free, weekly_reset=now + 96 * 3600), "B": dict(free, weekly_reset=now + 1 * 3600)})
        reads = []
        orig_get = uc.get_usage
        uc.get_usage = lambda path: (reads.append(Path(path).stem), orig_get(path))[1]
        rc, _ = sp.prepare("Opus")
        uc.get_usage = orig_get
        check("지금 계정 B 가 가장 이르면 B 그대로, 다른 계정은 안 읽는다",
              sp.slot_current(st.load_state(), "Opus") == "B" and reads == ["B"], (reads,))
        # (12) 2026-09-13 실사고: Fable 자리에 Fable 창 하나(7001) + Opus 로 바꾼 창 둘. A 가 5시간 한도,
        #      B·C 는 Fable 칸만 바닥. 옛 코드는 "Fable 이 남은 계정이 없다" 며 A 에 3시간 넘게 눌러앉았다.
        with st.with_lock():
            s2 = st.load_state()
            s2["blocked"], s2["scoped_blocked"] = {}, {}
            for l in ("B", "C"):
                st.mark_scoped_blocked(s2, l, "Fable", now + 48 * 3600)
            sp.switch(s2, "Fable", "A", "시험")
            for pid in ("7002", "7003"):
                alive.add(pid)
                st.note_session_claim(s2, pid, "Opus", "A", model="Opus 5 (1M context)", slot="Fable")
            st.save_state(s2)
        table.update({"A": dict(free, session_pct=100.0, session_reset=now + 2 * 3600),
                      "B": fable_full, "C": fable_full})
        check("채울 칸 = 자리 칸 Fable + 그 자리 창들이 쓰는 Opus",
              sp.slot_buckets(st.load_state(), "Fable") == ("Fable", "Opus"), sp.slot_buckets(st.load_state(), "Fable"))
        switched = sp.tend(claude_pool.PREEMPT_PCT)
        got = sp.slot_current(st.load_state(), "Fable")
        check("A 가 5시간 한도면 Fable 은 어디서도 못 써도 Opus 창을 위해 B·C 로 갈아끼운다",
              switched == ["Fable"] and got in ("B", "C"), (switched, got))
        check("자리 자격 파일도 그 계정 저장본",
              sa._obj_fingerprint(sp._load(sp.live_path("Fable"))) == sa._obj_fingerprint(sp._load(sp.store_path("Fable", got))))
        check("창이 끝난 뒤 확인(--slot-check)은 3 이 아니다(Fable 창을 --continue 로 되켜지 않게)",
              sp.prepare("Fable", reclaim=False)[0] == 2)
        # (13) A 의 5시간이 풀리면 Fable·Opus 둘 다 되는 A 로 돌아오고, 다음 회차엔 그대로(왕복 없음)
        with st.with_lock():
            s2 = st.load_state()
            s2["blocked"] = {}
            st.save_state(s2)
        table.update({"A": dict(free, scoped={"Fable": {"pct": 70.0, "reset": now + 48 * 3600},
                                              "Opus": {"pct": 5.0, "reset": now + 48 * 3600}})})
        sp.tend(claude_pool.PREEMPT_PCT)
        check("A 가 풀리면 A 로 돌아온다", sp.slot_current(st.load_state(), "Fable") == "A",
              sp.slot_current(st.load_state(), "Fable"))
        again = sp.tend(claude_pool.PREEMPT_PCT)
        check("다음 회차엔 그대로 A", again == [] and sp.slot_current(st.load_state(), "Fable") == "A", again)
        # (14) Opus 칸만 A 에서 바닥이고 두 칸을 다 채울 계정이 없으면, 멀쩡한 Fable 창을 밀지 않는다
        table.update({"A": dict(free, scoped={"Fable": {"pct": 10.0, "reset": now + 48 * 3600},
                                              "Opus": {"pct": 100.0, "reset": now + 48 * 3600}})})
        rc, cur = sp.ensure("Fable", threshold=claude_pool.PREEMPT_PCT)
        check("두 칸 다 채울 계정이 없으면 그대로 A(종료코드 0)", rc == 0 and cur == "A", (rc, cur))
        check("A 의 Opus 칸은 기록에 막힌다", st.is_scoped_blocked(st.load_state(), "A", "Opus"))
        # (15) 둘 다 되는 계정이 생기면 옮기되, 자리 칸(Fable)은 멀쩡했으니 종료코드 0.
        #      C 는 기록을 지워 멀쩡해 보이지만 실제로는 Fable 이 바닥이다(기록 낡음). 그래서 A → C → B 로
        #      두 번 옮기는데, 두 번째 이동(C 의 Fable 소진)을 3 으로 세면 안 된다. 들어올 때 계정 A 는 멀쩡했다.
        with st.with_lock():
            s2 = st.load_state()
            s2["scoped_blocked"] = {"A": {"Opus": now + 48 * 3600}}
            st.save_state(s2)
        table.update({"B": free})
        rc, _ = sp.prepare("Fable", reclaim=False)
        check("다른 창의 칸 때문에 B 로 옮겨도 창이 끝난 뒤 확인은 0(닫은 창을 되켜지 않게)",
              rc == 0 and sp.slot_current(st.load_state(), "Fable") == "B",
              (rc, sp.slot_current(st.load_state(), "Fable")))
        # (16) 이어가기 판단: 창 표시로 찾은 자리 계정의 그 창 모델 칸을 본다
        check("Fable 자리(B)의 Opus 창은 B 의 Opus 로 판단 → 쓸 수 있음",
              sp.usable_now("Fable", claude_pool.PREEMPT_PCT, model_bucket="Opus") is True)
        table.update({"B": dict(free, session_pct=100.0)})
        check("B 가 5시간 한도면 못 씀",
              sp.usable_now("Fable", claude_pool.PREEMPT_PCT, model_bucket="Opus") is False)
        check("claude_pool._usable_now 도 창의 자리 계정을 따른다(Opus 자리 A 가 멀쩡해도)",
              claude_pool._usable_now("Opus 5 (1M context)", "slot:Fable") is False)
        # (17) 검수 재현: 자리 칸(Fable)이 기록상 모든 계정에서 막혀도 기록이 낡았으면 실제로 읽어 풀어 준다.
        #      Fable 창 하나만, A 는 실제 Fable 바닥, B 는 기록만 막힘(실제 여유), C 는 실제 바닥.
        alive.discard("7002")
        alive.discard("7003")
        with st.with_lock():
            s2 = st.load_state()
            s2["blocked"] = {}
            s2["scoped_blocked"] = {"B": {"Fable": now + 48 * 3600}, "C": {"Fable": now + 48 * 3600}}
            sp.switch(s2, "Fable", "A", "시험")
            st.save_state(s2)
        table.update({"A": fable_full, "B": free, "C": fable_full})
        sp.tend(claude_pool.PREEMPT_PCT)
        st_now = st.load_state()
        check("기록만 막힌 B 를 실제로 읽어 풀고 B 로 간다", sp.slot_current(st_now, "Fable") == "B",
              sp.slot_current(st_now, "Fable"))
        check("실제 바닥인 C 의 기록은 그대로", st.is_scoped_blocked(st_now, "C", "Fable"))
        # (18) 유령 표시: 창이 끝난 셸의 표시는 자리 판단에서 뺀다(claude 자체이거나 claude 자식을 둔 셸만)
        procs = {50: (1, "powershell.exe"), 60: (50, "claude.exe"), 70: (1, "powershell.exe"), 80: (70, "node.exe")}
        check("claude 창이 딸린 셸·claude 자체만 센다",
              real[-1](["50", "60", "70", "80"], procs=procs) == ["50", "60"])
        # (19) 창이 끝난 뒤 확인(--slot-check): 끝난 창이 마지막으로 쓰던 모델 칸으로 3 을 정한다(검수 4번).
        #      7005 = Fable 로 켜서 Opus 로 바꾼 창(Fable 자리). 7001 Fable 창도 떠 있다.
        alive.add("7005")

        def seat_w(label, upd, scoped):
            with st.with_lock():
                s2 = st.load_state()
                s2["blocked"], s2["scoped_blocked"] = {}, scoped
                sp.switch(s2, "Fable", label, "시험")
                st.note_session_claim(s2, "7005", "Opus", label, model="Opus 5 (1M context)", slot="Fable")
                st.save_state(s2)
            table.update(upd)

        seat_w("C", {"A": free, "C": fable_full}, {"B": {"Fable": now + 48 * 3600}})
        rc, _ = sp.prepare("Fable", reclaim=False, window_bucket="Opus")
        check("Opus 로 쓰다 닫은 창은 그 계정 Fable 이 바닥이어도 되켜지 않는다(0)", rc == 0, rc)
        opus_full = dict(free, scoped={"Fable": {"pct": 5.0, "reset": now + 48 * 3600},
                                       "Opus": {"pct": 100.0, "reset": now + 48 * 3600}})
        seat_w("A", {"A": opus_full, "B": free, "C": free}, {})
        rc, _ = sp.prepare("Fable", reclaim=False)
        check("옛 방식(켤 때 모델 Fable)으로 보면 Opus 한도로 멈춘 창을 못 알아본다(0)", rc == 0, rc)
        seat_w("A", {"A": opus_full, "B": free, "C": free}, {})
        rc, _ = sp.prepare("Fable", reclaim=False, window_bucket="Opus")
        got = sp.slot_current(st.load_state(), "Fable")
        check("Opus 한도로 멈춘 창은 3(되켜짐), Opus 가 되는 계정으로 옮김", rc == 3 and got in ("B", "C"), (rc, got))
        # (20) cc 가 넘기는 --window 번호로 창 표시의 지금 모델 칸을 찾고, 명령으로도 같은 판단을 한다
        check("창 표시 번호로 모델 칸을 찾는다(없는 번호는 None)",
              claude_pool._claimed_bucket("7005") == "Opus" and claude_pool._claimed_bucket("99999") is None)
        seat_w("A", {"A": opus_full, "B": free, "C": free}, {})
        import contextlib
        import io
        import sys as _sys
        old_argv = _sys.argv
        _sys.argv = ["claude_pool.py", "--slot-check", "--window", "7005", "--model", "claude-fable-5-1"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = claude_pool.main()
        finally:
            _sys.argv = old_argv
        check("명령 --slot-check --window 도 같은 판단(3)", rc == 3, rc)
        # (21) 2026-09-23 실사고: Fable 자리 A 가 100%, B 98%·C 97% 는 97% 기준에 걸려 막힌 기록.
        #      옛 코드는 "갈아탈 계정이 없다" 로 A 에 눌러앉았다. 100% 미만 중 가장 여유 있는 C 로 가야 한다.
        alive.discard("7005")

        def seat_lr(label, upd, scoped):
            with st.with_lock():
                s2 = st.load_state()
                s2["blocked"], s2["scoped_blocked"] = {}, scoped
                sp._slots(s2).setdefault("Fable", {}).pop("last_resort_at", None)
                sp.switch(s2, "Fable", label, "시험")
                st.save_state(s2)
            table.update(upd)

        def fab(pct):
            return dict(free, scoped={"Fable": {"pct": pct, "reset": now + 48 * 3600},
                                      "Opus": {"pct": 5.0, "reset": now + 48 * 3600}})

        seat_lr("A", {"A": fab(100.0), "B": fab(98.0), "C": fab(97.0)},
                {"B": {"Fable": now + 48 * 3600}, "C": {"Fable": now + 48 * 3600}})
        switched = sp.tend(claude_pool.PREEMPT_PCT)
        st_now = st.load_state()
        check("A 100% 면 97~98% 계정 중 가장 여유 있는 C 로 간다(마지막 수단)",
              switched == ["Fable"] and sp.slot_current(st_now, "Fable") == "C",
              (switched, sp.slot_current(st_now, "Fable")))
        check("자리 자격 파일이 C 것",
              sa._obj_fingerprint(sp._load(sp.live_path("Fable"))) == sa._obj_fingerprint(sp._load(sp.store_path("Fable", "C"))))
        again = sp.tend(claude_pool.PREEMPT_PCT)
        check("C 가 97% 여도 100% 전엔 다시 안 옮긴다(왕복 없음)",
              again == [] and sp.slot_current(st.load_state(), "Fable") == "C", again)
        check("이어가기 판단은 100% 기준: C 97% 면 쓸 수 있음",
              claude_pool._usable_now("claude-fable-5-1", "slot:Fable") is True)
        table.update({"C": fab(100.0)})
        with st.with_lock():
            s2 = st.load_state()
            sp._slots(s2).setdefault("Fable", {}).pop("last_resort_at", None)
            st.save_state(s2)
        switched = sp.tend(claude_pool.PREEMPT_PCT)
        check("C 도 100% 가 되면 남은 B(98%) 로", sp.slot_current(st.load_state(), "Fable") == "B",
              sp.slot_current(st.load_state(), "Fable"))
        # 지금 계정 조회가 429 로 막혀도 마지막 기록이 100% 면 마지막 수단을 쓴다(2026-09-23 실측)
        seat_lr("A", {"A": None, "B": fab(98.0), "C": fab(97.0)},
                {"B": {"Fable": now + 48 * 3600}, "C": {"Fable": now + 48 * 3600}})
        with st.with_lock():
            s2 = st.load_state()
            st.note_usage(s2, "A", fab(100.0))
            st.save_state(s2)
        sp.tend(claude_pool.PREEMPT_PCT)
        check("지금 계정을 못 읽어도 기록이 100% 면 C 로", sp.slot_current(st.load_state(), "Fable") == "C",
              sp.slot_current(st.load_state(), "Fable"))
        seat_lr("A", {"A": None, "B": fab(98.0), "C": fab(97.0)},
                {"A": {"Fable": now + 48 * 3600}, "B": {"Fable": now + 48 * 3600}, "C": {"Fable": now + 48 * 3600}})
        with st.with_lock():
            s2 = st.load_state()
            s2["usage_seen"] = {}
            st.save_state(s2)
        sp.tend(claude_pool.PREEMPT_PCT)
        check("못 읽고 기록도 없으면 그대로 A(모르면 안 움직인다)", sp.slot_current(st.load_state(), "Fable") == "A",
              sp.slot_current(st.load_state(), "Fable"))
        check("기록의 초기화 시각이 지났으면 100% 로 안 본다",
              not sp.seen_full({"usage_seen": {"A": {"scoped": {"Fable": {"pct": 100.0, "reset": now - 60}}}}}, "A", "Fable"))
        # 창이 끝난 뒤 확인(--slot-check)의 종료코드(검수 재현)
        alive.discard("7001")
        seat_lr("A", {"A": fab(100.0), "B": fab(98.0), "C": fab(97.0)},
                {"B": {"Fable": now + 48 * 3600}, "C": {"Fable": now + 48 * 3600}})
        rc, _ = sp.prepare("Fable", reclaim=False, window_bucket="Fable")
        check("Fable 한도로 끝난 창(A 100%)은 97~98% 계정으로 옮기고 3(되켜짐)",
              rc == 3 and sp.slot_current(st.load_state(), "Fable") in ("B", "C"), (rc, sp.slot_current(st.load_state(), "Fable")))
        seat_lr("A", {"A": fab(50.0), "B": fab(98.0), "C": fab(100.0)},
                {"B": {"Fable": now + 48 * 3600}})
        rc, _ = sp.prepare("Fable", reclaim=False, window_bucket="Fable")
        check("들어올 때 A 가 멀쩡했으면 마지막 수단이 돌아도 3 이 아니다(닫은 창 안 되켬)", rc != 3, rc)
        alive.add("7001")
        # 전부 100% 면 그대로(2), 빈손이면 10분은 다시 안 읽는다
        seat_lr("A", {"A": fab(100.0), "B": fab(100.0), "C": fab(100.0)},
                {"B": {"Fable": now + 48 * 3600}, "C": {"Fable": now + 48 * 3600}})
        rc, cur = sp.ensure("Fable", threshold=claude_pool.PREEMPT_PCT)
        check("전부 100% 면 그대로 A(2)", rc == 2 and cur == "A", (rc, cur))
        reads = []
        orig_get = uc.get_usage
        uc.get_usage = lambda path: (reads.append(Path(path).stem), orig_get(path))[1]
        sp.ensure("Fable", threshold=claude_pool.PREEMPT_PCT)
        uc.get_usage = orig_get
        check("빈손 직후엔 다른 계정을 다시 안 읽는다", not ({"B", "C"} & set(reads)), reads)
    finally:
        (sp.POOL_ROOT, sp.STORE_ROOT, sp.CLAUDE_HOME, sp.LOG_FILE, uc.get_usage, uc.last_status,
         claude_pool.usage_check, st._pid_alive, sp._has_window) = real
        os.environ["AIROTATOR_NO_SLOTS"] = "1"
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        claude_pool.release_session(7001)


def case_slot_fresh_switch(tmp):
    """[60] 자리 교체 때 열쇠 경합 (2026-10-14 08:24 Opus 자리 B → A, 창이 Login expired).

    접근 열쇠가 끝난 저장본을 그대로 끼우면 창과 순환기가 같은 재발급 열쇠로 동시에 갱신해 창이 진다.
    끼우기 전에 저장본을 갱신하고, 꽂힌 자격은 순환기가 갱신하지 않으며, 열쇠가 죽은 계정은 건너뛴다.
    """
    print()
    print("[60] 자리 교체 때 열쇠 경합")
    import slot_pool as sp
    import usage_check as uc
    import cred_refresh as cr

    tmp = Path(tmp)
    setup(tmp, ["A", "B", "C"], "A")
    os.environ.pop("AIROTATOR_NO_SLOTS", None)
    if os.name != "nt":
        os.environ["AIROTATOR_NO_SLOTS"] = "1"
        return
    real = (sp.POOL_ROOT, sp.STORE_ROOT, sp.CLAUDE_HOME, sp.LOG_FILE, uc.get_usage, uc.last_status,
            uc._CACHE_FILE, cr.refresh_file, st._pid_alive, sp._has_window)
    sp._has_window = lambda keys, procs=None: keys
    sp.POOL_ROOT, sp.STORE_ROOT, sp.LOG_FILE = tmp / "pool", tmp / "creds" / "slots", tmp / "rotator.log"
    sp.CLAUDE_HOME = tmp / "home"
    (sp.CLAUDE_HOME / "projects").mkdir(parents=True)
    (sp.CLAUDE_HOME / "settings.json").write_text('{"model":"opus"}', encoding="utf-8")
    uc._CACHE_FILE = tmp / "usage-cache.json"
    st._pid_alive = lambda pid: False
    now = time.time()

    def cred(rt, hours):
        return {"claudeAiOauth": {"accessToken": "x", "refreshToken": rt,
                                  "expiresAt": int((now + hours * 3600) * 1000)}}

    refreshed = []
    dead = set()

    def fake_refresh(path):
        label = Path(path).stem
        refreshed.append(label)
        if label in dead:
            cr.last_error = "갱신 거절(HTTP 400)"
            return False
        sa._write_owner_only(Path(path), cred(f"rt-{label}-new", 8))
        return True

    cr.refresh_file = fake_refresh
    free = {"weekly_pct": 10.0, "session_pct": 5.0, "weekly_reset": now + 48 * 3600, "session_reset": now + 3600,
            "scoped": {"Fable": {"pct": 5.0, "reset": now + 48 * 3600}, "Opus": {"pct": 5.0, "reset": now + 48 * 3600}}}
    full = dict(free, scoped={"Fable": {"pct": 5.0, "reset": now + 48 * 3600},
                              "Opus": {"pct": 100.0, "reset": now + 48 * 3600}})
    table = {}

    def fake_get(path):
        u = table.get(Path(path).stem)
        uc.last_status = None if u else 401
        return u

    uc.get_usage = fake_get

    def seat(label):
        with st.with_lock():
            s2 = st.load_state()
            sp.switch(s2, "Opus", label, "시험")
            st.save_state(s2)

    try:
        sa._write_owner_only(sp.store_path("Opus", "A"), cred("rt-A", 6))
        sa._write_owner_only(sp.store_path("Opus", "B"), cred("rt-B", -12))   # 열쇠가 12시간 전에 끝난 저장본
        sa._write_owner_only(sp.store_path("Opus", "C"), cred("rt-C", 6))
        seat("A")
        refreshed.clear()
        # (1) A 가 바닥 → B 로 갈 때 B 저장본을 끼우기 전에 갱신한다(낡은 열쇠가 창에 들어가지 않는다)
        table.update({"A": full, "B": free, "C": free})
        rc, cur = sp.ensure("Opus", threshold=claude_pool.PREEMPT_PCT)
        live = sp._load(sp.live_path("Opus"))
        check("바닥난 A 에서 B 로 간다", cur == "B", (rc, cur))
        check("끼우기 전에 B 저장본을 갱신했다", refreshed[:1] == ["B"], refreshed)
        check("창에 꽂힌 자격은 갱신된 열쇠(옛 열쇠 아님)",
              (live or {}).get("claudeAiOauth", {}).get("refreshToken") == "rt-B-new")
        # (2) 꽂힌 계정(B)은 401 이어도 순환기가 갱신하지 않는다. 안 꽂힌 계정(C)은 갱신한다
        refreshed.clear()
        table.pop("B"); table.pop("C")
        sp._read_usage("Opus", "B")
        check("꽂힌 계정은 401 이어도 순환기가 갱신하지 않는다", "B" not in refreshed, refreshed)
        sa._write_owner_only(sp.store_path("Opus", "C"), cred("rt-C", -1))
        sp._read_usage("Opus", "C")
        check("안 꽂힌 계정은 401 이면 갱신한다", "C" in refreshed, refreshed)
        # (3) 갱신 직후 다시 읽기가 실패 기억에 막히지 않는다(10/13 실패 기억이 막아 3분간 폐기된 열쇠가 남았다)
        uc._cache_save({"C@Opus": {"fail_until": time.time() + 60, "fail_why": "HTTP 401"}})
        real_get = uc.get_usage
        calls = []

        def once_401(path):
            calls.append(1)
            if len(calls) == 1:
                uc.last_status = 401
                uc._cache_save({"C@Opus": {"fail_until": time.time() + 60, "fail_why": "HTTP 401"}})
                return None
            return real_get.__wrapped__(path) if hasattr(real_get, "__wrapped__") else free

        uc.get_usage = once_401
        sa._write_owner_only(sp.store_path("Opus", "C"), cred("rt-C", -1))
        got = uc.get_usage_for(sp.store_path("Opus", "C"), is_current=False)
        uc.get_usage = fake_get
        check("갱신 뒤 실패 기억을 지워 바로 다시 읽는다",
              got is not None and "fail_until" not in (uc._cache_load().get("C@Opus") or {}), got)
        # (4) 열쇠가 죽은 B 는 건너뛰고 C 로 간다(끼우면 창이 Login expired)
        seat("A")   # 떠나는 B 의 꽂힌 자격이 B 저장본으로 되저장되므로 저장본은 앉힌 뒤에 낡게 만든다
        sa._write_owner_only(sp.store_path("Opus", "B"), cred("rt-B", -12))
        sa._write_owner_only(sp.store_path("Opus", "C"), cred("rt-C", 6))
        dead.add("B")
        refreshed.clear()
        table.update({"A": full, "B": free, "C": free})
        rc, cur = sp.ensure("Opus", threshold=claude_pool.PREEMPT_PCT)
        live = sp._load(sp.live_path("Opus"))
        check("열쇠가 죽은 B 는 건너뛰고 C 로 간다", cur == "C" and "B" in refreshed, (rc, cur, refreshed))
        check("창에 꽂힌 자격은 C 것", (live or {}).get("claudeAiOauth", {}).get("refreshToken") == "rt-C")
    finally:
        (sp.POOL_ROOT, sp.STORE_ROOT, sp.CLAUDE_HOME, sp.LOG_FILE, uc.get_usage, uc.last_status,
         uc._CACHE_FILE, cr.refresh_file, st._pid_alive, sp._has_window) = real
        os.environ["AIROTATOR_NO_SLOTS"] = "1"


def case_last_resort_shared(tmp):
    """[59] 공용 계정: 지금 계정이 그 칸 100% 인데 다른 계정이 97~99% 면 그중 가장 여유 있는 곳으로 (2026-09-23)."""
    print()
    print("[59] 공용 계정 마지막 수단: 97~99% 계정이라도 쓴다")
    import usage_check as uc

    now = time.time()

    def fab(pct):
        return {"weekly_pct": 50.0, "session_pct": 5.0, "weekly_reset": now + 48 * 3600,
                "session_reset": now + 3600, "scoped": {"Fable": {"pct": pct, "reset": now + 48 * 3600}}}

    real_alive = st._pid_alive
    st._pid_alive = lambda pid: str(pid) == "4343"

    def run(table, current, scoped=None):
        if st.STATE_FILE.exists():
            st.STATE_FILE.unlink()
        setup(tmp, ["A", "B", "C"], current)
        s = st.load_state()
        s["reclaim_at"] = None
        s["scoped_blocked"] = scoped or {}
        st.save_state(s)
        real_get, real_status = uc.get_usage, uc.last_status
        install(Fake({x: (0, OK_OUT, "") for x in ("A", "B", "C")}))
        claude_pool.usage_check = uc
        uc.last_status = None
        uc.get_usage = lambda path: table.get(Path(path).stem)
        try:
            claude_pool.claim_session(4343, model="claude-fable-5-1")
            rc1 = claude_pool.tend_windows()
            c1 = st.load_state().get("current")
            rc2 = claude_pool.tend_windows()
            return rc1, c1, rc2, st.load_state().get("current")
        finally:
            uc.get_usage, uc.last_status = real_get, real_status
            claude_pool.usage_check = None

    try:
        blk = {"B": {"Fable": now + 48 * 3600}, "C": {"Fable": now + 48 * 3600}}
        rc1, c1, rc2, c2 = run({"A": fab(100.0), "B": fab(98.0), "C": fab(97.0)}, "A", blk)
        check("A 100%, B 98%·C 97%(막힌 기록) → C 로 간다(3)", rc1 == claude_pool.ENSURE_SWITCHED and c1 == "C",
              (rc1, c1))
        check("다음 회차엔 C 그대로(왕복 없음)", rc2 == 0 and c2 == "C", (rc2, c2))
        rc1, c1, _, _ = run({"A": fab(99.0), "B": fab(98.0), "C": fab(97.0)}, "A", blk)
        check("지금 계정이 99% 면 마지막 수단을 안 쓴다(그대로 A)", c1 == "A", (rc1, c1))
        rc1, c1, _, _ = run({"A": fab(100.0), "B": fab(100.0), "C": fab(100.0)}, "A", blk)
        check("전부 100% 면 그대로 A", c1 == "A", (rc1, c1))
        # 지금 계정 조회가 429 여도 마지막 기록이 100% 면 마지막 수단
        real_note = st.note_usage
        def run_seen():
            if st.STATE_FILE.exists():
                st.STATE_FILE.unlink()
            setup(tmp, ["A", "B", "C"], "A")
            s = st.load_state()
            s["reclaim_at"] = None
            s["scoped_blocked"] = dict(blk, A={"Fable": now + 48 * 3600})
            real_note(s, "A", fab(100.0))
            st.save_state(s)
        run_seen()
        table = {"A": None, "B": fab(98.0), "C": fab(97.0)}
        real_get, real_status = uc.get_usage, uc.last_status
        install(Fake({x: (0, OK_OUT, "") for x in ("A", "B", "C")}))
        claude_pool.usage_check = uc
        uc.get_usage = lambda path: table.get(Path(path).stem)
        try:
            claude_pool.claim_session(4343, model="claude-fable-5-1")
            s = st.load_state(); s["tend_revive_at"] = time.time(); st.save_state(s)
            claude_pool.tend_windows()
        finally:
            uc.get_usage, uc.last_status = real_get, real_status
            claude_pool.usage_check = None
        check("공용: 지금 계정을 못 읽어도 기록이 100% 면 C 로", st.load_state().get("current") == "C",
              st.load_state().get("current"))
    finally:
        st._pid_alive = real_alive
        claude_pool.release_session(4343)

if __name__ == "__main__":
    raise SystemExit(main())
