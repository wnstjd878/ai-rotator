"""
== 운영 맥락 ==
(style-guard:off — 코드 주석과 봇이 읽는 지시문이라 전자책 문체 지표 대상이 아니다)
실행 시점: claude_pool.py --tend 안에서 매번(본체 작업 스케줄러 ai-rotator-tend-windows 3분).
           Mac 에서는 아무것도 안 한다(아래 의도적 미구현).
입력: 이 PC 에 떠 있는 claude 창들의 화면 글자(콘솔 화면 읽기) + 등록된 창이면 대화 기록 끝부분
출력: 한도 거절로 멈춘 창의 입력 칸에 이어가기 한 줄 + Enter.
      넣은 기록 nudge_state.json(저장소 제외), 사람이 읽는 기록 rotator.log "이어가기:".
외부 의존: Windows 콘솔 입력 넣기(AttachConsole + WriteConsoleInputW). 2026-09-10 시험 창에서
           진짜 Fable 한도 멈춤을 만들고 넣기 → 전송까지 확인했다. 콘솔 창·Windows Terminal 탭 둘 다 된다.
판정 원칙(검수 반영 2026-09-10):
  - 한도 문구는 오류 표시(⎿)로 시작하는 줄일 때만 인정한다. 답변·도구 결과가 문구를 인용한 건 무시한다.
  - 그 뒤에 사람 입력(>)이나 새 답변(●)이 없고, 일하는 중 표시가 없고, 입력 칸이 비어 있어야 한다.
  - 등록 파일은 창 시작 시각(procStart)까지 맞아야 믿고, 믿을 수 있으면 대화 기록도 한도로 끝나 있어야 한다.
  - 넣은 뒤 입력 칸 내용이 넣은 문장과 똑같을 때만 Enter 를 누른다(사람 글자와 섞였으면 안 누른다).
  - 이어가기 문장은 "이미 끝났으면 다시 하지 말라" 는 조건을 달아 잘못 넣어도 작업을 되풀이하지 않게 한다.
  - "그 창이 쓰는 계정으로 지금 쓸 수 있나" 는 창 표시로 그 창의 자격(모델별 자리 폴더/공용)을 찾아
    본다(window_places, 2026-09-13). 모델 이름으로 짐작하면 Fable 로 켜서 Opus 로 바꾼 창을 엉뚱한 계정으로 본다.
의도적 미구현:
  - 창끼리 메시지 통로(~/.claude/sessions 의 messagingSocketPath): 받는 쪽이 보낸 사람이 등록된
    창인지 확인해서, 주기 작업이 보낸 메시지는 전달은 되지만 무시된다(2026-09-10 실측). 게다가
    다른 창에서 띄운 창(자식 표시를 물려받은 창)은 등록 자체가 안 돼 있다.
  - Mac: 콘솔 입력 넣기가 없다(Orca 터미널). 필요하면 따로 설계한다.
  - 멈춤을 처음 본 지 12시간 넘은 창, 같은 창 10분 안 재발송, 하루 6통 초과는 넣지 않는다.
재시작 정책: 없음(주기 작업 안에서 매번 새로 판단)
마지막 점검: 2026-09-10
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
CLAUDE_HOME = Path.home() / ".claude"
NUDGE_FILE = BASE / "nudge_state.json"

SHELLS = {"powershell.exe", "pwsh.exe", "cmd.exe", "bash.exe", "wsl.exe", "nu.exe"}
MIN_GAP = 10 * 60                  # 같은 창에 다시 넣기까지 최소 간격
DAY_CAP = 6                        # 같은 창에 하루 최대
MAX_AGE = 12 * 3600                # 멈춤이 이보다 오래됐으면 사람이 일부러 둔 것으로 본다
TAIL_BYTES = 512 * 1024

# 한 줄로만 쓴다. 줄바꿈이 들어가면 그 자리에서 먼저 보내진다.
NUDGE_LINE = ("[계정 순환기 자동 이어가기] 한도 오류로 끊긴 작업이 있으면 끊긴 지점부터 이어서 진행하고, "
              "이미 끝났으면 다시 하지 말고 '이어갈 작업 없음'이라고만 답해 주세요.")

# 실측 문구 두 가지(세션 한도 / 모델 한도). 오류는 '⎿' 로 시작하는 줄에 찍힌다.
LIMIT_LINE_RE = re.compile(r"^\s*⎿\s+(You've (?:hit|reached) your\b.{0,60}?\blimit)")
LIMIT_BUCKET_RE = re.compile(r"your (Fable|Opus|Sonnet|Haiku)\b")
BUSY_RE = re.compile(r"…\s*\(|esc to interrupt", re.I)          # 일하는 중 표시(돌아가는 동사…(초))
RULE_RE = re.compile(r"^─{20,}")
MODEL_RE = re.compile(r"\|\s*((?:Fable|Opus|Sonnet|Haiku)[^|]*?)\s*\|")
# 멈춘 뒤에 붙어도 되는 부속 줄(끝난 뒤 걸린 시간, 끼어드는 스크립트 결과, 알림)
# 2026-09-12 추가: 새 판 claude 는 한도 뒤에 "● Usage limit reached · continuing automatically at 1:10pm ·
# esc or type to cancel" 을 붙이고 그 계정의 재설정 시각까지 기다린다. 새 답변이 아니라 대기 표시다 —
# 이걸 답변으로 보면 계정을 갈아끼워도 그 창은 최대 5시간을 헛기다린다(실측: 두 창이 이 상태로 남음).
# 글자를 넣으면 대기가 취소되고 그 줄이 보내지므로 이어가기 대상이다.
NOISE_BULLET = re.compile(r"^●\s*(Ran \d+ .*hook|Remote Control|Update installed|Usage limit reached)", re.I)


# ── 화면 판정(순수 함수, 시험 대상) ─────────────────────────────────────────────

def _split_screen(lines: list):
    """화면 → (대화 내용, 입력 칸 줄들, 상태줄 이하). 입력 칸 테두리가 안 보이면 None."""
    lines = [str(l).replace("\xa0", " ").rstrip() for l in lines]
    rules = [i for i, l in enumerate(lines) if RULE_RE.match(l.strip())]
    if len(rules) < 2:
        return None
    top, bottom = rules[-2], rules[-1]
    return lines[:top], lines[top + 1:bottom], lines[bottom + 1:]


def _squash(s: str) -> str:
    """입력 칸 글자 비교용: 공백·줄바꿈·앞의 '>' 표시를 뺀다(칸 폭에 따라 줄이 접힌다)."""
    return re.sub(r"\s", "", s or "").lstrip(">❯")


def parse_screen(lines: list) -> dict:
    """창 화면 글자 → {stalled, busy, input_empty, model, limit, limit_bucket, sig}."""
    res = {"stalled": False, "busy": False, "input_empty": False, "model": None,
           "limit": "", "limit_bucket": None, "sig": ""}
    parts = _split_screen(lines)
    if parts is None:
        return res
    history, box, tail = parts
    res["input_empty"] = bool(box) and all(l.strip() in ("", ">", "❯") for l in box)
    for l in tail:
        m = MODEL_RE.search(l)
        if m:
            res["model"] = m.group(1)
            break
    res["busy"] = any(BUSY_RE.search(l) for l in history[-8:])
    last = None
    for i, l in enumerate(history):
        if LIMIT_LINE_RE.match(l):
            last = i
    if last is None:
        return res
    res["limit"] = LIMIT_LINE_RE.match(history[last]).group(1)
    b = LIMIT_BUCKET_RE.search(res["limit"])
    res["limit_bucket"] = b.group(1) if b else None
    # 멈춤 서명은 한도 문구만으로 만든다. 앞뒤 줄을 섞으면 창 크기가 바뀌어 줄이 다시 접힐 때마다
    # 새 멈춤으로 보여 "처음 본 시각" 이 초기화된다(재검수). 같은 창의 다음 멈춤과 구분하는 건
    # 멈춤이 풀리는 순간 기록을 지우는 쪽이 맡는다(nudge_stalled).
    res["sig"] = hashlib.sha1(_squash(res["limit"]).encode("utf-8")).hexdigest()[:16]
    for l in history[last + 1:]:
        s = l.strip()
        if s.startswith((">", "❯", "›")):
            return res          # 한도 뒤에 사람(또는 이어가기) 입력이 있다
        if s.startswith("●") and not NOISE_BULLET.match(s):
            return res          # 한도 뒤에 새 답변이 있다
    res["stalled"] = not res["busy"]
    return res


# ── 대화 기록(등록된 창만 있다) ────────────────────────────────────────────────

def _slug(cwd: str) -> str:
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
        lines = lines[1:]
    out = []
    for ln in lines:
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    return out


def _epoch(ts) -> float | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def stall_info(records: list) -> dict | None:
    """마지막 답변 줄이 한도 거절(isApiErrorMessage, error=rate_limit)이면 그 정보, 아니면 None."""
    last, model = None, None
    for r in reversed(records):
        if r.get("type") != "assistant":
            continue
        if last is None:
            last = r
            continue
        m = (r.get("message") or {}).get("model")
        if m and m != "<synthetic>":
            model = m
            break
    if not last or not last.get("isApiErrorMessage") or last.get("error") != "rate_limit":
        return None
    return {"uuid": last.get("uuid"), "at": _epoch(last.get("timestamp")), "model": model}


def _registered(home: Path = CLAUDE_HOME) -> dict:
    out = {}
    for f in (home / "sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out[int(d.get("pid"))] = d
        except (OSError, ValueError, TypeError):
            continue
    return out


# ── Windows 콘솔 조작(도우미 프로세스 안에서만 부른다) ─────────────────────────

def _k32():
    import ctypes
    import ctypes.wintypes as wt
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.AttachConsole.argtypes = [wt.DWORD]
    k.CreateFileW.restype = wt.HANDLE
    k.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
    k.CloseHandle.argtypes = [wt.HANDLE]
    k.GetConsoleTitleW.argtypes = [wt.LPWSTR, wt.DWORD]
    return k


def _console_lines(k) -> list:
    import ctypes
    import ctypes.wintypes as wt

    class COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class SMALL_RECT(ctypes.Structure):
        _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                    ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]

    class CSBI(ctypes.Structure):
        _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD), ("wAttributes", wt.WORD),
                    ("srWindow", SMALL_RECT), ("dwMaximumWindowSize", COORD)]

    k.GetConsoleScreenBufferInfo.argtypes = [wt.HANDLE, ctypes.POINTER(CSBI)]
    k.ReadConsoleOutputCharacterW.argtypes = [wt.HANDLE, wt.LPWSTR, wt.DWORD, COORD, ctypes.POINTER(wt.DWORD)]
    h = k.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)
    try:
        info = CSBI()
        if not k.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
            return []
        w = info.dwSize.X
        lines = []
        for y in range(info.srWindow.Top, info.srWindow.Bottom + 1):
            buf = ctypes.create_unicode_buffer(w + 1)
            n = wt.DWORD()
            k.ReadConsoleOutputCharacterW(h, buf, w, COORD(0, y), ctypes.byref(n))
            lines.append(buf.value[:n.value].rstrip())
        return lines
    finally:
        k.CloseHandle(h)


def _put(k, h, s: str, vk: int = 0, ctrl: int = 0) -> bool:
    """입력 칸에 키를 넣는다(누름·뗌 한 쌍씩). `vk`/`ctrl` 은 Ctrl+C 같은 조합키용."""
    import ctypes
    import ctypes.wintypes as wt

    class KEY(ctypes.Structure):
        _fields_ = [("bKeyDown", wt.BOOL), ("wRepeatCount", wt.WORD), ("wVirtualKeyCode", wt.WORD),
                    ("wVirtualScanCode", wt.WORD), ("uChar", wt.WCHAR), ("dwControlKeyState", wt.DWORD)]

    class EV(ctypes.Union):
        _fields_ = [("KeyEvent", KEY), ("_pad", ctypes.c_byte * 16)]

    class REC(ctypes.Structure):
        _fields_ = [("EventType", wt.WORD), ("Event", EV)]

    k.WriteConsoleInputW.argtypes = [wt.HANDLE, ctypes.POINTER(REC), wt.DWORD, ctypes.POINTER(wt.DWORD)]
    recs = []
    for ch in s:
        for down in (True, False):
            r = REC()
            r.EventType = 1   # KEY_EVENT
            r.Event.KeyEvent = KEY(down, 1, vk or (0x0D if ch == "\r" else 0), 0, ch, ctrl)
            recs.append(r)
    arr = (REC * len(recs))(*recs)
    n = wt.DWORD()
    return bool(k.WriteConsoleInputW(h, arr, len(recs), ctypes.byref(n))) and n.value == len(recs)


def _box(k) -> str:
    """지금 입력 칸 글자(공백·줄바꿈 뺀 것). 테두리가 안 보이면 빈 문자열."""
    parts = _split_screen(_console_lines(k))
    return _squash("".join(parts[1])) if parts else ""


def _clear_box(k, h) -> bool:
    """Ctrl+C 로 입력 칸을 비운다(글자가 있을 때만 부른다 — 빈 칸에 두 번 누르면 창이 닫힌다)."""
    _put(k, h, "\x03", vk=0x43, ctrl=0x0008)   # LEFT_CTRL_PRESSED
    time.sleep(0.8)
    return _box(k) == ""


def _console_type(k, text: str) -> dict:
    """입력 칸에 글자를 넣고, 칸에 그 글자만 보이면 Enter 를 누른다. {ok, why}."""
    h = k.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
    try:
        if not _put(k, h, text):
            return {"ok": False, "why": "글자 넣기 실패"}
        # 글자와 Enter 를 나눠 넣는다. 한 번에 넣으면 클로드가 붙여넣기로 보고 Enter 를 줄바꿈으로
        # 삼킨다(2026-09-10 실측: 75자 문장 끝의 Enter 가 입력 칸 줄바꿈이 됐다).
        # 그 사이 사람이 친 글자와 섞였으면 Enter 를 누르지 않는다(검수 반영).
        want = _squash(text)
        for _ in range(12):          # 화면이 늦게 그려질 수 있어 최대 6초 기다린다
            time.sleep(0.5)
            if _box(k) == want:
                return {"ok": _put(k, h, "\r"), "why": ""}
        # 끝내 안 맞으면 Enter 대신 입력 칸을 비운다. 안 비우면 나중에 사람이 친 글과 같이 전송되고,
        # 칸이 차 있어 다음 회차도 손을 못 댄다(재검수 반영).
        cleared = _box(k) != "" and _clear_box(k, h)
        return {"ok": False, "why": "입력 칸 내용이 넣은 문장과 달라 Enter 대신 "
                + ("입력 칸을 비웠다" if cleared else "그대로 뒀다(비우기 실패)")}
    finally:
        k.CloseHandle(h)


def _helper_main(argv) -> int:
    """도우미: `--read <pid>...` 화면·제목 읽기 / `--type <pid>` 멈춤 재확인 뒤 이어가기 줄 넣기.
    결과는 `--out <파일>` 에 JSON 으로 쓴다(콘솔을 떼고 붙이므로 출력 통로를 못 믿는다)."""
    out = argv[argv.index("--out") + 1]
    k = _k32()
    k.FreeConsole()
    res: dict = {}
    if argv[0] == "--read":
        import ctypes
        for p in argv[1:argv.index("--out")]:
            pid = int(p)
            if not k.AttachConsole(pid):
                res[pid] = None
                continue
            try:
                buf = ctypes.create_unicode_buffer(512)
                k.GetConsoleTitleW(buf, 512)
                res[pid] = {"title": buf.value.lstrip("✳◑◐◒◓·* ").strip(), "lines": _console_lines(k)}
            finally:
                k.FreeConsole()
    elif argv[0] == "--type":
        pid = int(argv[1])
        if not k.AttachConsole(pid):
            res = {"ok": False, "why": "붙기 실패"}
        else:
            try:
                scr = parse_screen(_console_lines(k))   # 넣기 직전에 한 번 더 본다
                if not (scr["stalled"] and scr["input_empty"]):
                    res = {"ok": False, "why": "그 사이 상태가 바뀜"}
                else:
                    res = _console_type(k, NUDGE_LINE)
            finally:
                k.FreeConsole()
    Path(out).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return 0


def _run_helper(args: list, timeout: float = 30):
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    fd, out = tempfile.mkstemp(prefix="nudge-", suffix=".json")
    os.close(fd)
    try:
        subprocess.run([str(pyw if pyw.exists() else exe), str(Path(__file__).resolve()), *args, "--out", out],
                       timeout=timeout, creationflags=0x08000000)   # CREATE_NO_WINDOW(깜빡임 방지)
        txt = Path(out).read_text(encoding="utf-8")
        return json.loads(txt) if txt else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def claude_windows(procs: dict | None = None) -> list:
    """이 PC 의 사람 창 claude 프로세스 번호(부모가 셸인 것). 백그라운드 창 관리자·봇은 뺀다."""
    procs = _process_table() if procs is None else procs
    return sorted(pid for pid, (ppid, exe) in procs.items()
                  if exe == "claude.exe" and procs.get(ppid, (0, ""))[1] in SHELLS)


def window_places(pids, parents: dict | None = None, state: dict | None = None) -> dict:
    """창 → 그 창이 쓰는 자격. 'slot:<칸>'(모델별 자리 폴더) / 'shared'(공용 자격). (style-guard:off)

    창 표시(session_claims)는 cc 로 켠 창이면 그 셸 번호로, 가로채기로 켠 창이면 claude 번호로 적힌다.
    그래서 창 번호와 부모 번호를 둘 다 본다. 표시를 못 찾은 창은 결과에서 빠진다(옛 방식으로 짐작).
    2026-09-13 실사고: 모델 이름만으로 짐작해 Fable 자리의 Opus 창을 Opus 자리 계정(D)으로 판단했고,
    A 의 5시간 한도에 막힌 그 창에 이어가기를 넣어 곧바로 또 막혔다.
    """
    import pool_state as st
    try:
        state = st.load_state() if state is None else state
        claims = st._clean_claims(state.get("session_claims"))
    except Exception:
        return {}
    if parents is None:
        parents = {p: pp for p, (pp, _) in _process_table().items()}
    out = {}
    for pid in pids:
        for key in (pid, parents.get(pid)):
            item = claims.get(str(key)) if key else None
            if item and st._same_process(key, item.get("started")) is not False:
                out[pid] = f"slot:{item['slot']}" if item.get("slot") else "shared"
                break
    return out


def _process_table() -> dict:
    """이 PC 의 프로세스 {번호: (부모 번호, 실행 파일 이름 소문자)}."""
    import ctypes
    import ctypes.wintypes as wt

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PE)]
    k.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PE)]
    k.CloseHandle.argtypes = [wt.HANDLE]
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    procs = {}
    try:
        e = PE()
        e.dwSize = ctypes.sizeof(PE)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            procs[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile.lower())
            ok = k.Process32NextW(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return procs


def _proc_start(pid: int) -> int:
    """프로세스 시작 시각(FILETIME, 등록 파일의 procStart 와 같은 단위). 못 읽으면 0."""
    import ctypes
    import ctypes.wintypes as wt
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wt.HANDLE
    k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    PFT = ctypes.POINTER(wt.FILETIME)
    k.GetProcessTimes.argtypes = [wt.HANDLE, PFT, PFT, PFT, PFT]
    k.CloseHandle.argtypes = [wt.HANDLE]
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return 0
    try:
        c, e, a, b = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        if not k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(a), ctypes.byref(b)):
            return 0
        return (c.dwHighDateTime << 32) | c.dwLowDateTime
    finally:
        k.CloseHandle(h)


# ── 판단(시험 가능하게 읽기·넣기를 바꿔 끼울 수 있다) ───────────────────────────

def _load(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def nudge_stalled(usable, *, log=print, now: float | None = None, home: Path = CLAUDE_HOME,
                  state_file: Path = NUDGE_FILE, windows=None, reader=None, typer=None,
                  start_of=None, places=None) -> list:
    """한도로 멈춘 창에 이어가기 줄을 넣는다. 넣은 창 이름 목록.

    `usable(model)` 은 지금 계정으로 그 모델을 쓸 수 있으면 True. 못 쓰면 넣어 봐야 또 멈추므로
    넣지 않는다(다음 회차가 계정이 바뀐 뒤 넣는다). 창 표시로 그 창의 자격을 찾았으면
    `usable(model, where)` 로 부른다(where = 'slot:<칸>' / 'shared', window_places).
    `places(pids)` 는 {창 번호: where}. 안 주면 실제 창에서는 window_places, 시험에서는 빈 값.
    """
    if os.name != "nt" and windows is None:
        return []
    now = time.time() if now is None else now
    procs = None
    if windows:
        pids = windows()
    else:
        procs = _process_table()
        pids = claude_windows(procs)
    if not pids:
        return []
    if places is None:
        if procs is not None:
            parents = {p: pp for p, (pp, _) in procs.items()}
            places = lambda ps: window_places(ps, parents=parents)   # noqa: E731
        else:
            places = lambda ps: {}   # noqa: E731
    try:
        place_map = places(pids) or {}
    except Exception:
        place_map = {}
    screens = (reader or (lambda ps: _run_helper(["--read", *map(str, ps)])))(pids) or {}
    typer = typer or (lambda pid: _run_helper(["--type", str(pid)], timeout=45)
                      or {"ok": False, "why": "도우미 실패"})
    start_of = start_of or _proc_start
    regs = _registered(home)
    data = _load(state_file)
    wins = data.get("windows") if isinstance(data.get("windows"), dict) else {}
    seen = data.get("seen") if isinstance(data.get("seen"), dict) else {}
    changed, sent, ok_cache = False, [], {}
    keep = set()   # 처음 본 시각을 남길 창: 지금도 멈춰 있거나 이번에 화면을 못 읽은 창
    for pid in pids:
        started = start_of(pid)
        key = f"{pid}:{started}"
        got = screens.get(pid) or screens.get(str(pid))
        if not got:
            keep.add(key)
            continue
        scr = parse_screen(got.get("lines") or [])
        if not scr["stalled"]:
            continue    # 멈춤이 풀린 창의 처음 본 시각은 아래에서 지운다(다음 멈춤은 새로 센다)
        keep.add(key)
        if not scr["input_empty"]:
            continue
        name = got.get("title") or str(pid)
        # 화면에는 멈춘 시각이 없어서 멈춤을 처음 본 때부터 센다(3분마다 보므로 실제와 몇 분 차이)
        skey = f"{key}|{scr['sig']}"
        if skey not in seen:
            seen[skey] = now
            changed = True
        if now - float(seen[skey]) > MAX_AGE:
            continue
        model = scr["limit_bucket"] or scr["model"]   # 한도 문구에 모델 이름이 있으면 그게 정답
        sess = regs.get(pid)
        if sess and started and abs(int(sess.get("procStart") or 0) - started) > 20_000_000:
            sess = None   # 번호만 같은 옛 창의 등록 파일이다(시작 시각이 2초 넘게 다름)
        if sess:
            tp = transcript_path(sess, home)
            if tp is not None:
                info = stall_info(_tail_records(tp))
                if not info:
                    continue    # 화면엔 한도 문구가 남아 있어도 대화는 이미 이어졌다
                if info["at"] is not None and now - info["at"] > MAX_AGE:
                    continue
                model = model or info.get("model")
        r = wins.get(key) or {}
        times = [t for t in (r.get("times") or []) if now - t < 86400]
        if times and now - max(times) < MIN_GAP:
            continue
        if len(times) >= DAY_CAP:
            if not r.get("capped"):
                log(f"이어가기: '{name}' 창은 오늘 {DAY_CAP}번 넣었는데 또 멈춰 더 넣지 않는다")
                wins[key] = dict(r, capped=True)
                changed = True
            continue
        where = place_map.get(pid) or place_map.get(str(pid))
        ck = (where, model)
        if ck not in ok_cache:
            try:
                ok_cache[ck] = bool(usable(model) if where is None else usable(model, where))
            except Exception:
                ok_cache[ck] = False
        if not ok_cache[ck]:
            continue    # 그 창이 쓰는 계정도 그 모델 한도가 막혀 있다
        res = typer(pid) or {}
        if not res.get("ok"):
            log(f"이어가기: '{name}' 창에 넣기 실패({res.get('why') or '알 수 없음'})")
            continue
        times.append(now)
        wins[key] = {"times": times, "name": name}
        changed = True
        sent.append(name)
        log(f"이어가기: 한도로 멈춘 '{name}' 창에 이어서 하라고 넣었다 ({scr['limit']}, 모델 {model or '모름'}, "
            f"자격 {where or '표시 없음'})")
    # 처음 본 시각은 멈춤이 풀린 창·닫힌 창 것만 지운다. 나이로 지우면 이틀 넘게 멈춰 둔 창이
    # 새 멈춤처럼 보여 한 번 들어간다(재검수 반영).
    pruned = {k: v for k, v in seen.items() if k.split("|", 1)[0] in keep}
    if pruned != seen:
        seen, changed = pruned, True
    if changed:
        wins = {k: v for k, v in wins.items() if any(now - t < 2 * 86400 for t in (v.get("times") or [now]))}
        try:
            _save(state_file, {"windows": wins, "seen": seen})
        except OSError as e:
            log(f"이어가기: 넣은 기록 저장 실패({type(e).__name__})")
    return sent


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--read", "--type"):
        raise SystemExit(_helper_main(sys.argv[1:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--scan":
        # 사람이 확인하는 용도: 창마다 판정만 보여 주고 아무것도 넣지 않는다
        procs = _process_table()
        ps = claude_windows(procs)
        where = window_places(ps, parents={p: pp for p, (pp, _) in procs.items()})
        scr = _run_helper(["--read", *map(str, ps)]) or {}
        for p in ps:
            g = scr.get(str(p)) or {}
            v = parse_screen(g.get("lines") or [])
            print(p, g.get("title", "?"), "멈춤" if v["stalled"] else "-", "일하는중" if v["busy"] else "",
                  "입력칸비어있음" if v["input_empty"] else "입력칸에글자", v["model"],
                  where.get(p, "표시없음"), v["limit"])
