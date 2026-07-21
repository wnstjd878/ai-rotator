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
마지막 점검: 2026-07-22
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
        self.notices = []

    def run(self, args, **kwargs):
        label = st.load_state().get("current")
        self.calls.append(label)
        rc, out, err = self.per_label.get(label, (1, "", "unknown account"))
        return subprocess.CompletedProcess(args, rc, out, err)

    def switch(self, label):
        self.switches.append(label)
        return self.switch_ok

    def notify(self, message):
        self.notices.append(message)


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
    check("알림을 보냄", len(fake.notices) == 1, f"notices={len(fake.notices)}")
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


def main():
    real_run = subprocess.run
    real_switch = claude_pool._switch
    real_notify = claude_pool.notify
    try:
        for case in (case_success_no_switch, case_false_positive, case_rotate_on_limit,
                     case_all_exhausted, case_already_switched, case_reset_parsing,
                     case_auth, case_unknown_error, case_manual_next):
            with tempfile.TemporaryDirectory() as tmp:
                case(tmp)
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
