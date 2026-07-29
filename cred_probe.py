"""
== 운영 맥락 ==
용도: 클로드 자격이 어느 자리에 무슨 값으로 있는지 한눈에 본다. 열쇠꾸러미 / 자격 파일 /
      계정 저장본 세 자리를 지문으로 대조한다. **"열쇠꾸러미에 옛 자격" 알림을 받았을 때
      지우기 전에 이걸 먼저 돌린다** — 만료 안 된 최신본을 지우면 계정이 통째로 죽는다.
실행: python3 ~/projects/ai-rotator/cred_probe.py
      열쇠꾸러미 값은 화면 세션에서만 읽힌다(원격 접속 창에서는 "못 읽음"이 정상).
출력: 값은 절대 안 찍는다. 재발급 열쇠의 지문 앞 12자리와 남은 시간만.
외부 의존: security 명령
마지막 점검: 2026-07-30
"""

import hashlib
import json
import subprocess
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CREDS = BASE / "creds"
LIVE = Path.home() / ".claude" / ".credentials.json"
SERVICE = "Claude Code-credentials"


def fp(o):
    t = (o.get("claudeAiOauth") or {}).get("refreshToken") or ""
    return hashlib.sha256(t.encode()).hexdigest()[:12] if t else None


def exp(o):
    a = o.get("claudeAiOauth") or {}
    out = []
    for k, name in (("expiresAt", "출입증"), ("refreshTokenExpiresAt", "재발급 열쇠")):
        v = a.get(k)
        if not v:
            continue
        left = (v / 1000) - time.time()
        out.append("%s %s" % (name, ("%.1f시간 남음" % (left / 3600)) if left > 0 else "만료됨"))
    return ", ".join(out) or "만료 정보 없음"


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def read_keychain():
    """열쇠꾸러미 값을 이 프로세스 안으로만 읽는다. 디스크에 남기지 않는다."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", "kwon", "-w"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads((r.stdout or "").strip())
    except Exception:
        return None


def main():
    print("[열쇠꾸러미]")
    kc = read_keychain()
    if kc:
        print("  읽음   지문 %s   %s" % (fp(kc), exp(kc)))
    else:
        print("  못 읽음 (원격 접속 창이거나 화면 잠금. 항목이 아예 없을 수도 있음)")

    print("[자격 파일]")
    o = read_json(LIVE)
    if o:
        print("  있음   지문 %s   %s" % (fp(o), exp(o)))
    else:
        print("  없음")

    print("[계정 저장본]")
    for f in sorted(CREDS.glob("*.json")):
        o = read_json(f)
        if not o:
            print("  %s: 못 읽음" % f.stem)
            continue
        same = "   열쇠꾸러미와 같은 값" if (kc and fp(o) == fp(kc)) else ""
        print("  %s: 지문 %s   %s%s" % (f.stem, fp(o), exp(o), same))

    print("[보관본]")
    found = False
    for sub in ("rescued", "prev", "dead"):
        d = CREDS / sub
        if not d.exists():
            continue
        for f in sorted(d.glob("*.json")):
            o = read_json(f)
            if not o:
                continue
            found = True
            same = "   열쇠꾸러미와 같은 값" if (kc and fp(o) == fp(kc)) else ""
            print("  %s/%s: 지문 %s   %s%s" % (sub, f.stem, fp(o), exp(o), same))
    if not found:
        print("  없음")

    if kc:
        a = kc.get("claudeAiOauth") or {}
        v = a.get("refreshTokenExpiresAt") or 0
        alive = v and (v / 1000) > time.time()
        print()
        if alive:
            print("판단: 열쇠꾸러미 값이 아직 살아 있습니다. 지우면 안 됩니다.")
        else:
            print("판단: 열쇠꾸러미 값이 만료됐습니다. 지워도 되는 옛 자격입니다.")


if __name__ == "__main__":
    raise SystemExit(main())
