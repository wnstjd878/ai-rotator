"""
== 운영 맥락 ==
용도: 맥에서 자격이 저장되는 진짜 자리(열쇠꾸러미)의 최신 값을, 순환기가 쓰는 두 자리
      (~/.claude/.credentials.json 과 creds/<라벨>.json)로 되비춘다.
실행 시점: launchd com.user.claude-cred-mirror, 5분마다 + 시작 시 1회 (화면 세션 필요)
입력: 열쇠꾸러미 항목 "Claude Code-credentials"
출력: ~/.claude/.credentials.json (미러), creds/<라벨>.json (되저장, prev/ 에 한 세대 백업)
외부 의존: security 명령, api.anthropic.com/api/oauth/profile (자격이 누구 것인지 확인)

왜 필요한가 (2026-07-30 실측):
  클로드는 자격을 갱신할 때마다 재발급 열쇠를 한 번 쓰고 버린다(회전). 화면 세션에서 도는
  봇(launchd)이 갱신하면 그 최신 값은 **열쇠꾸러미에만** 저장되고 파일은 만들어지지 않는다.
  순환기는 파일만 갈아끼우는 설계라, 시간이 지나면 저장본이 전부 폐기된 옛 열쇠가 된다.
  - 계정을 되돌리는 순간 "로그인 만료"(2026-07-29 네 계정 동시 사망의 원인)
  - 원격 접속(ssh) 자리는 열쇠꾸러미를 못 읽어 자격이 아예 없다(session_learn 실패)
  - 순환기가 열쇠꾸러미 항목을 보고 "옛 자격 그림자"로 오판해 사람에게 지우라고 알린다
  되비추기가 돌면 이 셋이 한꺼번에 사라진다.

안전 규칙:
  - 열쇠꾸러미를 못 읽는 자리(원격 접속·화면 잠금)에서는 아무것도 하지 않는다.
  - 자격이 누구 것인지 서버에 물어 확정하지 못하면 저장본은 건드리지 않는다(계정 오염 방지).
  - 값은 파일로만 흐른다. 화면·명령 인자·로그에 남기지 않는다(지문 12자리만).
  - 열쇠꾸러미를 지우지 않는다. 지우는 판단은 claude_pool 쪽 한 곳에만 둔다.

양방향으로 바꾼 이유 (2026-07-31 실사고):
  원래는 열쇠꾸러미 -> 파일 한 방향만 봤다. 그런데 **원격 접속 자리에서 클로드가 돌면
  파일 쪽이 갱신되고**, 갱신은 재발급 열쇠를 회전시키므로 그 순간 열쇠꾸러미 값이 폐기된다.
  그러면 열쇠꾸러미를 먼저 보는 봇(launchd)만 죽는다 — 07-31 07:45~08:45 표지 생성 등
  맥 봇 전체가 한 시간 멈췄다. 한 방향 되비추기는 이 경우를 메우지 못한다.
  이제 두 자리를 견줘 **나중에 받은 쪽을 반대편에 반영**한다.
  어느 쪽이 나중인지는 출입증(access token) 만료 시각으로 가른다 — 갱신할 때마다 새로 받으므로
  더 미래인 쪽이 나중 것이다. 재발급 열쇠 만료 시각은 로그인 시점에 정해져 회전해도 안 바뀌니
  기준으로 쓸 수 없다.
의도적 미구현: 열쇠꾸러미를 못 읽는 자리(원격 접속·화면 잠김)에서의 반대 방향 쓰기.
      견줄 대상을 못 읽으면서 쓰면 최신 값을 옛 값으로 밀 수 있다. 그 자리는 그냥 쉰다.
마지막 점검: 2026-07-31
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
LIVE = Path.home() / ".claude" / ".credentials.json"
SERVICE = "Claude Code-credentials"
LOG = BASE / "rotator.log"

sys.path.insert(0, str(BASE))


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " [되비추기] " + msg
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def creds_dir() -> Path:
    try:
        import pool_state as st

        return Path(getattr(st, "CREDS_DIR", BASE / "creds"))
    except Exception:
        return BASE / "creds"


def fingerprint(obj) -> str | None:
    """재발급 열쇠의 지문. 값 자체는 어디에도 남기지 않는다."""
    try:
        token = (obj.get("claudeAiOauth") or {}).get("refreshToken") or ""
    except Exception:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else None


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def keychain_account() -> str | None:
    """열쇠꾸러미 항목의 계정 이름. 값은 읽지 않는다."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    for line in (r.stdout or "").splitlines():
        if '"acct"' in line and "=" in line:
            name = line.split("=", 1)[1].strip().strip('"')
            if name:
                return name
    return os.environ.get("USER") or None


def read_keychain(account: str):
    """열쇠꾸러미 값을 이 프로세스 안으로만 읽어 온다. 못 읽으면 None.

    **디스크에 쓰지 않는다.** 예전에는 /tmp 파일로 받았는데, 그 파일이 기본 권한(0644)으로
    만들어져 같은 컴퓨터의 다른 프로그램이 재발급 열쇠 원문을 읽을 수 있었다
    (코덱스 검수 2026-07-30, 재현됨). 화면·명령 인자에도 값이 안 실린다(9.1).
    """
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    try:
        obj = json.loads((r.stdout or "").strip())
    except Exception:
        return None
    if not isinstance(obj, dict) or not fingerprint(obj):
        return None
    return obj


def write_atomic(dest: Path, obj) -> None:
    # 자격이 담기므로 만들 때부터 소유자 전용(0600). 내용을 쓴 뒤에 권한을 조이면
    # 그 사이에 다른 프로그램이 읽을 수 있다(코덱스 지적과 같은 틈).
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / ("." + dest.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(obj))
    try:
        os.chmod(tmp, 0o600)  # 남아 있던 파일을 다시 쓴 경우 대비
    except Exception:
        pass
    os.replace(tmp, dest)


def label_of(obj) -> str | None:
    """이 자격이 어느 계정 것인지 서버에 직접 물어 라벨로 바꾼다. 모르면 None.

    출입증이 만료돼 있으면 조회가 안 된다. 그때는 지문 대조로 떨어진다(갱신 직전 값과
    같은 저장본이 있으면 그 라벨). 둘 다 안 되면 None 이라 저장본을 건드리지 않는다.
    """
    try:
        import switch_account as sw
    except Exception:
        return None

    probe = Path("/tmp/.cred_mirror_probe.json")
    try:
        write_atomic(probe, obj)
        email = sw.cred_email(probe)
    except Exception:
        email = None
    finally:
        try:
            probe.unlink(missing_ok=True)
        except Exception:
            pass

    label = None
    try:
        label = sw.label_of_email(email)
    except Exception:
        label = None
    if label:
        return label

    # 서버 조회가 안 될 때: 만료 시각이 같은 저장본을 같은 계정으로 본다.
    # 재발급 열쇠 만료 시각은 그 계정에 로그인한 시점에서 정해지므로 계정마다 다르다.
    try:
        mine = (obj.get("claudeAiOauth") or {}).get("refreshTokenExpiresAt")
    except Exception:
        mine = None
    if not mine:
        return None
    hits = []
    for f in sorted(creds_dir().glob("*.json")):
        o = read_json(f)
        if not o:
            continue
        if (o.get("claudeAiOauth") or {}).get("refreshTokenExpiresAt") == mine:
            hits.append(f.stem)
    return hits[0] if len(hits) == 1 else None


def issued_at(obj) -> int:
    """이 자격을 언제 받았는지 견줄 값. 출입증 만료 시각을 쓴다(클수록 나중에 받은 것).

    출입증은 갱신할 때마다 새로 받으므로 이게 유일하게 믿을 만한 순서다.
    재발급 열쇠 만료 시각은 로그인 시점에 정해져 회전해도 안 바뀌어서 못 쓴다.
    """
    try:
        v = (obj.get("claudeAiOauth") or {}).get("expiresAt")
        return int(v) if v is not None else 0
    except Exception:
        return 0


_push_err = ""


def _warn_hourly(key: str, msg: str) -> None:
    """같은 사유는 한 시간에 한 번만 기록한다(5분 주기라 매번 쓰면 도배).
    도장 파일은 creds/ 아래(커밋 제외 폴더)에 둔다."""
    stamp = creds_dir() / (".warn-" + key)
    try:
        last = float(stamp.read_text(encoding="utf-8"))
    except Exception:
        last = 0.0
    if time.time() - last < 3600:
        return
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass
    log(msg)


PUSH_FAIL_FILE = ".push-fail-streak"   # 연달아 몇 번 못 밀었는지. creds/ 아래(커밋 제외)
PUSH_FAIL_ALERT_AT = 2                 # 이만큼 연달아 실패하면 사람에게 한 번 알린다


def _push_fail_path():
    return creds_dir() / PUSH_FAIL_FILE


def _note_push_fail(err: str) -> None:
    """열쇠꾸러미에 못 민 횟수를 세고, 두 번 연달아면 사람에게 한 번 알린다.

    화면이 잠긴 맥에서는 자격을 저장하는 진짜 자리에 새 값을 못 넣는다. 그대로 두면
    맥에서 도는 봇들이 어느 순간 로그인 만료로 멈춘다(2026-07-31 한 시간 정지가 그 유형).
    기록에만 남기면 사람이 로그를 열어봐야 알 수 있어서(2026-09-04 사용자 요구) 알림을 붙였다.
    한 번은 갈아끼우는 중에도 날 수 있어 넘기고, 두 번째부터 알린다. 성공하면 조용히 지운다.
    """
    p = _push_fail_path()
    try:
        row = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        row = {}
    count = int(row.get("count") or 0) + 1
    alerted = bool(row.get("alerted"))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"count": count, "alerted": alerted or count >= PUSH_FAIL_ALERT_AT,
                                 "at": time.time()}), encoding="utf-8")
    except Exception:
        pass
    if alerted or count < PUSH_FAIL_ALERT_AT:
        return
    try:
        import notify

        notify.send(
            "맥에서 클로드 로그인 정보를 저장하는 자리에 새 값을 못 넣고 있습니다. "
            "맥 화면이 잠겨 있으면 이렇게 됩니다. 맥 본체 화면을 한 번 열어 주세요. "
            "그대로 두면 맥에서 도는 봇들이 로그인 만료로 멈춥니다. "
            f"({count}번 연달아 실패, 사유: {err or '원인 미상'})")
    except Exception:
        pass


def _clear_push_fail() -> None:
    """다시 밀 수 있게 됐다. 센 것을 지운다(다음에 또 막히면 처음부터 센다)."""
    try:
        _push_fail_path().unlink(missing_ok=True)
    except Exception:
        pass


def push_to_keychain(obj) -> bool:
    """파일 쪽이 나중 것일 때 열쇠꾸러미를 그 값으로 맞춘다. 맞췄으면 True.

    봇(launchd)이 실제로 읽는 자리가 열쇠꾸러미다. 여기가 옛 값이면 파일이 멀쩡해도
    봇만 인증 실패한다(2026-07-31 한 시간 정지). 쓰기는 화면 세션에서만 된다.
    실패 사유는 _push_err 에 남긴다 — 예전엔 아무 기록 없이 False 만 돌려줘,
    되비추기가 왜 안 되는지 로그로는 알 수 없었다(2026-08-10 조사 때 확인).
    """
    global _push_err
    _push_err = ""
    try:
        import switch_account as sw
    except Exception as e:
        _push_err = "switch_account 불러오기 실패: %s" % e
        return False
    tmp = creds_dir() / ".mirror-push.json"
    try:
        write_atomic(tmp, obj)
        note = sw.switch_keychain(tmp)
    except Exception as e:
        _push_err = "%s: %s" % (type(e).__name__, str(e)[:120])
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    if not note.startswith("열쇠꾸러미도"):
        _push_err = "switch_keychain 응답: %s" % (note or "(빈 응답)")[:120]
        return False
    return True


def mirror_live(obj, fp: str) -> bool:
    """원격 접속 자리가 읽는 파일에 최신 값을 깐다. 바뀐 게 있으면 True."""
    cur = read_json(LIVE)
    if cur and fingerprint(cur) == fp:
        return False
    write_atomic(LIVE, obj)
    return True


def mirror_store(obj, fp: str, label: str) -> bool:
    """계정 저장본에 최신 값을 되저장한다. 바뀐 게 있으면 True."""
    dest = creds_dir() / (label + ".json")
    cur = read_json(dest)
    if cur and fingerprint(cur) == fp:
        return False
    if dest.exists():
        prev = creds_dir() / "prev"
        prev.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dest, prev / dest.name)
    write_atomic(dest, obj)
    return True


def apply_pending() -> str:
    """원격 접속 자리에서 예약해 둔 계정 전환을 화면 세션에서 마무리한다.

    돌려주는 값: "none"(예약 없음) / "done"(마무리함) / "blocked"(예약은 있는데 아직 못 함).

    되비추기(열쇠꾸러미 → 파일)와 계정 전환(파일 → 열쇠꾸러미)은 방향이 반대다.
    그래서 이게 sync 의 첫 번째 일이고, **"blocked" 면 되비추기로 내려가면 안 된다.**
    내려가면 열쇠꾸러미에 남아 있는 옛 계정 값이 방금 갈아끼운 파일을 덮어써서 전환이
    소리 없이 취소된다(코덱스 검수 2026-07-30).
    """
    p = creds_dir() / ".pending-switch"
    try:
        label = p.read_text(encoding="utf-8").strip()
    except Exception:
        return "none"
    if not label:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
        return "none"

    src = creds_dir() / (label + ".json")
    if not src.exists():
        log("예약된 계정 '%s' 의 저장본이 없어 전환 예약을 지웠습니다." % label)
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
        return "none"

    try:
        import switch_account as sw

        # 예약을 건 뒤 시간이 지나 저장본 출입증이 곧 끝날 수 있다. 그대로 꽂으면 봇들이
        # 한꺼번에 갱신하다 부딪친다(2026-10-22 04:47: 13초 남은 A 를 꽂아 3분 거절).
        fresh = sw.freshen_before_install(src, live_file_too=True)
        if fresh:
            log(fresh)
        note = sw.switch_keychain(src)
    except Exception:
        return "blocked"
    if not note.startswith("열쇠꾸러미도"):
        return "blocked"   # 아직 화면 세션이 아니다. 다음 회차에 다시 해 본다.

    obj = read_json(src)
    if obj:
        mirror_live(obj, fingerprint(obj))
    try:
        p.unlink(missing_ok=True)
    except Exception:
        pass
    log("예약돼 있던 '%s' 계정 전환을 열쇠꾸러미까지 마무리했습니다." % label)
    return "done"


def sync(quiet: bool = False) -> bool:
    """되비추기 1회. 실제로 맞춘 자리가 있으면 True.

    순환기(claude_pool)가 로그인 거절을 받았을 때도 이 함수를 부른다. 두 자리가 어긋나
    나는 거절은 열쇠꾸러미를 지워서가 아니라 되비춰서 고치는 것이 맞다.
    """
    if sys.platform != "darwin":
        return False

    pending = apply_pending()
    if pending == "done":
        # 방금 넣은 값이 최신이다. 되비추기는 다음 회차에 자연히 맞는다.
        return True
    if pending == "blocked":
        # 갈아끼우기를 아직 못 끝냈다. 여기서 되비추면 옛 계정 값이 새 파일을 덮어
        # 전환이 취소된다. 아무것도 하지 않고 다음 회차를 기다린다.
        return False

    account = keychain_account()
    if not account:
        # 항목이 없으면 파일이 원본인 상태다. 되비출 것이 없다.
        return False

    obj = read_keychain(account)
    if obj is None:
        # 원격 접속 창이거나 화면이 잠겨 값을 못 읽는다. 조용히 넘어간다(알림도 안 낸다).
        return False

    fp = fingerprint(obj)

    # 파일 쪽이 나중에 받은 것이면 방향을 뒤집는다.
    # 원격 접속 자리에서 클로드가 돌면 파일만 갱신되고, 그 갱신이 열쇠꾸러미 값을 폐기시켜
    # 봇 자리만 죽는다(2026-07-31). 그때는 파일 값을 열쇠꾸러미로 밀어야 한다.
    live = read_json(LIVE)
    live_fp = fingerprint(live) if live else None
    if live_fp and live_fp != fp and issued_at(live) > issued_at(obj):
        if not push_to_keychain(live):
            # 화면 세션이 아니거나 화면이 잠겼다. 다음 회차에 다시 해 본다.
            # 단 이 상태가 이어지면 봇 자리만 죽어 있는 것이므로 흔적은 남긴다
            # (시간당 1회 — 예전엔 무기록이라 어긋남 자체를 몰랐다).
            _warn_hourly("push-fail", "자격 파일이 더 최신인데 열쇠꾸러미에 못 밀었습니다"
                         "(%s). 다음 회차에 다시 시도합니다." % (_push_err or "원인 미상"))
            _note_push_fail(_push_err)
            return False
        _clear_push_fail()
        label = label_of(live)
        changed_store = mirror_store(live, live_fp, label) if label else False
        where = ["열쇠꾸러미"] + (["'%s' 저장본" % label] if changed_store else [])
        if not quiet:
            log("자격 파일이 더 최신이라 그 값(%s)을 %s에 맞췄습니다." % (live_fp, ", ".join(where)))
        return True

    # 여기까지 왔으면 밀 일이 없거나 열쇠꾸러미가 더 최신이다. 막혀 있던 게 풀린 것이니
    # 실패 센 것을 지운다(안 지우면 한 번 알린 뒤로 영영 다시 안 알린다).
    _clear_push_fail()
    changed_live = mirror_live(obj, fp)

    label = label_of(obj)
    changed_store = False
    if label:
        changed_store = mirror_store(obj, fp, label)

    if changed_live or changed_store:
        where = []
        if changed_live:
            where.append("자격 파일")
        if changed_store:
            where.append("'%s' 저장본" % label)
        if not quiet:
            log("열쇠꾸러미의 최신 자격(%s)을 %s에 되비췄습니다." % (fp, ", ".join(where)))
        return True
    if not label and not quiet:
        log("자격이 누구 것인지 확인하지 못해 저장본은 그대로 뒀습니다(파일만 최신).")
    return False


def main() -> int:
    sync()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
