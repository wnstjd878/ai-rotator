"""
== 운영 맥락 ==
용도: Claude 계정마다 claude.ai 웹에 보이는 "한도 초기화권"(남은 개수·쓸 수 있는 기한)을 읽어
      reset_credits.json 에 적는다. AI 오피스 AI 한도 탭이 이 파일을 읽어 카드에 띄운다(2026-11-19 사장님
      "AI 한도에 초기화권도 같이 띄워주셈").
왜 브라우저인가: 초기화권은 /api/oauth/usage?at_wall=1 의 cedar_ember·juniper_tide 칸에 오는데, 이 칸은
      프로필 권한(user:profile)이 있는 로그인만 받는다. 2026-11-17 부터 모든 계정이 1년 토큰(user:inference 만)이라
      그 주소로는 못 읽는다. 그래서 계정별 전용 크롬(chrome/<라벨>, relogin.py 가 쓰는 것)에 남은 claude.ai 로그인으로
      claude.ai/api/organizations/<조직>/usage?at_wall=1 을 읽는다. 창 없는(headless) 크롬은 claude.ai 보안 확인
      ("잠시만 기다리십시오")에 막혀서, 화면 밖 위치에 최소화한 창으로 띄운다.
실행 시점: AI 오피스가 AI 한도 탭을 볼 때 이 파일이 3시간 넘게 묵었으면 배경에서 한 번 부른다(src/aiquota.js).
      손으로: python reset_credits.py [라벨 ...]
입력: accounts.json 라벨, chrome/<라벨> 프로필
출력: reset_credits.json {at, accounts:{라벨:{ok, at, why, weekly, grants:[...], juniper:{...}}}}
      메일 주소·조직 번호·열쇠는 적지 않는다.
의도적 미구현: 초기화권 사용(청구) 호출. 읽기만 한다. 쓰는 건 사람이 claude.ai 화면에서 누른다.
      로그인이 풀린 프로필에 로그인 시도 안 함(그 계정은 why 에 적고 넘어간다).
겹침: 그 라벨 전용 크롬이 이미 떠 있으면(relogin 중) 새로 띄우지 않고 그 창에 붙는다.
마지막 점검: 2026-11-19
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from relogin import CHROME_DIR, chrome_bin, labels, port_for  # noqa: E402

OUT = BASE / "reset_credits.json"
LOCK = BASE / "reset_credits.lock"
PROBE_PORT = 9349   # 수집 전용(재로그인 창 9250대와 안 겹침)
CHALLENGE = ("기다리십시오", "moment", "Just a moment")


def _alive(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5)
        return True
    except Exception:
        return False


def _grant(g: dict) -> dict:
    lab = str(g.get("label") or "")
    clears = list(g.get("clears") or [])
    if "fable" in lab.lower():
        kind = "Fable 한도 초기화"
    elif "seven_day" in clears:
        kind = "주간 한도 초기화"
    else:
        kind = "한도 초기화"
    return {
        "id": str(g.get("id") or ""), "kind": kind, "label": lab,
        "left": int(g.get("resets_left") or 0), "total": int(g.get("resets_total") or 0),
        "endsAt": g.get("ends_at") or "", "startsAt": g.get("starts_at") or "",
        "usableNow": bool(g.get("usable_now")), "paused": bool(g.get("paused")),
        "why": g.get("unavailable_reason") or "",
    }


def _read_one(pg) -> dict:
    pg.goto("https://claude.ai/new", timeout=60000)
    for _ in range(25):
        t = pg.title() or ""
        if not any(c in t for c in CHALLENGE):
            break
        time.sleep(1.5)
    if "/login" in pg.url:
        return {"ok": False, "why": "이 계정 전용 크롬의 claude.ai 로그인이 풀렸습니다"}
    orgs = pg.evaluate("async()=>{const r=await fetch('/api/organizations');return [r.status, r.status===200? await r.json(): null]}")
    if orgs[0] != 200 or not isinstance(orgs[1], list):
        return {"ok": False, "why": f"claude.ai 조직 조회 실패(응답 {orgs[0]})"}
    chat = [o for o in orgs[1] if "chat" in (o.get("capabilities") or [])]
    if not chat:
        return {"ok": False, "why": "구독 조직을 못 찾았습니다"}
    org = chat[0]
    r = pg.evaluate("async u=>{const r=await fetch(u);return [r.status, r.status===200? await r.json(): null]}",
                    f"/api/organizations/{org['uuid']}/usage?at_wall=1")
    if r[0] != 200 or not isinstance(r[1], dict):
        return {"ok": False, "why": f"사용량 조회 실패(응답 {r[0]})"}
    u = r[1]
    ce = u.get("cedar_ember") or {}
    jt = u.get("juniper_tide") or {}
    sd = u.get("seven_day") or {}
    grants = [_grant(g) for g in (ce.get("grants") or []) if isinstance(g, dict)]
    return {
        "ok": True, "tier": str(org.get("rate_limit_tier") or ""),
        "weekly": sd.get("utilization"), "weeklyReset": sd.get("resets_at") or "",
        "grants": grants, "left": sum(g["left"] for g in grants),
        "cooldownUntil": ce.get("cooldown_until") or "",
        "juniper": {"available": bool(jt.get("available")), "eligible": bool(jt.get("eligible")),
                    "nextAvailableAt": jt.get("next_available_at") or "", "perWeek": jt.get("resets_per_week")},
    }


def read_label(label: str) -> dict:
    from playwright.sync_api import sync_playwright

    own = None
    port = port_for(label)
    if not _alive(port):   # 재로그인 창이 안 떠 있으면 수집용으로 화면 밖에 띄운다
        prof = CHROME_DIR / label
        if not (prof / "Default").is_dir():
            return {"ok": False, "why": "이 계정 전용 크롬 프로필이 없습니다"}
        port = PROBE_PORT
        own = subprocess.Popen(
            [chrome_bin(), f"--remote-debugging-port={port}", f"--user-data-dir={prof}",
             "--no-first-run", "--no-default-browser-check", "--disable-blink-features=AutomationControlled",
             "--start-minimized", "--window-position=-32000,-32000", "--window-size=800,600", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(40):
            if _alive(port):
                break
            time.sleep(0.5)
    try:
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = b.contexts[0] if b.contexts else b.new_context()
            pg = ctx.new_page()
            try:
                return _read_one(pg)
            finally:
                try:
                    pg.close()
                except Exception:
                    pass
    except Exception as e:
        return {"ok": False, "why": f"크롬 연결 실패({type(e).__name__})"}
    finally:
        if own:
            own.terminate()
            try:
                own.wait(10)
            except Exception:
                own.kill()


def main(argv: list[str]) -> int:
    try:
        if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 900:
            print("[reset_credits] 이미 도는 중"); return 0
        LOCK.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    try:
        try:
            data = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            data = {"accounts": {}}
        targets = argv or labels()
        for lab in targets:
            if lab not in labels():
                continue
            r = read_label(lab)
            r["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            prev = data["accounts"].get(lab) or {}
            if not r.get("ok") and prev.get("ok"):   # 실패면 지난 값은 남기고 이유만 붙인다
                prev = dict(prev, why=r.get("why", ""), failAt=r["at"])
                r = prev
            data["accounts"][lab] = r
            print(f"[reset_credits] {lab}: " + (f"초기화권 {r.get('left')}개" if r.get("ok") and not r.get("why") else r.get("why", "")))
        data["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        tmp = OUT.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, OUT)
        return 0
    finally:
        try:
            LOCK.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
