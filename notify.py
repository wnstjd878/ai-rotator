"""
== 운영 맥락 ==
용도: 계정 순환에서 사람이 알아야 할 일(전 계정 한도 도달 등)을 텔레그램으로 보낸다.
입력: 봇 자격은 ~/projects/main-agent/.env 의 TELEGRAM_TOKEN / CHAT_ID 를 읽어 쓴다.
      (없으면 mkt-agent/.env 로 넘어감. 둘 다 없으면 조용히 아무것도 안 한다.)
출력: 텔레그램 메시지 1건
주의: 자격 값은 이 모듈 안에서만 쓰고 화면·기록·명령 인자에 절대 싣지 않는다.
      보내기 실패는 예외를 올리지 않는다 — 알림 때문에 본 작업이 멈추면 안 되므로.
의도적 미구현: 알림 묶음/재시도 없음. 같은 사고가 반복되면 그때 붙인다.
마지막 점검: 2026-07-22
"""

from __future__ import annotations

import os
import urllib.parse
import urllib.request
from pathlib import Path

ENV_CANDIDATES = [
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


def send(message: str) -> bool:
    """메시지를 보낸다. 보냈으면 True, 자격이 없거나 실패하면 False."""
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


if __name__ == "__main__":
    import sys

    text = " ".join(sys.argv[1:]) or "계정 순환기 알림 시험"
    print("보냄" if send(text) else "못 보냄 (자격 없음 또는 전송 실패)")
