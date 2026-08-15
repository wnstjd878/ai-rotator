"""
== 운영 맥락 ==
용도: 저장해둔 계정 자격(creds/<라벨>.json)을 지금 쓰는 자격 파일로 덮어써서
      Claude Code 로그인을 그 계정으로 바꾼다 (재로그인 없음). 세 머신 공통 파일 방식.
      creds/<라벨>.json → ~/.claude/.credentials.json (교체 전 .bak 백업).
방식이 파일 하나로 통일된 이유(2026-07-27 확인): 최신 claude 는 Mac 에서도
      ~/.claude/.credentials.json 파일이 있으면 그걸 우선 읽는다. 그래서 Mac 도
      Keychain 을 안 쓰고 파일 스왑으로 처리한다(Keychain 쓰기는 ssh·launchd 에서
      승인 문제로 막혔었다 — 그 걸림돌이 사라짐).
파일만으로는 부족하다(2026-07-30 실측 정정): claude 는 파일을 읽어 쓰기는 하지만,
      자격을 갱신하면 새 값을 **Keychain 에만** 남기고 파일은 만들지 않는다. 그래서 화면
      세션(launchd 봇)에서는 갈아끼운 파일이 곧 뒤처지고, 다음 갱신부터 옛 계정으로 되돌아간
      것처럼 보인다. 이제 Mac 에서는 파일과 Keychain 을 **함께** 갈아끼운다(switch_keychain).
      Keychain 에 못 쓰는 자리(원격 접속·화면 잠금)에서는 creds/.pending-switch 에 예약만
      남기고, 화면 세션에서 도는 cred_mirror 가 마무리한다.
실행: python switch_account.py <라벨> [--from <떠나는라벨>]
입력: creds/<라벨>.json (그 머신에서 직접 로그인해 저장한 자격이어야 함.
      다른 머신에서 복사한 자격은 access token 만료 후 refresh 가 거부된다 — 복사 금지).
되저장(2026-07-28 추가): 갈아타기 직전에 지금 쓰던 계정의 갱신된 자격을 그 라벨 파일로
      되돌려 저장한다. --from 을 안 주면 state.json 의 current 를 떠나는 계정으로 본다.
      이게 없으면 갱신분이 유실되어 그 계정으로 돌아왔을 때 로그인이 풀린다(save_back 참조).
주의: 교체는 다음 claude 실행부터 적용. 실행 중 세션에는 즉시 반영 안 됨.
      creds/ 파일은 자격정보다. 화면 출력·git 커밋 금지.
마지막 점검: 2026-07-28
"""

import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

try:  # Windows 콘솔·파이프에서 한글 출력이 깨지지 않게
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
CREDS_DIR = BASE / "creds"
LIVE = Path.home() / ".claude" / ".credentials.json"


def list_labels():
    return sorted(f.stem for f in CREDS_DIR.glob("*.json"))


def _fingerprint(path: Path):
    """자격 파일의 재발급 열쇠 지문(sha256). 값 자체는 어디에도 남기지 않는다."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    token = ((data.get("claudeAiOauth") or {}).get("refreshToken")) or ""
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def current_label():
    """state.json 이 기록한 '지금 쓰는 계정'. 못 읽으면 None."""
    try:
        sys.path.insert(0, str(BASE))
        import pool_state as st

        return st.load_state().get("current")
    except Exception:
        return None


PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLI_UA = "claude-cli/2.1.0 (external, cli)"


def cred_email(path=None):
    """그 자격이 실제로 누구 계정인지 서버에 직접 물어본다. 못 알아내면 None.

    **`claude auth status` 를 쓰면 안 된다(2026-07-28 실측).** 그 명령이 말하는 이메일은
    자격 파일이 아니라 `~/.claude.json` 의 oauthAccount 에 따로 캐시된 값이다. 자격 파일을
    바꿔 끼워도 캐시는 안 따라오므로, 파일은 D 인데 status 는 C 라고 답한다. 그걸 믿고
    되저장하면 멀쩡한 계정 자격을 엉뚱한 라벨에 덮어쓴다.
    자격 파일에는 계정 식별자가 없어서 파일만 봐서는 알 수 없고, 이 조회가 유일한 방법이다.
    출입증이 만료된 저장본은 조회가 안 된다(그건 알 수 없음으로 둔다).
    """
    import urllib.error
    import urllib.request

    path = path or LIVE
    try:
        token = json.loads(Path(path).read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"]
    except Exception:
        return None
    req = urllib.request.Request(PROFILE_URL, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": CLI_UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return ((body.get("account") or {}).get("email")) or None


def label_of_email(email):
    if not email:
        return None
    try:
        accounts = json.loads((BASE / "accounts.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    for x in accounts:
        if (x.get("email") or "").lower() == email.lower():
            return x.get("label")
    return None


def _rte(obj):
    try:
        return ((obj or {}).get("claudeAiOauth") or {}).get("refreshTokenExpiresAt")
    except Exception:
        return None


def _label_by_rte(obj):
    """재발급 열쇠 만료 시각으로 주인 라벨을 추정한다. 유일하게 일치할 때만 답한다.

    이 값은 로그인 시점에 정해져 회전해도 안 바뀐다. 그래서 갱신된 자격(지문이 어느
    저장본과도 안 맞는 값)도 서버 조회 없이 가려낼 수 있다.
    """
    mine = _rte(obj)
    if not mine:
        return None
    hits = []
    for f in sorted(CREDS_DIR.glob("*.json")):
        try:
            o = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if _rte(o) == mine:
            hits.append(f.stem)
    return hits[0] if len(hits) == 1 else None


def live_label():
    """지금 실제로 꽂혀 있는 계정의 라벨. 못 알아내면 None.

    1순위는 서버에 직접 물어보기(cred_email). 사람이 순환기 모르게 로그인하면 기록이
    낡는데(사용량이 없어 계정을 바꾸는 건 흔한 일이다), 그 낡은 값을 믿고 되저장하면
    엉뚱한 라벨에 다른 계정 자격이 들어간다.
    조회가 안 되면(연결 실패 등) 지문 대조, 그것도 안 되면(회전된 자격) 재발급 열쇠
    만료 시각의 유일 일치로 떨어진다.
    """
    label = label_of_email(cred_email(LIVE))
    if label:
        return label
    live_fp = _fingerprint(LIVE)
    if live_fp is None:
        return None
    for f in sorted(CREDS_DIR.glob("*.json")):
        if _fingerprint(f) == live_fp:
            return f.stem
    try:
        live = json.loads(LIVE.read_text(encoding="utf-8"))
    except Exception:
        return None
    return _label_by_rte(live)


def save_back(leaving) -> str:
    """갈아타기 직전, 지금 쓰던 계정의 갱신된 자격을 그 라벨 파일에 되저장한다.

    claude 는 실행 중 자격이 낡으면 스스로 새로 발급받아 ~/.claude/.credentials.json 을
    덮어쓴다. 이때 재발급 열쇠(refresh token)도 새것으로 바뀌고 옛 열쇠는 서버에서
    폐기된다. 되저장을 안 하면 creds/<라벨>.json 에 폐기된 옛 열쇠가 남고, 그 계정으로
    되돌아온 순간 "Login expired" 가 뜬다.
    2026-07-28 실제 사고: 이 유실로 저장해둔 계정 네 개가 전부 못 쓰게 됐다.

    라벨을 잘못 알고 덮어쓰면 멀쩡한 계정 하나를 잃는다. 그래서 두 겹으로 막는다.
    (1) 기록(state.current)보다 실측(live_label — claude auth status 의 이메일)을 우선한다.
        사용량이 없어 사람이 /login 으로 계정을 바꾸는 일은 흔하고, 그때 기록이 낡는다.
    (2) 그래도 지금 자격이 다른 라벨의 저장본과 같으면 덮어쓰지 않고 넘어간다.
    돌려주는 값은 기록용 한 줄이다.
    """
    # (맥) 클로드가 갱신분을 열쇠꾸러미로 옮기고 파일을 지웠을 수 있다 — 먼저 복원한다.
    pulled = None
    if sys.platform == "darwin":
        try:
            pulled = _pull_live_from_keychain()
        except Exception:
            pulled = None

    def _done(msg: str) -> str:
        return f"{pulled}. {msg}" if pulled else msg

    if not LIVE.exists():
        return _done("지금 쓰는 자격 파일이 없어 되저장 안 함")
    if not leaving:
        leaving = live_label()  # 기록이 비었어도 실측으로 알아낼 수 있다
        if not leaving:
            return _done("떠나는 계정을 몰라 되저장 안 함")
    dest = CREDS_DIR / f"{leaving}.json"
    if not dest.exists():
        return _done(f"'{leaving}' 저장본이 없어 되저장 안 함")

    live_fp = _fingerprint(LIVE)
    if live_fp is None:
        return _done("지금 자격을 못 읽어 되저장 안 함")
    if live_fp == _fingerprint(dest):
        return _done(f"'{leaving}' 자격에 변화 없음(되저장 불필요)")

    # 기록보다 실측이 우선. 사람이 /login 으로 계정을 바꿔 기록이 낡았을 수 있다.
    actual = live_label()
    if actual and actual != leaving:
        note = f"기록은 '{leaving}' 이지만 실제로는 '{actual}' 이라 그쪽에 저장합니다"
        leaving = actual
        dest = CREDS_DIR / f"{leaving}.json"
        if not dest.exists() or live_fp == _fingerprint(dest):
            return _done(f"{note} — 이미 최신이라 되저장 불필요")
    else:
        note = None

    # 기록된 라벨과 실제 꽂힌 자격이 어긋나면 덮어쓰지 않는다(다른 계정 자격 오염 방지)
    for other in CREDS_DIR.glob("*.json"):
        if other.stem == leaving:
            continue
        if _fingerprint(other) == live_fp:
            return _done(f"지금 자격은 '{leaving}' 이 아니라 '{other.stem}' 의 저장본과 같습니다. "
                         f"덮어쓰지 않고 넘어갑니다")

    # 한 세대 되돌리기용. creds/*.json 목록에 안 섞이도록 하위 폴더에 둔다
    prev_dir = CREDS_DIR / "prev"
    prev_dir.mkdir(exist_ok=True)
    shutil.copy2(dest, prev_dir / f"{leaving}.json")
    shutil.copy2(LIVE, dest)
    done = f"'{leaving}' 의 갱신된 자격을 되저장했습니다"
    return _done(f"{note} — {done}" if note else done)


KEYCHAIN_SERVICE = "Claude Code-credentials"
PENDING = CREDS_DIR / ".pending-switch"


def keychain_account():
    """이미 걸려 있는 열쇠꾸러미 항목의 계정 이름. 없으면 None. 값은 안 읽는다."""
    import subprocess

    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    for line in (r.stdout or "").splitlines():
        if '"acct"' in line and "=" in line:
            name = line.split("=", 1)[1].strip().strip('"')
            if name:
                return name
    return None


def read_keychain_value():
    """열쇠꾸러미 값을 이 프로세스 안으로만 읽어 온다. 못 읽으면 None(원격 접속·화면 잠금 포함).

    값은 디스크·화면·명령 인자에 안 남긴다(9.1). 임시 파일도 안 만든다.
    """
    import subprocess

    if sys.platform != "darwin":
        return None
    acct = keychain_account()
    if not acct:
        return None
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                            "-a", acct, "-w"], capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    try:
        obj = json.loads((r.stdout or "").strip())
    except Exception:
        return None
    if not isinstance(obj, dict) or not ((obj.get("claudeAiOauth") or {}).get("refreshToken")):
        return None
    return obj


def _issued_at(obj) -> int:
    """자격을 언제 받았는지 견줄 값(출입증 만료 시각 — 갱신마다 새로 받아 클수록 나중 것)."""
    try:
        v = ((obj or {}).get("claudeAiOauth") or {}).get("expiresAt")
        return int(v) if v is not None else 0
    except Exception:
        return 0


def _obj_fingerprint(obj):
    token = ((obj or {}).get("claudeAiOauth") or {}).get("refreshToken") or ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest() if token else None


def _write_owner_only(dest: Path, obj) -> None:
    """자격을 담는 파일은 만들 때부터 소유자 전용(0600)으로 쓴다."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / ("." + dest.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(obj))
    os.replace(tmp, dest)


def _owner_of(obj):
    """이 자격의 주인 라벨. 지문 일치 → 서버 조회 → 재발급 열쇠 만료 시각 순으로 가린다."""
    fp = _obj_fingerprint(obj)
    if fp:
        for f in sorted(CREDS_DIR.glob("*.json")):
            if _fingerprint(f) == fp:
                return f.stem
    probe = CREDS_DIR / ".owner-probe.json"
    label = None
    try:
        _write_owner_only(probe, obj)
        label = label_of_email(cred_email(probe))
    except Exception:
        label = None
    finally:
        try:
            probe.unlink(missing_ok=True)
        except Exception:
            pass
    if label:
        return label
    return _label_by_rte(obj)


def _pull_live_from_keychain():
    """(맥) 열쇠꾸러미 값이 파일보다 나중 것이면 자격 파일을 그 값으로 복원한다.

    실측(2026-08-15): 화면 세션의 클로드는 자격을 갱신하면 새 값을 열쇠꾸러미로 옮기고
    **자격 파일을 지워버린다.** 그 상태에서 파일만 보고 되저장하면 갱신분이 유실되고,
    그 계정으로 되돌아오는 순간 로그인이 풀린다(8/14 C·8/15 A 사망의 뿌리).

    주인을 못 알아낸 값은 파일로 승격하지 않는다(코덱스 검수 2026-08-15) — 승격하면
    뒤따르는 되저장이 엉뚱한 라벨을 덮을 수 있다. 그 값은 rescued/ 에 보관만 한다.
    """
    kc = read_keychain_value()
    if not kc:
        return None
    try:
        live = json.loads(LIVE.read_text(encoding="utf-8")) if LIVE.exists() else None
    except Exception:
        live = None
    if live and _issued_at(live) >= _issued_at(kc):
        return None
    if not _owner_of(kc):
        rescued = CREDS_DIR / "rescued"
        rescued.mkdir(exist_ok=True)
        _write_owner_only(rescued / f"kc-{int(time.time())}.json", kc)
        return "열쇠꾸러미에 주인 미상의 더 새 값이 있어 creds/rescued/ 에 보관만 했습니다"
    _write_owner_only(LIVE, kc)
    return "열쇠꾸러미의 갱신분이 더 최신이라 자격 파일을 그 값으로 복원했습니다"


def _rescue_keychain_value(new_src: Path):
    """열쇠꾸러미를 덮어쓰기 직전, 들어 있는 값이 최신 유일본이면 주인 라벨에 되저장한다.

    실사고(2026-08-15 A): 예약 전환이 열쇠꾸러미를 덮으면서 밤새 갱신된 A 값이 유실됐고,
    A 저장본에는 폐기된 옛 열쇠만 남아 계정이 죽었다. 덮기 전에 반드시 건져 둔다.
    """
    kc = read_keychain_value()
    if not kc:
        return None
    fp = _obj_fingerprint(kc)
    if not fp or fp == _fingerprint(new_src):
        return None
    if LIVE.exists() and fp == _fingerprint(LIVE):
        return None  # 파일과 같은 값이면 save_back(파일 경유)이 책임진다
    for f in CREDS_DIR.glob("*.json"):
        if _fingerprint(f) == fp:
            return None  # 이미 라벨 저장본에 있다
    label = _owner_of(kc)
    if label and (CREDS_DIR / f"{label}.json").exists():
        dest = CREDS_DIR / f"{label}.json"
        try:
            cur = json.loads(dest.read_text(encoding="utf-8"))
        except Exception:
            cur = None
        if cur and _issued_at(cur) >= _issued_at(kc):
            return None  # 저장본이 이미 더 나중 것이다
        prev_dir = CREDS_DIR / "prev"
        prev_dir.mkdir(exist_ok=True)
        shutil.copy2(dest, prev_dir / dest.name)
        _write_owner_only(dest, kc)
        return f"덮어쓰기 전에 열쇠꾸러미의 '{label}' 갱신분을 저장본에 되저장했습니다"
    rescued = CREDS_DIR / "rescued"
    rescued.mkdir(exist_ok=True)
    _write_owner_only(rescued / f"kc-{int(time.time())}.json", kc)
    return "덮어쓰기 전 열쇠꾸러미 값의 주인을 몰라 creds/rescued/ 에 보관했습니다"


def switch_keychain(src: Path) -> str:
    """맥 열쇠꾸러미 항목도 같은 값으로 덮어쓴다. 결과를 한 줄로 돌려준다.

    화면 세션에서 도는 봇(launchd)이 실제로 읽는 자리가 여기다. 파일만 갈아끼우면
    다음 자격 갱신 때 봇 자리가 옛 계정으로 되돌아간 것처럼 보인다(2026-07-30 실측).

    값은 표준입력으로만 흐른다. 명령 인자에 넣으면 프로세스 목록에 자격이 남는다(9.1).
    원격 접속·화면 잠금에서는 실패하는데, 그건 고장이 아니라 그 자리의 정상 결과다.
    """
    import os
    import subprocess

    if sys.platform != "darwin":
        return "맥이 아니라 열쇠꾸러미는 건너뜁니다"
    # **이미 있는 항목의 계정 이름을 그대로 써야 한다.** 로그인 사용자 이름으로 새로 쓰면
    # 이름이 다를 때 항목이 하나 더 생기고, 클로드는 먼저 걸린 옛 항목을 계속 읽는다.
    # 그러면 "바꿨습니다"라고 말해 놓고 봇 자리는 안 바뀐 상태가 된다(코덱스 검수 2026-07-30).
    acct = keychain_account() or os.environ.get("USER") or "kwon"
    # 덮어쓰기 전 되저장 — 들어 있는 값이 최신 유일본이면 먼저 건져 둔다(유실 방지).
    try:
        rescue = _rescue_keychain_value(src)
        if rescue:
            print(f"[keychain] {rescue}")
    except Exception:
        pass  # 되저장 실패가 전환 자체를 막으면 안 된다
    try:
        payload = src.read_bytes()
        cmd = 'add-generic-password -U -a "%s" -s "%s" -X "%s"\n' % (
            acct, KEYCHAIN_SERVICE, payload.hex())
        r = subprocess.run(["security", "-i"], input=cmd, capture_output=True,
                           text=True, timeout=20)
    except Exception as e:
        return f"열쇠꾸러미에 못 넣었습니다({type(e).__name__})"
    if r.returncode != 0:
        return "열쇠꾸러미에 못 넣었습니다(원격 접속이거나 화면이 잠긴 자리)"
    return "열쇠꾸러미도 같은 계정으로 맞췄습니다"


def mark_pending(label: str) -> None:
    """열쇠꾸러미에 못 넣었을 때, 화면 세션이 마무리하도록 예약만 남긴다."""
    try:
        PENDING.parent.mkdir(parents=True, exist_ok=True)
        PENDING.write_text(label, encoding="utf-8")
    except Exception:
        pass


def switch_file(src: Path, leaving=None, label=None) -> bool:
    """creds 자격을 지금 쓰는 자격 파일 자리로 복사한다. 세 머신 공통.

    복사 전에 지금 쓰던 계정의 갱신분을 그 라벨 파일로 되저장한다(save_back).
    맥에서는 열쇠꾸러미까지 같이 맞춘다. 못 맞추면 예약을 남긴다.
    """
    LIVE.parent.mkdir(parents=True, exist_ok=True)
    print(f"[save-back] {save_back(leaving)}")
    if LIVE.exists():
        shutil.copy2(LIVE, LIVE.with_suffix(".json.bak"))  # 되돌리기용 백업
    # 열쇠꾸러미를 먼저 처리한다. 덮어쓰기 전 되저장(_rescue_keychain_value)이 갈아탈 라벨의
    # 저장본(src)을 더 새 값으로 갱신할 수 있는데, 파일을 먼저 깔면 그 갱신 전 값이
    # 자격 파일에 남아 다음 호출이 거절받는다. 열쇠꾸러미 다음에 src 를 읽어야 최신이 깔린다.
    if sys.platform == "darwin":
        note = switch_keychain(src)
        print(f"[keychain] {note}")
        if note.startswith("열쇠꾸러미에 못 넣었습니다"):
            mark_pending(label or src.stem)
        else:
            try:
                PENDING.unlink(missing_ok=True)
            except Exception:
                pass
    shutil.copy2(src, LIVE)
    return True


def main():
    labels = list_labels()
    args = [a for a in sys.argv[1:] if a.strip()]
    leaving = None
    if "--from" in args:
        i = args.index("--from")
        if i + 1 < len(args):
            leaving = args[i + 1].strip()
        args = args[:i] + args[i + 2:]
    if not args:
        print("사용법: python switch_account.py <라벨> [--from <떠나는라벨>]")
        print("저장된 계정:", ", ".join(labels) if labels else "(없음 — 먼저 save_account.py)")
        return 1
    label = args[0].strip()
    src = CREDS_DIR / f"{label}.json"
    if not src.exists():
        print(f"[switch] '{label}' 저장본이 없습니다. 저장된 계정: {', '.join(labels)}")
        return 1

    # --from 을 안 주면 기록된 현재 계정을 떠나는 계정으로 본다(사람이 직접 부른 경우)
    if leaving is None:
        leaving = current_label()
    if leaving == label:
        leaving = None  # 같은 계정으로 다시 꽂는 건 되저장 대상이 아니다

    if not switch_file(src, leaving=leaving, label=label):
        return 1
    print(f"[switch] Claude Code 로그인을 '{label}' 로 교체했습니다.")
    print("[switch] 열려있는 claude 를 종료하고 다시 실행하면 적용됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
