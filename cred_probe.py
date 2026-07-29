"""자격이 어느 자리에 무슨 값으로 있는지만 본다. 값은 출력하지 않는다(지문 12자리만)."""
import hashlib
import json
import subprocess
import time
from pathlib import Path

BASE = Path.home() / "projects" / "ai-rotator"
CREDS = BASE / "creds"
LIVE = Path.home() / ".claude" / ".credentials.json"
TMP = Path("/tmp/.kc_probe.json")


def fp(o):
    t = (o.get("claudeAiOauth") or {}).get("refreshToken") or ""
    return hashlib.sha256(t.encode()).hexdigest()[:12] if t else None


def exp(o):
    a = o.get("claudeAiOauth") or {}
    out = []
    for k in ("expiresAt", "refreshTokenExpiresAt"):
        v = a.get(k)
        if not v:
            continue
        left = (v / 1000) - time.time()
        txt = ("남음 %.1f시간" % (left / 3600)) if left > 0 else "만료됨"
        out.append(k + "=" + txt)
    return ", ".join(out) or "만료정보없음"


print("=== 열쇠꾸러미 ===")
kc = None
try:
    with open(TMP, "w") as fh:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials",
             "-a", "kwon", "-w"],
            stdout=fh, stderr=subprocess.DEVNULL, timeout=20,
        )
    if r.returncode == 0:
        kc = json.loads(TMP.read_text().strip())
        print("  읽기 성공  지문:", fp(kc), " ", exp(kc))
    else:
        print("  읽기 실패(잠김 또는 없음)")
except Exception as e:
    print("  읽기 오류:", type(e).__name__)
finally:
    TMP.unlink(missing_ok=True)

print("=== 파일 자리 ===")
if LIVE.exists():
    o = json.loads(LIVE.read_text())
    print("  있음  지문:", fp(o), " ", exp(o))
else:
    print("  없음")

print("=== 계정 저장본 ===")
for f in sorted(CREDS.glob("*.json")):
    try:
        o = json.loads(f.read_text())
    except Exception:
        print("  %s: 못 읽음" % f.stem)
        continue
    mark = "  <-- 열쇠꾸러미와 같은 값" if (kc and fp(o) == fp(kc)) else ""
    print("  %s: %s  %s%s" % (f.stem, fp(o), exp(o), mark))

print("=== 보관본(rescued/prev) ===")
for sub in ("rescued", "prev", "dead"):
    d = CREDS / sub
    if not d.exists():
        continue
    for f in sorted(d.glob("*.json")):
        try:
            o = json.loads(f.read_text())
        except Exception:
            continue
        mark = "  <-- 열쇠꾸러미와 같은 값" if (kc and fp(o) == fp(kc)) else ""
        print("  %s/%s: %s  %s%s" % (sub, f.stem, fp(o), exp(o), mark))
