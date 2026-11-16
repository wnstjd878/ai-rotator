"""
== 운영 맥락 ==
실행 시점: 사람이 손으로 (python setup_token.py <라벨> / --test <라벨>)
입력: accounts.json 라벨, 그 계정 전용 크롬(relogin.py 의 9250대 포트)
출력: creds/longtoken/<라벨>.token (claude setup-token 이 주는 장기 열쇠, git 제외 폴더)
외부 의존: claude CLI setup-token, platform.claude.com 승인 화면
목적: 30일마다 "브라우저로 다시 로그인"(구글 그림 고르기 포함)을 1년에 1회로 줄이는 시험 (2026-11-16 사장님 지시)
의도적 미구현: 순환기(claude_pool) 연결은 시험 결과를 본 뒤에 한다. 무인·예약 실행 금지(feedback_no_auto_relogin).
보안: 열쇠 값은 파일에만 쓴다. 화면에는 길이·앞부분 종류·지문 12자리만 찍는다.
마지막 점검: 2026-11-16
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import relogin as R

TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")
OUT_DIR = R.BASE / "creds" / "longtoken"


def _clean(text):
    return TOKEN_RE.sub("[열쇠]", text)


def _env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env["BROWSER"] = str(R.BASE / "browser_noop.cmd")
    return env


def issue(label):
    if label not in R.labels():
        print("없는 라벨:", label)
        return 1
    R.launch_chrome(label)
    p = subprocess.Popen(R.claude_cmd("setup-token"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                         bufsize=1, cwd=str(R.BASE), env=_env())
    buf, url, deadline = "", None, time.time() + 60
    while time.time() < deadline and not url:
        line = p.stdout.readline()
        if not line:
            if p.poll() is not None:
                break
            continue
        buf += line
        m = re.search(r"https://\S*oauth/authorize\S+", line)
        if m:
            url = m.group(0).rstrip("\"'）)")
    if not url:
        p.kill()
        print("승인 주소를 못 받음. 출력:", _clean(buf)[:500])
        return 1
    code = R.open_tab_and_wait(label, url, p)
    if code and code != "DONE":
        print(f"승인 코드 받음(길이 {len(code)}). 열쇠 받는 중...")
        p.stdin.write(code + "\n")
        p.stdin.flush()
    elif not code:
        p.kill()
        print("승인 없음")
        return 1
    try:
        rest, _ = p.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        p.kill()
        rest, _ = p.communicate()
    allout = buf + (rest or "")
    toks = TOKEN_RE.findall(allout)
    print("claude 출력(열쇠 가림):")
    print("\n".join(l for l in _clean(rest or "").splitlines() if l.strip())[-1500:])
    if not toks:
        print("열쇠를 출력에서 못 찾음")
        return 1
    tok = toks[-1]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    f = OUT_DIR / f"{label}.token"
    f.write_text(tok, encoding="utf-8")
    meta = {"label": label, "issuedAt": int(time.time()), "sha12": hashlib.sha256(tok.encode()).hexdigest()[:12]}
    (OUT_DIR / f"{label}.json").write_text(json.dumps(meta), encoding="utf-8")
    print(f"저장: {f} (길이 {len(tok)}, 종류 {tok[:13]}…가림, 지문 {meta['sha12']})")
    return 0


def test(label):
    tok = (OUT_DIR / f"{label}.token").read_text(encoding="utf-8").strip()
    # 1) claude 호출
    env = _env()
    env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    env["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="lt_")
    r = subprocess.run(R.claude_cmd("-p", "OK 한 단어로만 답해", "--model", "haiku"), env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    print("claude 호출 종료코드", r.returncode, "답:", _clean((r.stdout or "") + (r.stderr or "")).strip()[:300])
    # 계정 확인
    r2 = subprocess.run(R.claude_cmd("auth", "status"), env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=60)
    print("auth status:", _clean((r2.stdout or "") + (r2.stderr or "")).strip()[:600])
    # 2) 한도 조회
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + tok, "User-Agent": "claude-cli/2.1.0 (external, cli)",
        "Accept": "application/json", "anthropic-beta": "oauth-2025-04-20"})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            body = res.read().decode("utf-8", "replace")
            print("한도 조회", res.status, _clean(body)[:400])
    except urllib.error.HTTPError as e:
        print("한도 조회", e.code, _clean(e.read().decode("utf-8", "replace"))[:300])
    except Exception as e:
        print("한도 조회 실패", type(e).__name__)
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        sys.exit(test(sys.argv[2]))
    sys.exit(issue(sys.argv[1] if len(sys.argv) > 1 else "E"))
