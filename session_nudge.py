"""
== 운영 맥락 ==
(style-guard:off — 코드 주석과 봇이 읽는 지시문이라 전자책 문체 지표 대상이 아니다)
실행 시점: claude_pool.py --tend 안에서 매번(본체 작업 스케줄러 ai-rotator-tend-windows 3분,
           Mac LaunchAgent com.user.claude-tend 180초). 따로 등록된 예약 작업은 없다.
입력: ~/.claude/sessions/<번호>.json (클로드가 창마다 적는 등록 파일: 대화 번호·상태·메시지 통로 주소)
      + 각 창의 대화 기록(~/.claude/projects/<폴더>/<대화 번호>.jsonl) 끝부분
출력: 한도 거절로 멈춘 창에 "이어서 하라" 메시지 1통(창끼리 주고받는 메시지 통로,
      Windows=이름 붙은 파이프, Mac=유닉스 소켓). 보낸 기록은 nudge_state.json.
외부 의존: Claude Code 창 간 메시지 규약(msgV 1). 2.1.267 에서 가짜 창으로 실제 전송을 받아
           형식을 확인했다(2026-09-10). 규약이 바뀌면 조용히 안 먹을 수 있으므로 보내기 실패는
           rotator.log 에 남긴다.
의도적 미구현:
  - 키보드 입력 흉내(창을 앞으로 띄워 타이핑): 사람이 쓰는 중인 창에 끼어들 위험. 메시지 통로만 쓴다.
  - 사람 입력으로 위장(감싸는 표시 없이 보내기): 받는 쪽이 다른 창에서 온 메시지인지 알아야 한다.
  - 여러 번 재촉: 창마다 멈춘 곳 한 번에 1통, 하루 6통, 10분 간격 상한.
재시작 정책: 없음(주기 작업 안에서 매번 새로 판단)
마지막 점검: 2026-09-10
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
import uuid
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
CLAUDE_HOME = Path.home() / ".claude"
NUDGE_FILE = BASE / "nudge_state.json"

KINDS = ("interactive", "bg")      # 사람이 띄운 창(대화형·백그라운드). 봇 단발 호출은 대상 아님
MAX_AGE = 12 * 3600                # 이보다 오래 전에 멈춘 창은 사람이 일부러 둔 것으로 본다
MIN_GAP = 10 * 60                  # 같은 창에 다시 보내기까지 최소 간격
DAY_CAP = 6                        # 같은 창에 하루 최대
TAIL_BYTES = 512 * 1024            # 대화 기록은 수십 MB 라 끝부분만 읽는다
FROM_NAME = "계정 순환기"

NUDGE_TEXT = (
    "계정 한도 때문에 멈췄던 작업을 이어 갑니다. 계정 순환기가 남은 계정으로 이미 바꿔 두었습니다.\n"
    "사용자가 비운 동안 쓰라고 미리 맡겨 둔 자동 이어가기입니다. 멈추기 직전에 하던 작업을 "
    "멈춘 지점부터 그대로 계속 진행하세요. 이 메시지에 따로 답하거나 확인을 구할 필요는 없습니다."
)


def _slug(cwd: str) -> str:
    """클로드가 대화 기록 폴더 이름을 만드는 방식(영숫자 외 전부 '-'). C:\\Users\\X → C--Users-X."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd or "")


def transcript_path(sess: dict, home: Path = CLAUDE_HOME) -> Path | None:
    sid = sess.get("sessionId")
    if not sid:
        return None
    p = home / "projects" / _slug(sess.get("cwd", "")) / f"{sid}.jsonl"
    if p.exists():
        return p
    hits = list((home / "projects").glob(f"*/{sid}.jsonl"))
    return hits[0] if hits else None


def _tail_records(path: Path, nbytes: int = TAIL_BYTES) -> list:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            raw = f.read()
    except OSError:
        return []
    lines = raw.split(b"\n")
    if size > nbytes:
        lines = lines[1:]   # 잘린 첫 줄은 버린다
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def _epoch(ts) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def stall_info(records: list) -> dict | None:
    """대화의 마지막 답변이 한도 거절이면 그 정보를, 아니면 None.

    한도 거절은 모델 '<synthetic>' 인 답변 줄(isApiErrorMessage, error=rate_limit)로 남는다.
    그 뒤로 붙는 건 걸린 시간 기록(system)·첨부 같은 부속 줄이고, 백그라운드 작업 완료 알림이
    새 턴을 부르면 그 턴도 거절받아 같은 모양이 또 쌓인다. 그래서 '마지막 답변 줄' 하나만 본다.
    모델은 거절 전의 진짜 답변에서 가져온다(어느 모델 한도를 봐야 하는지 알아야 하므로).
    """
    last = None
    model = None
    for r in reversed(records):
        if r.get("type") != "assistant":
            continue
        msg = r.get("message") or {}
        if last is None:
            last = r
            continue
        m = msg.get("model")
        if m and m != "<synthetic>":
            model = m
            break
    if not last or not last.get("isApiErrorMessage") or last.get("error") != "rate_limit":
        return None
    text = "".join(c.get("text", "") for c in ((last.get("message") or {}).get("content") or [])
                   if isinstance(c, dict))
    return {"uuid": last.get("uuid"), "at": _epoch(last.get("timestamp")),
            "model": model, "text": text[:120]}


def _pid_alive_default(pid) -> bool:
    try:
        import pool_state
        return pool_state._pid_alive(pid)
    except Exception:
        return False


def live_sessions(home: Path = CLAUDE_HOME, pid_alive=None) -> list:
    alive = pid_alive or _pid_alive_default
    out = []
    for f in (home / "sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("kind") not in KINDS or not d.get("messagingSocketPath"):
            continue
        if not alive(d.get("pid")):
            continue
        out.append(d)
    return out


def _from_addr(target: str) -> str:
    """보내는 쪽 주소. 되돌려 받을 통로는 없지만 받는 쪽이 형식을 볼 수 있어 같은 모양으로 만든다."""
    tag = hashlib.sha256(b"ai-rotator").hexdigest()[:32]
    if target.startswith("\\\\.\\pipe\\"):
        return f"uds:\\\\.\\pipe\\LOCAL\\cc-msg-{tag}"
    return f"uds:{os.path.join(os.path.dirname(target), tag[:16] + '.sock')}"


def build_frame(target: str, text: str, from_name: str = FROM_NAME) -> bytes:
    """창 간 메시지 한 통. 가짜 창으로 실제 전송을 받아 본 모양 그대로다(2026-09-10, msgV 1)."""
    frm = _from_addr(target)
    body = (f'<cross-session-message from="{frm}" from-name="{from_name}" from-mode="default">\n'
            f"{text}\n</cross-session-message>")
    frame = {"msgV": 1, "msg_id": str(uuid.uuid4()), "type": "user",
             "message": {"role": "user", "content": body},
             "priority": "next", "from": frm}
    return (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def send(target: str, text: str, tries: int = 5) -> str | None:
    """보내면 None, 실패하면 사유(문자열). 받는 쪽 대답은 기다리지 않는다(실측: 대답 없이 끝남)."""
    data = build_frame(target, text)
    last = "알 수 없음"
    for _ in range(tries):
        try:
            if target.startswith("\\\\.\\pipe\\"):
                with open(target, "r+b", buffering=0) as f:
                    f.write(data)
            else:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(5)
                try:
                    s.connect(target)
                    s.sendall(data)
                finally:
                    s.close()
            return None
        except OSError as e:
            last = f"{type(e).__name__}({getattr(e, 'winerror', None) or e.errno})"
            time.sleep(0.3)   # 파이프가 다른 연결을 받는 중이면(231) 잠깐 뒤 다시
    return last


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def nudge_stalled(usable, *, log=print, now: float | None = None, home: Path = CLAUDE_HOME,
                  state_file: Path = NUDGE_FILE, sender=send, pid_alive=None) -> list:
    """한도로 멈춘 채 쉬고 있는 창에 이어가기 메시지를 보낸다. 보낸 창 이름 목록.

    `usable(model)` 은 지금 계정으로 그 모델을 쓸 수 있으면 True. 아직 못 쓰면 보내 봐야 또
    거절받고 멈추므로 안 보낸다(다음 회차가 계정이 바뀐 뒤 보낸다).
    창이 일하는 중(busy)이거나 사람 입력·권한 확인을 기다리는 중이면 건드리지 않는다.
    """
    now = time.time() if now is None else now
    rec = _load(state_file)
    sent = []
    ok_cache: dict = {}
    changed = False
    for sess in live_sessions(home, pid_alive):
        if sess.get("status") != "idle":
            continue
        tp = transcript_path(sess, home)
        if tp is None:
            continue
        info = stall_info(_tail_records(tp))
        if not info or not info.get("uuid"):
            continue
        if info["at"] is not None and now - info["at"] > MAX_AGE:
            continue
        sid = sess["sessionId"]
        r = rec.get(sid) or {}
        if r.get("stall") == info["uuid"]:
            continue    # 이 멈춤엔 이미 보냈다
        times = [t for t in (r.get("times") or []) if now - t < 86400]
        if times and now - max(times) < MIN_GAP:
            continue
        name = sess.get("name") or str(sess.get("pid"))
        if len(times) >= DAY_CAP:
            if not r.get("capped"):
                log(f"이어가기: '{name}' 창은 오늘 {DAY_CAP}번 보냈는데 또 멈춰 더 보내지 않는다")
                rec[sid] = dict(r, capped=True)
                changed = True
            continue
        model = info.get("model")
        if model not in ok_cache:
            try:
                ok_cache[model] = bool(usable(model))
            except Exception:
                ok_cache[model] = False
        if not ok_cache[model]:
            continue    # 지금 계정도 그 모델 한도가 막혀 있다. 계정이 바뀐 뒤 다음 회차에 보낸다
        why = sender(sess["messagingSocketPath"], NUDGE_TEXT)
        if why:
            log(f"이어가기: '{name}' 창에 보내기 실패({why})")
            continue
        times.append(now)
        rec[sid] = {"stall": info["uuid"], "times": times, "name": name}
        changed = True
        sent.append(name)
        log(f"이어가기: 한도로 멈춘 '{name}' 창에 이어서 하라고 보냈다 ({info['text'][:60]})")
    if changed:
        # 끝난 대화의 기록은 이틀 지나면 지운다(파일이 계속 커지지 않게)
        rec = {k: v for k, v in rec.items()
               if any(now - t < 2 * 86400 for t in (v.get("times") or [now]))}
        try:
            _save(state_file, rec)
        except OSError as e:
            log(f"이어가기: 보낸 기록 저장 실패({type(e).__name__})")
    return sent
