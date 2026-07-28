"""
== 운영 맥락 ==
용도: 이 머신에 저장된 계정 자격이 (1) 살아 있는지 (2) 라벨과 실제 계정이 맞는지
      (3) 재발급 열쇠 만료가 임박했는지 한 번에 점검한다. 아침 점검 봇이 3대에 이걸 돌린다.
실행: python creds_audit.py            사람이 읽는 줄로 출력
      python creds_audit.py --json     기계가 읽는 형태(아침 점검 봇이 씀)
출력: 라벨별 상태. **자격 값은 절대 출력하지 않는다**(이메일은 계정 식별용이라 표시).
판정: dead  = 서버 조회 실패(만료·폐기 가능) 또는 저장본 없음
      wrong = 그 라벨에 다른 계정 자격이 들어 있음 (2026-07-28 winserver 에서 실제로 발생)
      soon  = 재발급 열쇠 만료가 EXPIRY_WARN_DAYS 이내
왜 필요한가: 자격이 죽거나 라벨이 섞여도 겉으로는 아무 일 없어 보인다. 순환기가 그 계정을
      고르는 순간에야 "Login expired" 로 드러나고, 그때는 이미 봇이 멈춘 뒤다.
고침: dead/soon → python relogin.py <라벨> (원격은 --remote <머신> <라벨>)
      wrong → 그 라벨로 재로그인하면 제자리로 덮어써진다
마지막 점검: 2026-07-29
"""

import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

EXPIRY_WARN_DAYS = 7

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def audit():
    import switch_account as sa

    try:
        accounts = json.loads((BASE / "accounts.json").read_text(encoding="utf-8"))
        labels = [x["label"] for x in accounts if x.get("label")]
    except Exception:
        labels = sorted(f.stem for f in sa.CREDS_DIR.glob("*.json"))

    out = {"labels": {}, "dead": [], "wrong": [], "soon": [], "current": None}
    try:
        out["current"] = sa.live_label()
    except Exception:
        pass

    for label in labels:
        f = sa.CREDS_DIR / f"{label}.json"
        info = {"state": "ok", "days": None, "actual": None}
        if not f.exists():
            info["state"] = "missing"
            out["dead"].append(label)
            out["labels"][label] = info
            continue

        try:
            o = json.loads(f.read_text(encoding="utf-8"))["claudeAiOauth"]
            info["days"] = round(
                (int(o.get("refreshTokenExpiresAt") or 0) / 1000 - time.time()) / 86400, 1)
        except Exception:
            info["days"] = None

        mail = sa.cred_email(f)
        if mail is None:
            info["state"] = "dead"
            out["dead"].append(label)
        else:
            actual = sa.label_of_email(mail)
            info["actual"] = actual
            if actual and actual != label:
                info["state"] = "wrong"
                out["wrong"].append(label)

        if info["state"] == "ok" and info["days"] is not None and info["days"] <= EXPIRY_WARN_DAYS:
            info["state"] = "soon"
            out["soon"].append(label)
        out["labels"][label] = info

    out["bad"] = bool(out["dead"] or out["wrong"] or out["soon"])
    return out


def main():
    r = audit()
    if "--json" in sys.argv[1:]:
        print(json.dumps(r, ensure_ascii=False))
        return 0
    print(f"현재 계정: {r['current'] or '(모름)'}")
    for label, info in r["labels"].items():
        days = f"  만료 {info['days']}일" if info["days"] is not None else ""
        if info["state"] == "ok":
            print(f"  {label}: 정상{days}")
        elif info["state"] == "soon":
            print(f"  {label}: 만료 임박{days} — 재로그인 필요")
        elif info["state"] == "wrong":
            print(f"  {label}: 라벨 어긋남 — 실제는 '{info['actual']}'{days}")
        elif info["state"] == "missing":
            print(f"  {label}: 저장본 없음")
        else:
            print(f"  {label}: 자격 죽음(조회 실패){days}")
    print("판정:", "확인 필요" if r["bad"] else "전부 정상")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
