"""
== 운영 맥락 ==
용도: 계정 순환에서 사람이 알아야 할 일(전 계정 한도 도달 등)을 텔레그램으로 보낸다.
입력: TELEGRAM_TOKEN / CHAT_ID 를 환경변수 → ~/.config/ai-rotator/notify.env →
      ~/projects/main-agent/.env → ~/projects/mkt-agent/.env 순으로 찾아 쓴다.
      (하나도 없으면 조용히 아무것도 안 한다. Windows 본체엔 봇 .env 가 없어서
       notify.env 가 없으면 알림이 통째로 빠진다 — 2026-07-27 실제로 그랬다.)
출력: 텔레그램 메시지 1건
주의: 자격 값은 이 모듈 안에서만 쓰고 화면·기록·명령 인자에 절대 싣지 않는다.
      보내기 실패는 예외를 올리지 않는다 — 알림 때문에 본 작업이 멈추면 안 되므로.
의도적 미구현: 알림 묶음/재시도 없음. 같은 사고가 반복되면 그때 붙인다.
AI 오피스 경로: 2026-10-22 추가(send 위 설명 참고). 비상 길(stuck_alert)은 urgent=True 로 부른다.
마지막 점검: 2026-10-22
"""

from __future__ import annotations

import os
import urllib.parse
import urllib.request
from pathlib import Path

ENV_CANDIDATES = [
    # 봇이 없는 머신(Windows 본체)용. 자동 백업이 도는 폴더 밖에 둬서 딸려 올라가지 않게 한다.
    Path.home() / ".config" / "ai-rotator" / "notify.env",
    Path.home() / "projects" / "main-agent" / ".env",
    Path.home() / "projects" / "mkt-agent" / ".env",
]
TIMEOUT = 10


def _read_env(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        return {}
    return out


def _creds():
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat = os.environ.get("CHAT_ID", "")
    if token and chat:
        return token, chat
    for path in ENV_CANDIDATES:
        if not path.exists():
            continue
        env = _read_env(path)
        token = env.get("TELEGRAM_TOKEN", "")
        chat = env.get("CHAT_ID", "")
        if token and chat:
            return token, chat
    return "", ""


# 2026-10-22 사용자 "AI 한도 알림도 오피스로": AI 오피스가 켜져 있으면 텔레그램 대신 AI 오피스로 보낸다.
# 이 PC 는 알림함 파일(앱이 읽어 회의실에 올리고 PC 알림을 띄움), 맥은 기존 우편함(aioffice_outbox.route, 10분마다 이 PC 로 넘어옴).
# 로그인·인증처럼 사람이 바로 손써야 하는 것과 urgent=True(맥이 꺼졌을 때 비상 알림 등)는 지금처럼 텔레그램으로 간다.
URGENT_WORDS = ("로그인", "인증", "자격", "토큰", "열쇠", "비밀", "결제", "입금")
ALIVE = Path.home() / ".claude" / ".aioffice-alive"
INBOX = Path.home() / "Documents" / "AI-Office" / "logs" / "notify-inbox.jsonl"
OUTBOX_MAC = Path.home() / ".claude" / "ops" / "aioffice_outbox.py"
# AI 오피스도 맥 우편함도 없는 기계(winserver): 켜짐 표시(.aioffice-alive)를 본컴이 ssh 로 갱신해 주면 이 파일에 쌓고,
# 본컴 aioffice_alerts.py 가 10분마다 `notify.py --take` 로 가져간다. 표시가 낡으면(앱 꺼짐) 텔레그램.
OUTBOX_LOCAL = Path.home() / ".config" / "ai-rotator" / "office-outbox.jsonl"


def _office_alive() -> bool:
    try:
        import time
        return (time.time() - ALIVE.stat().st_mtime) < 25 * 60
    except Exception:
        return False


def _to_office(message: str) -> bool:
    """AI 오피스로 넘겼으면 True. 넘길 길이 없거나 앱이 꺼져 있으면 False(그러면 텔레그램)."""
    try:
        if INBOX.parent.is_dir() and _office_alive():   # AI 오피스가 깔린 이 PC
            import json, time
            with open(INBOX, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "who": "계정 순환", "text": message}, ensure_ascii=False) + "\n")
            return True
        if OUTBOX_MAC.is_file():   # 맥: 우편함이 앱 켜짐을 스스로 판단해, 꺼져 있으면 False 를 돌려준다
            import importlib.util
            spec = importlib.util.spec_from_file_location("_aioffice_outbox", OUTBOX_MAC)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return bool(mod.route(message, who="계정 순환"))
        if _office_alive():   # winserver 처럼 본컴이 켜짐 표시를 갱신해 주는 기계
            import json, time
            OUTBOX_LOCAL.parent.mkdir(parents=True, exist_ok=True)
            with open(OUTBOX_LOCAL, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "who": "계정 순환(winserver)", "text": message}, ensure_ascii=False) + "\n")
            return True
    except Exception:
        return False
    return False


def send(message: str, urgent: bool = False) -> bool:
    """메시지를 보낸다. 보냈으면 True, 자격이 없거나 실패하면 False."""
    if not urgent and not any(w in message for w in URGENT_WORDS) and _to_office(message):
        return True
    token, chat = _creds()
    if not token or not chat:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat, "text": message}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status == 200
    except Exception:
        return False


def _take() -> None:
    """본컴이 ssh 로 부른다: 쌓인 알림을 한 줄씩(ascii JSON) 내놓고 파일을 비운다."""
    import json
    if not OUTBOX_LOCAL.exists():
        return
    tmp = OUTBOX_LOCAL.with_suffix(".taking")
    try:
        OUTBOX_LOCAL.replace(tmp)
    except Exception:
        return
    for line in tmp.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line:
            try:
                print(json.dumps(json.loads(line), ensure_ascii=True))
            except Exception:
                pass
    tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["--alive"]:   # 본컴이 "AI 오피스 켜져 있음"을 알려 줄 때
        ALIVE.parent.mkdir(parents=True, exist_ok=True); ALIVE.write_text("alive", encoding="utf-8"); raise SystemExit(0)
    if sys.argv[1:2] == ["--take"]:
        _take(); raise SystemExit(0)

    text = " ".join(sys.argv[1:]) or "계정 순환기 알림 시험"
    print("보냄" if send(text) else "못 보냄 (자격 없음 또는 전송 실패)")
