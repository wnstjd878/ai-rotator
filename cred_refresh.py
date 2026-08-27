"""
== 운영 맥락 ==
용도: 저장해둔 계정 자격(creds/<라벨>.json)의 접근 열쇠가 만료됐을 때, 같이 저장된
      재발급 열쇠로 새 접근 열쇠를 받아 그 파일만 갱신한다. 지금 안 쓰는 계정의 사용량
      조회가 거절돼 화면이 낡은 기록으로 밀리는 것을 막는 것이 목적이다.
실행 시점: usage_check.get_usage_for 가 자격 만료로 거절당했을 때(사람이 cc/cs 를 칠 때가 대부분).
입력: creds/<라벨>.json 의 refreshToken
출력: 같은 파일(임시 파일에 쓴 뒤 통째로 교체, 이전 세대는 creds/prev/<라벨>.json)
외부 의존: platform.claude.com/v1/oauth/token, cred_mirror(원자 저장·지문)

안전 규칙:
  - **지금 쓰는 계정은 갱신하지 않는다.** 그 자격은 claude 가 스스로 갱신하며, 우리가
    끼어들면 claude 가 들고 있는 값이 폐기된다(2026-07-31 Mac 봇 한 시간 정지가 그 유형).
    부르는 쪽에서 현재 계정을 걸러 넣는다.
  - 갱신은 재발급 열쇠를 **한 번 쓰고 버리는 회전**이다. 그래서 새 값을 반드시 같은 파일에
    저장해야 한다. 저장이 깨지면 그 계정 자격을 잃는다 — 임시 파일에 먼저 쓰고 통째로 바꾼다.
  - 자격은 머신마다 따로다(creds/ 는 git 에 안 들어간다). 여기서 갱신해도 다른 머신의
    같은 계정 자격은 그대로다. 3대가 각자 자기 것을 갱신한다.
  - 값은 파일로만 흐른다. 화면·명령 인자·기록에는 지문 12자리만 남긴다.
  - 재발급 열쇠 자체가 만료됐으면 부르지 않는다(사람이 다시 로그인해야 하는 몫이다).
  - 같은 파일을 둘이 동시에 갱신하면 한쪽이 폐기된 열쇠를 저장한다. 잠금 파일로 막고,
    잠금을 못 잡으면 그냥 물러난다(다른 쪽이 하고 있다).

의도적 미구현: 실패해도 사람을 부르지 않는다. 표시가 낡은 기록으로 떨어질 뿐이고, 진짜
      재로그인 알림은 auto_relogin(매일 09:20)과 아침 점검 봇(08:04)이 이미 한다.
      갱신 성공을 다른 머신에 알리지도 않는다(자격이 머신별로 따로라 알릴 것이 없다).
마지막 점검: 2026-08-27
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # claude CLI 공개 식별자(비밀 아님)
UA = "claude-cli/2.1.0 (external, cli)"
TIMEOUT = 15
LOCK_STALE = 120  # 초. 이보다 오래된 잠금은 죽은 것으로 본다.

last_error = None  # 마지막 실패 사유(종류만, 자격 값은 절대 안 담는다)


def _ms(value):
    """저장본은 밀리초, 우리 계산은 초. 섞이면 만료 판정이 뒤집힌다."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v / 1000 if v > 1e11 else v


def _fingerprint(obj):
    try:
        import cred_mirror

        return cred_mirror.fingerprint(obj)
    except Exception:
        import hashlib

        tok = ((obj or {}).get("claudeAiOauth") or {}).get("refreshToken") or ""
        return hashlib.sha256(tok.encode("utf-8")).hexdigest()[:12] if tok else None


def _write_atomic(dest: Path, obj) -> None:
    """자격이 담기므로 만들 때부터 소유자 전용으로. 부품은 cred_mirror 한 벌을 쓴다."""
    try:
        import cred_mirror

        cred_mirror.write_atomic(dest, obj)
        return
    except Exception:
        pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / ("." + dest.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(obj))
    os.replace(tmp, dest)


def _acquire(lock: Path) -> bool:
    """잠금 하나. 못 잡으면 False — 기다리지 않는다(사람이 화면을 기다리는 자리다)."""
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > LOCK_STALE:
            lock.unlink()
    except OSError:
        pass
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return True
    except OSError:
        return False


def can_refresh(obj, now: float | None = None) -> bool:
    """이 저장본으로 갱신을 시도할 수 있는가(재발급 열쇠가 있고 아직 안 만료됐는가)."""
    o = (obj or {}).get("claudeAiOauth") or {}
    if not o.get("refreshToken"):
        return False
    exp = _ms(o.get("refreshTokenExpiresAt"))
    return exp is None or exp > (now if now is not None else time.time())


def _post(refresh_token: str) -> dict | None:
    """새 자격을 받아온다. 토큰은 본문에만 싣는다(명령 인자·주소에 넣지 않는다)."""
    global last_error
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA,
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 400/401 은 폐기된 열쇠다. 본문에 자격이 섞일 수 있어 읽지 않는다.
        last_error = f"갱신 거절(HTTP {e.code})"
    except Exception as e:
        last_error = f"갱신 실패({type(e).__name__})"
    return None


def refresh_file(cred_path) -> bool:
    """creds/<라벨>.json 의 접근 열쇠를 새로 받아 그 파일을 갱신한다. 성공하면 True.

    부르기 전에 **현재 쓰는 계정이 아닌지** 확인할 것(파일 맨 위 안전 규칙).
    """
    global last_error
    last_error = None
    path = Path(cred_path)
    try:
        obj = json.load(open(path, encoding="utf-8"))
    except Exception:
        last_error = "자격 파일 못 읽음"
        return False
    if not isinstance(obj, dict) or not can_refresh(obj):
        last_error = "재발급 열쇠 없음 또는 만료"
        return False

    lock = path.parent / (".refresh-" + path.name + ".lock")
    if not _acquire(lock):
        last_error = "다른 곳에서 갱신 중"
        return False
    try:
        # 잠금을 잡는 사이에 다른 쪽이 이미 갱신했을 수 있다. 그러면 회전된 열쇠로
        # 또 부르는 셈이라 거절당한다 — 다시 읽어 최신 값으로 부른다.
        try:
            obj = json.load(open(path, encoding="utf-8"))
        except Exception:
            last_error = "자격 파일 못 읽음"
            return False
        o = obj.get("claudeAiOauth") or {}
        data = _post(o.get("refreshToken") or "")
        if not data or not data.get("access_token"):
            if data:
                last_error = "갱신 응답에 접근 열쇠 없음"
            return False

        now = time.time()
        new = dict(o)
        new["accessToken"] = data["access_token"]
        if data.get("refresh_token"):
            new["refreshToken"] = data["refresh_token"]
        expires_in = data.get("expires_in")
        if expires_in:
            new["expiresAt"] = int((now + float(expires_in)) * 1000)
        for key, field in (("refresh_expires_in", "refreshTokenExpiresAt"),):
            if data.get(key):
                new[field] = int((now + float(data[key])) * 1000)
        for key, field in (("scope", "scopes"), ("subscription_type", "subscriptionType")):
            if data.get(key) and not new.get(field):
                new[field] = data[key]

        # 회전이 일어났으면 옛 값은 이미 폐기됐다. 되돌릴 데가 없으니 백업은 참고용이다.
        try:
            import shutil

            prev = path.parent / "prev"
            prev.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, prev / path.name)
        except Exception:
            pass

        out = dict(obj)
        out["claudeAiOauth"] = new
        _write_atomic(path, out)
        return True
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    import sys

    creds = Path(__file__).resolve().parent / "creds"
    labels = sys.argv[1:] or [f.stem for f in sorted(creds.glob("*.json"))]
    for label in labels:
        f = creds / f"{label}.json"
        if not f.exists():
            print(f"{label}: 자격 파일 없음")
            continue
        before = _fingerprint(json.load(open(f, encoding="utf-8")))
        ok = refresh_file(f)
        after = _fingerprint(json.load(open(f, encoding="utf-8"))) if ok else before
        print(f"{label}: {'갱신됨' if ok else '안 함(' + str(last_error) + ')'}"
              f" 지문 {before} -> {after}")
