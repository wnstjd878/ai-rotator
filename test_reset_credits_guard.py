"""초기화권 쓰기 막기 시험(2026-11-21). 실제 요청 없이 claim_blocker 판정만 본다. 실행: python test_reset_credits_guard.py"""
import time
from reset_credits import claim_blocker

NOW = time.time()
def iso(h):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW + h * 3600, timezone.utc).isoformat()
def pre(pct, hours, fable=False, usable=True, left=1, fpct=None):
    g = {"id": "g1", "label": "Fable reset" if fable else "One full limit reset", "left": left, "usableNow": usable}
    lim = {"weekly": {"pct": pct, "resetsAt": iso(hours)}}
    if fpct is not None:
        lim["fable"] = {"pct": fpct, "resetsAt": iso(hours)}
    return {"grants": [g], "limits": lim, "nextGrantId": "g1"}

cases = [
    ("A 처럼 9시간 뒤 풀림", pre(100, 9), "저절로 풀림"),
    ("B 처럼 51%만 씀", pre(51, 140), "51% 사용"),
    ("꽉 찼고 3일 남음", pre(100, 72), ""),
    ("사용량 모름", {"grants": [{"id": "g1", "label": "x", "left": 1, "usableNow": True}], "limits": {}}, "사용량 확인 안 됨"),
    ("다 쓴 초기화권", pre(100, 72, left=0), "이미 씀"),
    ("차례 아님", pre(100, 72, usable=False), "못 쓴다고"),
    ("Fable 권인데 Fable 칸 없음", pre(100, 72, fable=True), "Fable 한도 사용량 확인 안 됨"),
    ("Fable 권, Fable 40%", pre(100, 72, fable=True, fpct=40), "Fable 한도 40% 사용"),
    ("Fable 권, Fable 100%", pre(10, 72, fable=True, fpct=100), ""),
    ("없는 번호", pre(100, 72), "없는 초기화권"),
]
fail = 0
for name, p, want in cases:
    got = claim_blocker(p, "zz" if name == "없는 번호" else "g1", NOW)
    ok = (got == "") if want == "" else (want in got)
    fail += not ok
    print(("통과 " if ok else "실패 ") + name + " → " + (got or "쓸 수 있음"))
print(f"실패 {fail}개")
raise SystemExit(1 if fail else 0)
