"""
== 운영 맥락 ==
실행 시점: 손으로 (python test_longtoken.py). 네트워크 안 씀.
목적: 1년 토큰 처리(2026-11-17) 회귀 시험. 코덱스 검수 지적 1(30일 열쇠도 sk-ant-oat01- 접두사) 재발 방지.
마지막 점검: 2026-11-17
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import cred_refresh as cr

FAIL = 0


def check(name, cond):
    global FAIL
    print(("  통과: " if cond else "  실패: ") + name)
    if not cond:
        FAIL += 1


regular = {"claudeAiOauth": {"accessToken": "sk-ant-oat01-xxxx", "refreshToken": "sk-ant-ort01-yyyy", "expiresAt": 1}}
longt = {"claudeAiOauth": {"accessToken": "sk-ant-oat01-xxxx", "refreshToken": "lt-abc", "expiresAt": 9e15}}

check("30일 열쇠(같은 접두사)는 1년 토큰이 아니다", not cr.is_long_lived(regular))
check("1년 토큰 꼴을 알아본다", cr.is_long_lived(longt))
check("안쪽 dict 로 줘도 알아본다", cr.is_long_lived(longt["claudeAiOauth"]))

d = tempfile.mkdtemp()
p = Path(d) / "X.json"
p.write_text(json.dumps(longt), encoding="utf-8")
called = []
orig = cr._post
cr._post = lambda t: called.append(t) or None
try:
    ok = cr.refresh_file(p)
finally:
    cr._post = orig
check("1년 토큰은 갱신 요청을 보내지 않고 성공", ok and not called)
check("1년 토큰 파일은 그대로", json.loads(p.read_text(encoding="utf-8")) == longt)

p.write_text(json.dumps(regular), encoding="utf-8")
called.clear()
cr._post = lambda t: called.append(t) or None
try:
    cr.refresh_file(p)
finally:
    cr._post = orig
check("30일 열쇠는 예전처럼 갱신을 시도한다", called == ["sk-ant-ort01-yyyy"])

import install_longtoken as il
q = Path(d) / "Y.json"
q.write_text("{}", encoding="utf-8")
il.write(q, longt)
if os.name == "posix":
    check("저장 뒤 권한 0600", (q.stat().st_mode & 0o777) == 0o600)
check("저장 내용", json.loads(q.read_text(encoding="utf-8")) == longt)

print("전부 통과" if not FAIL else f"실패 {FAIL}건")
sys.exit(1 if FAIL else 0)
