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
초기화권 쓰기(2026-11-19 사장님 "초기화권 누르면 초기화되는 기능도 붙여봐"): python reset_credits.py --claim <라벨> <초기화권 번호>
      claude.ai/api/organizations/<조직>/reset_rate_limits 에 {program:"cedar_ember", grant_id, request_id} (Claude Code 의
      /rate-limit-options 와 같은 요청). AI 오피스 카드의 "쓰기" 버튼이 확인 창을 거친 뒤에만 부른다. 결과 JSON 한 줄을 찍고 그 계정을 다시 읽는다.
      claude.ai 는 초기화권을 정해진 차례로만 받는다(next_grant_id, 아니면 not_next_grant).
      로그인이 풀린 프로필에 로그인 시도 안 함(그 계정은 why 에 적고 넘어간다).
겹침: 그 라벨 전용 크롬이 이미 떠 있으면(relogin 중이거나 지난 실행이 못 끈 창) 새로 띄우지 않고 그 창에 붙는다.
      어느 창인지는 번호가 아니라 그 프로필 폴더의 DevToolsActivePort 로 찾는다.
계정 확인(2026-11-21 사고): 예전엔 모든 계정이 9349 하나를 같이 썼고, 지난 실행이 못 끈 A 크롬이 9349 에 남자
      B·C 를 부를 때 그 A 창에 붙어 B·C 버튼이 A 초기화권을 썼다. 그래서 (1) 새로 띄울 땐 빈 번호를 고르고
      (2) 끌 땐 자식까지 끄고 (3) 읽기·쓰기 전에 claude.ai 에 로그인된 메일이 accounts.json 의 그 라벨 메일과 같은지
      확인해 다르면 멈춘다. 이 확인을 빼지 말 것.
쓰기 막기(2026-11-21 사장님 "이런 오류 절대 안나게"): claim_blocker 가 쓰기 직전에 본다. 그 한도를 90% 안 썼거나
      12시간 안에 저절로 풀리거나, 사용량·풀리는 시각을 못 읽으면 쓰지 않는다. 앱에서 넘기는 길은 일부러 만들지 않았다.
마지막 점검: 2026-11-21
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
from relogin import ACCOUNTS, CHROME_DIR, chrome_bin, labels  # noqa: E402

OUT = BASE / "reset_credits.json"
LOCK = BASE / "reset_credits.lock"
PROBE_PORTS = range(9349, 9380)   # 수집 전용(재로그인 창 9250대와 안 겹침). 띄울 때마다 빈 번호를 고른다
CHALLENGE = ("기다리십시오", "moment", "Just a moment")


def _alive(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5)
        return True
    except Exception:
        return False


def _free_port() -> int | None:
    import socket
    for p in PROBE_PORTS:
        if _alive(p):
            continue
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return None


def _running_port(prof: Path) -> int | None:
    """그 프로필 크롬이 이미 떠 있으면 그 원격 조작 번호(프로필 폴더의 DevToolsActivePort)."""
    try:
        p = int((prof / "DevToolsActivePort").read_text(encoding="utf-8").split()[0])
    except Exception:
        return None
    return p if _alive(p) else None


def _expected_email(label: str) -> str:
    try:
        for x in json.loads(ACCOUNTS.read_text(encoding="utf-8")):
            if x.get("label") == label:
                return str(x.get("email") or "").strip().lower()
    except Exception:
        pass
    return ""


WHO_JS = "async()=>{const r=await fetch('/api/account');if(r.status!==200)return [r.status,''];const j=await r.json();return [200,String(j.email_address||j.email||'')]}"


def _wrong_account(pg, label: str) -> str:
    """로그인된 claude.ai 계정이 그 라벨 계정이 아니면 이유 문장, 맞으면 빈 문자열."""
    want = _expected_email(label)
    if not want:
        return "accounts.json 에 이 계정 메일이 없어 맞는 계정인지 확인할 수 없습니다"
    st, got = pg.evaluate(WHO_JS)
    if st != 200 or not got:
        return f"로그인된 계정을 확인하지 못했습니다(응답 {st})"
    if got.strip().lower() != want:
        return f"이 계정 전용 크롬에 다른 계정({got})이 로그인돼 있습니다"
    return ""


def _kill_tree(proc) -> None:
    try:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass
    try:
        proc.wait(10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


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
        "why": g.get("unavailable_reason") or "", "clears": clears,
    }


def _limits(u: dict) -> dict:
    """주간 전체·Fable 사용률과 풀리는 시각. limits 칸(weekly_all·weekly_scoped Fable)을 쓰고 없으면 seven_day."""
    out = {}
    for l in u.get("limits") or []:
        if not isinstance(l, dict):
            continue
        name = (((l.get("scope") or {}).get("model") or {}).get("display_name") or "")
        if l.get("kind") == "weekly_all":
            out["weekly"] = {"pct": l.get("percent"), "resetsAt": l.get("resets_at") or ""}
        elif l.get("kind") == "weekly_scoped" and "fable" in str(name).lower():
            out["fable"] = {"pct": l.get("percent"), "resetsAt": l.get("resets_at") or ""}
    sd = u.get("seven_day") or {}
    if "weekly" not in out and sd:
        out["weekly"] = {"pct": sd.get("utilization"), "resetsAt": sd.get("resets_at") or ""}
    return out


def _read_one(pg, label: str) -> dict:
    pg.goto("https://claude.ai/new", timeout=60000)
    for _ in range(25):
        t = pg.title() or ""
        if not any(c in t for c in CHALLENGE):
            break
        time.sleep(1.5)
    if "/login" in pg.url:
        return {"ok": False, "why": "이 계정 전용 크롬의 claude.ai 로그인이 풀렸습니다"}
    bad = _wrong_account(pg, label)
    if bad:
        return {"ok": False, "why": bad, "wrongAccount": True}
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
        "grants": grants, "limits": _limits(u), "left": sum(g["left"] for g in grants), "nextGrantId": ce.get("next_grant_id") or "",
        "atLimit": bool(ce.get("at_limit")),
        "cooldownUntil": ce.get("cooldown_until") or "",
        "juniper": {"available": bool(jt.get("available")), "eligible": bool(jt.get("eligible")),
                    "nextAvailableAt": jt.get("next_available_at") or "", "perWeek": jt.get("resets_per_week")},
    }


CLAIM_JS = """async([org,body])=>{const r=await fetch('/api/organizations/'+org+'/reset_rate_limits',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let j=null;try{j=await r.json()}catch(e){};return [r.status,j]}"""


def _org(pg):
    orgs = pg.evaluate("async()=>{const r=await fetch('/api/organizations');return [r.status, r.status===200? await r.json(): null]}")
    if orgs[0] != 200 or not isinstance(orgs[1], list):
        return None
    chat = [o for o in orgs[1] if "chat" in (o.get("capabilities") or [])]
    return chat[0]["uuid"] if chat else None


def _claim_one(pg, label: str, grant_id: str) -> dict:
    import re
    import uuid
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", grant_id or ""):
        return {"ok": False, "why": "초기화권 번호가 올바르지 않습니다"}
    pre = _read_one(pg, label)   # 로그인·보안 확인 통과 + 맞는 계정인지 확인
    if not pre.get("ok"):
        return {"ok": False, "why": pre.get("why") or "읽기 실패", "wrongAccount": bool(pre.get("wrongAccount"))}
    bad = _wrong_account(pg, label)   # 쓰기 직전 한 번 더
    if bad:
        return {"ok": False, "why": bad, "wrongAccount": True}
    stop = claim_blocker(pre, grant_id)
    if stop:
        return {"ok": False, "why": stop, "blocked": True}
    org = _org(pg)
    if not org:
        return {"ok": False, "why": "claude.ai 조직을 못 찾았습니다"}
    st, j = pg.evaluate(CLAIM_JS, [org, {"program": "cedar_ember", "grant_id": grant_id, "request_id": uuid.uuid4().hex}])
    j = j if isinstance(j, dict) else {}
    res = str(j.get("result") or "")
    return {"ok": st == 200 and res == "reset", "status": st, "result": res, "reason": j.get("reason") or "",
            "resetsLeft": j.get("resets_left"), "cleared": j.get("cleared") or [], "weeklyResetsAt": j.get("weekly_resets_at") or ""}


MIN_USED_PCT = 90      # 이만큼 안 썼으면 쓰지 않는다(덜 쓴 계정에 초기화권을 버리는 것 막기)
MIN_HOURS_LEFT = 12    # 한도가 이 시간 안에 저절로 풀리면 쓰지 않는다(2026-11-21 사장님 "A는 오늘 21시에 초기화인데")


def claim_blocker(pre: dict, grant_id: str, now: float | None = None) -> str:
    """초기화권을 쓰면 안 되는 이유. 쓸 수 있으면 빈 문자열. 확인할 값이 없으면 쓰지 않는다(모르면 멈춤)."""
    from datetime import datetime, timezone
    g = next((x for x in pre.get("grants") or [] if x.get("id") == grant_id), None)
    if not g:
        return "이 계정에 그 초기화권이 없습니다"
    if g.get("left", 0) <= 0:
        return "이미 다 쓴 초기화권입니다"
    if not g.get("usableNow"):
        return "지금은 쓸 수 없는 초기화권입니다" + (" (다른 초기화권 먼저)" if pre.get("nextGrantId") and pre.get("nextGrantId") != grant_id else "")
    fable = "fable" in str(g.get("label") or "").lower()
    lim = (pre.get("limits") or {}).get("fable" if fable else "weekly")
    name = "Fable 한도" if fable else "주간 한도"
    if not lim or lim.get("pct") is None or not lim.get("resetsAt"):
        return name + " 사용량을 확인하지 못해 쓰지 않았습니다"
    pct = float(lim["pct"])
    try:
        at = datetime.fromisoformat(str(lim["resetsAt"]).replace("Z", "+00:00"))
    except Exception:
        return name + " 풀리는 시각을 확인하지 못해 쓰지 않았습니다"
    hours = (at.timestamp() - (now if now is not None else time.time())) / 3600
    when = at.astimezone().strftime("%m/%d %H:%M")
    if hours < MIN_HOURS_LEFT:
        return f"{name}가 {when}에 저절로 풀려서 쓰지 않았습니다({hours:.0f}시간 남음)"
    if pct < MIN_USED_PCT:
        return f"{name}를 아직 {pct:.0f}%만 써서 쓰지 않았습니다({MIN_USED_PCT}% 넘었을 때만 씁니다)"
    return ""


def with_page(label: str, fn):
    from playwright.sync_api import sync_playwright

    own = None
    prof = CHROME_DIR / label
    port = _running_port(prof)   # 이 프로필이 이미 떠 있으면 그 창(번호가 아니라 프로필로 찾는다)
    if port is None:
        if not (prof / "Default").is_dir():
            return {"ok": False, "why": "이 계정 전용 크롬 프로필이 없습니다"}
        port = _free_port()
        if port is None:
            return {"ok": False, "why": "수집용 크롬 번호가 다 차 있습니다"}
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
        if not _alive(port):
            _kill_tree(own)
            return {"ok": False, "why": "이 계정 전용 크롬이 뜨지 않았습니다"}
    try:
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = b.contexts[0] if b.contexts else b.new_context()
            pg = ctx.new_page()
            try:
                return fn(pg)
            finally:
                try:
                    pg.close()
                except Exception:
                    pass
    except Exception as e:
        return {"ok": False, "why": f"크롬 연결 실패({type(e).__name__})"}
    finally:
        if own:
            _kill_tree(own)


def read_label(label: str) -> dict:
    return with_page(label, lambda pg: _read_one(pg, label))


def main(argv: list[str]) -> int:
    if argv[:1] == ["--claim"]:
        if len(argv) != 3 or argv[1] not in labels():
            print(json.dumps({"ok": False, "why": "사용: --claim <라벨> <초기화권 번호>"}, ensure_ascii=False)); return 2
        r = with_page(argv[1], lambda pg: _claim_one(pg, argv[1], argv[2]))
        print("CLAIM " + json.dumps(r, ensure_ascii=False), flush=True)
        argv = [argv[1]]   # 그 계정만 다시 읽어 파일에 반영
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
            if not r.get("ok") and prev.get("ok") and not r.get("wrongAccount"):   # 실패면 지난 값은 남기고 이유만 붙인다
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
