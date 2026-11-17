"""
== 운영 맥락 ==
실행 시점: 사람이 손으로 1회 (python install_longtoken.py [--dry-run])
입력: creds/longtoken/<라벨>.token (setup_token.py 로 받은 1년 토큰)
출력: 이 머신의 자격 파일을 1년 토큰 꼴로 바꿔 씀
      - 저장본 creds/<라벨>.json, creds/slots/<칸>/<라벨>.json (있는 것만)
      - 쓰는 중인 파일 ~/.claude/.credentials.json, ~/.claude-pool/<칸>/.credentials.json (주인 라벨을 알 때만)
      바꾸기 전 전부 creds/_bak_30d_<시각>/ 에 그대로 복사(되돌리기: 그 폴더에서 같은 자리로 복사)
목적: 30일마다 브라우저 재로그인(구글 그림 고르기)을 없앤다 (2026-11-17 사장님 지시)
꼴: claudeAiOauth.accessToken=1년 토큰, refreshToken="lt-"+지문(계정마다 달라 지문 대조가 계정을 가름),
    expiresAt=refreshTokenExpiresAt=발급+364일, scopes=["user:inference"]. 다른 칸(subscriptionType 등)은 유지.
    cred_refresh.is_long_lived 가 이 꼴을 알아보고 갱신을 건너뛴다.
의도적 미구현: 맥·winserver 는 각 머신에서 따로(토큰 파일을 옮긴 뒤 같은 스크립트).
보안: 토큰 값은 파일로만. 화면엔 라벨·경로·지문 12자리만.
마지막 점검: 2026-11-17
"""
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CREDS = BASE / "creds"
LT = CREDS / "longtoken"
HOME = Path.home()
DRY = "--dry-run" in sys.argv


def fp(s):
    return hashlib.sha256(str(s).encode()).hexdigest()[:12]


def load(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return None


def oauth(obj):
    return (obj or {}).get("claudeAiOauth") or {}


def tokens():
    out = {}
    for f in LT.glob("*.token"):
        meta = load(LT / f"{f.stem}.json") or {}
        out[f.stem] = (f.read_text(encoding="utf-8").strip(), int(meta.get("issuedAt") or time.time()))
    return out


def stored_files():
    """(라벨, 경로) 저장본 목록."""
    out = []
    for f in CREDS.glob("*.json"):
        if f.stem.isalnum() and f.stem.upper() == f.stem and len(f.stem) <= 3:
            out.append((f.stem, f))
    sl = CREDS / "slots"
    if sl.is_dir():
        for d in sl.iterdir():
            if d.is_dir():
                for f in d.glob("*.json"):
                    if f.stem.upper() == f.stem and len(f.stem) <= 3:
                        out.append((f.stem, f))
    return out


def live_files():
    out = [("main", HOME / ".claude" / ".credentials.json")]
    pool = HOME / ".claude-pool"
    if pool.is_dir():
        for d in pool.iterdir():
            f = d / ".credentials.json"
            if d.is_dir() and f.is_file():
                out.append((d.name, f))
    return out


def owner_of_live(f, stored, state):
    """쓰는 중인 파일의 주인 라벨: 같은 재발급 열쇠·접근 열쇠를 가진 저장본 → 순환기 기록."""
    o = oauth(load(f))
    hit = set()
    for lab, p in stored:
        so = oauth(load(p))
        if (o.get("refreshToken") and so.get("refreshToken") == o.get("refreshToken")) or (o.get("accessToken") and so.get("accessToken") == o.get("accessToken")):
            hit.add(lab)
    if len(hit) == 1:
        return hit.pop(), "토큰 대조"
    if not hit:
        try:  # 쓰는 중인 파일은 claude 가 스스로 갱신해 저장본과 달라진다 → 서버에 메일을 묻는다(30일 열쇠일 때만 됨)
            import switch_account as sa
            mail = (sa.cred_email(f) or "").lower()
            for a in load(BASE / "accounts.json") or []:
                if isinstance(a, dict) and str(a.get("email") or "").lower() == mail and mail:
                    return a.get("label"), "서버 메일 조회"
        except Exception:
            pass
        cur = (state.get("slots") or {}).get(f.parent.name, {}).get("current") if f.parent.parent.name == ".claude-pool" else state.get("current")
        if cur:
            return str(cur), "순환기 기록"
    return None, ("저장본 여러 개와 겹침 " + ",".join(sorted(hit))) if hit else "대조 안 됨"


def build(old, tok, issued):
    obj = dict(old) if isinstance(old, dict) else {}
    o = dict(oauth(old))
    exp = (issued + 364 * 86400) * 1000
    o.update({"accessToken": tok, "refreshToken": "lt-" + hashlib.sha256(tok.encode()).hexdigest()[:40],
              "expiresAt": exp, "refreshTokenExpiresAt": exp, "scopes": ["user:inference"]})
    o.setdefault("subscriptionType", "max")
    obj["claudeAiOauth"] = o
    return obj


def write(p, obj):
    tmp = Path(str(p) + ".lt-tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, p)


def main():
    toks = tokens()
    if not toks:
        print("creds/longtoken 에 토큰이 없습니다")
        return 1
    stored = stored_files()
    state = load(BASE / "state.json") or {}
    lives = live_files()
    plan = []
    for lab, p in stored:
        if lab in toks:
            plan.append((p, lab, "저장본"))
    for name, f in lives:
        lab, why = owner_of_live(f, stored, state)
        if lab in toks:
            plan.append((f, lab, f"쓰는 중({name}, {why})"))
        else:
            print(f"건너뜀: {f} 주인 모름({why})")
    print(f"바꿀 파일 {len(plan)}개:")
    for p, lab, kind in plan:
        print(f"  {lab}  {kind}  {p}")
    if DRY:
        return 0
    bak = CREDS / f"_bak_30d_{time.strftime('%Y%m%d-%H%M%S')}"
    for p, lab, kind in plan:
        rel = p.relative_to(HOME)
        dst = bak / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
    print(f"백업: {bak}")
    for p, lab, kind in plan:
        tok, issued = toks[lab]
        write(p, build(load(p), tok, issued))
    # 확인
    bad = 0
    for p, lab, kind in plan:
        o = oauth(load(p))
        ok = o.get("accessToken") == toks[lab][0]
        bad += 0 if ok else 1
        print(f"  {'됨' if ok else '안 됨'}  {lab}  {p.name}  지문 {fp(o.get('accessToken'))}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
