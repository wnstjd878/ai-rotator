"""
== 운영 맥락 ==
용도: 이 머신에 저장된 계정 자격이 (1) 살아 있는지 (2) 라벨과 실제 계정이 맞는지
      (3) 재발급 열쇠 만료가 임박했는지 한 번에 점검한다. 아침 점검 봇이 3대에 이걸 돌린다.
실행: python creds_audit.py            사람이 읽는 줄로 출력
      python creds_audit.py --json     기계가 읽는 형태(아침 점검 봇이 씀)
출력: 라벨별 상태. **자격 값은 절대 출력하지 않는다**(이메일은 계정 식별용이라 표시).
판정: dead    = 저장본이 없거나, 조회도 안 되는데 재발급 열쇠까지 만료됨
      unknown = 조회는 안 되지만 재발급 열쇠는 살아 있음 (거의 항상 이것. 경보 아님)
      wrong   = 그 라벨에 다른 계정 자격이 들어 있음 (2026-07-28 winserver 에서 실제로 발생)
      soon    = 재발급 열쇠 만료가 EXPIRY_WARN_DAYS 이내 (조회 여부와 무관하게 잡는다)
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
# 재발급 열쇠 수명은 30일이다. 이보다 훨씬 큰 만료일이 나오면 값이 깨진 것으로 본다.
# 그냥 두면 "만료 95077일" 처럼 찍히면서 만료 임박 경보가 영영 안 뜬다
# (2026-07-29 Mac A 에서 실제로 한 번 관측, 그 뒤 파일이 갱신되며 정상값으로 돌아옴).
MAX_SANE_DAYS = 60

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def audit():
    import switch_account as sa

    try:
        accounts = json.loads((BASE / "accounts.json").read_text(encoding="utf-8"))
        labels = [x["label"] for x in accounts if x.get("label")]
        # 구독을 해지해 일부러 뺀 계정(accounts.json 의 "retired": true).
        # 점검 대상에서 통째로 제외한다 — 안 그러면 저장본 없음이 dead 로 집계돼
        # 아침 점검이 매일 "죽음 C, relogin 필요" 를 보낸다(2026-08-18 C 해지).
        # 재구독하면 retired 를 지우고 save_account.py 로 저장하면 점검이 되살아난다.
        retired = [x["label"] for x in accounts if x.get("label") and x.get("retired")]
    except Exception:
        labels = sorted(f.stem for f in sa.CREDS_DIR.glob("*.json"))
        retired = []

    out = {"labels": {}, "dead": [], "wrong": [], "soon": [], "unknown": [],
           "odd": [], "retired": retired, "current": None}
    try:
        out["current"] = sa.live_label()
    except Exception:
        pass

    for label in labels:
        if label in retired:
            continue  # 해지 계정은 labels 결과에도 안 넣는다(아침 봇의 "정상 N개" 계산 오염 방지)
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
        if info["days"] is not None and info["days"] > MAX_SANE_DAYS:
            info["odd_days"] = info["days"]  # 원래 값은 남겨서 무슨 값이었는지 보이게
            info["days"] = None
            out["odd"].append(label)

        mail = sa.cred_email(f)
        if mail is None:
            # 조회 실패는 죽음이 아니다. 출입증(access token)이 8시간이라 지금 안 쓰는 계정의
            # 저장본은 대개 만료돼 있다. 재발급 열쇠가 남아 있으면 갈아끼우는 순간 claude 가
            # 새 출입증을 받아 정상 동작한다. 여기서 죽었다고 보면 멀쩡한 계정 넷이 아침 점검
            # 알림에 매일 "자격 죽음" 으로 나간다(2026-07-29 실측: 4계정 전부 오탐).
            # 진짜 죽은 자격은 순환기가 부르는 순간 dead 로 잡아 갈아탄다(그쪽이 확증 자리다).
            # 만료값이 깨진 계정(odd)은 값이 이상한 것이지 자격이 죽은 게 아니다.
            # dead 로 넣으면 "자격 죽음" 으로 잘못 알린다. 경보는 odd 쪽에서 이미 켜진다.
            if (info["days"] is not None and info["days"] > 0) or label in out["odd"]:
                info["state"] = "unknown"
                out["unknown"].append(label)
            else:
                info["state"] = "dead"
                out["dead"].append(label)
        else:
            actual = sa.label_of_email(mail)
            info["actual"] = actual
            if actual and actual != label:
                info["state"] = "wrong"
                out["wrong"].append(label)

        # 만료 임박은 조회가 됐든 안 됐든 알려야 한다. 조회 안 되는 계정의 만료를 놓치면
        # 재로그인 시점을 넘겨 그 계정이 진짜로 죽는다.
        if (info["state"] in ("ok", "unknown")
                and info["days"] is not None and info["days"] <= EXPIRY_WARN_DAYS):
            if info["state"] == "unknown":
                out["unknown"].remove(label)
            info["state"] = "soon"
            out["soon"].append(label)
        out["labels"][label] = info

    out["bad"] = bool(out["dead"] or out["wrong"] or out["soon"] or out["odd"])
    return out


def main():
    r = audit()
    if "--json" in sys.argv[1:]:
        print(json.dumps(r, ensure_ascii=False))
        return 0
    print(f"현재 계정: {r['current'] or '(모름)'}")
    for label, info in r["labels"].items():
        days = f"  만료 {info['days']}일" if info["days"] is not None else ""
        if info.get("odd_days") is not None:
            days = f"  만료값 이상({info['odd_days']}일), 자격 파일 확인 필요"
        if info["state"] == "ok":
            print(f"  {label}: 정상{days}")
        elif info["state"] == "soon":
            print(f"  {label}: 만료 임박{days}, 재로그인 필요")
        elif info["state"] == "wrong":
            print(f"  {label}: 라벨 어긋남, 실제는 '{info['actual']}'{days}")
        elif info["state"] == "missing":
            print(f"  {label}: 저장본 없음")
        elif info["state"] == "unknown":
            print(f"  {label}: 정상(지금은 사용량만 조회 불가, 출입증 8시간 만료){days}")
        else:
            print(f"  {label}: 자격 죽음(조회 실패, 재발급 열쇠도 만료){days}")
    for label in r.get("retired") or []:
        print(f"  {label}: 제외(해지 계정, 점검 안 함)")
    print("판정:", "확인 필요" if r["bad"] else "전부 정상")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
