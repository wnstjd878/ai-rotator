"""
== 운영 맥락 ==
(style-guard:off — 코드 주석과 봇이 읽는 지시문이라 전자책 문체 지표 대상이 아니다)
실행 시점: claude_pool.py --tend 안에서 매번(본체 작업 스케줄러 ai-rotator-tend-windows 3분).
           Mac 에서는 아무것도 안 한다(아래 의도적 미구현).
입력: 이 PC 에 떠 있는 claude 창들의 화면 글자(콘솔 화면 읽기) + 등록된 창이면 대화 기록 끝부분
출력: 한도 거절로 멈춘 창의 입력 칸에 "이어서 계속" 한 줄 + Enter.
      보낸 기록 nudge_state.json, 사람이 읽는 기록 rotator.log.
외부 의존: Windows 콘솔 입력 넣기(AttachConsole + WriteConsoleInputW). 2026-09-10 시험 창에서
           한글 문장 + Enter 를 넣어 창이 실제로 답하는 것까지 확인했다. 콘솔 창·Windows Terminal
           탭 둘 다 화면 읽기가 된다.
의도적 미구현:
  - 창끼리 메시지 통로(~/.claude/sessions 의 messagingSocketPath): 받는 쪽이 보낸 사람이 등록된
    창인지 확인해서, 주기 작업이 보낸 메시지는 전달은 되지만 무시된다(2026-09-10 실측). 게다가
    다른 창에서 띄운 창(자식 표시를 물려받은 창)은 등록 자체가 안 돼 있다.
  - Mac: 콘솔 입력 넣기가 없다(Orca 터미널). 필요하면 따로 설계한다.
  - 입력 칸에 글자가 있거나 일하는 중인 창, 권한 확인을 기다리는 창은 건드리지 않는다.
  - 멈춘 지 12시간 넘은 창, 같은 창 10분 안 재발송, 하루 6통 초과는 보내지 않는다.
재시작 정책: 없음(주기 작업 안에서 매번 새로 판단)
마지막 점검: 2026-09-10
"""
from __future__ import annotations

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
MAX_AGE = 12 * 3600                # 대화 기록으로 본 멈춤이 이보다 오래됐으면 사람이 일부러 둔 것
TAIL_BYTES = 512 * 1024

# 한 줄로만 쓴다. 줄바꿈이 들어가면 그 자리에서 먼저 보내진다.
NUDGE_LINE = ("[계정 순환기 자동 이어가기] 계정 한도로 멈췄던 작업입니다. 남은 계정으로 바꿔 두었으니 "
              "멈춘 지점부터 그대로 이어서 계속 진행해 주세요.")

# 실측 문구 두 가지: "You've hit your session limit · resets 7am" /
# "You've reached your Fable limit. Run /usage-credits to continue ..."
LIMIT_RE = re.compile(r"You've (?:hit|reached) your\b.{0,60}?\blimit")
BUSY_RE = re.compile(r"…\s*\(|esc to interrupt", re.I)          # 일하는 중 표시(돌아가는 동사…(초))
RULE_RE = re.compile(r"^─{20,}")
MODEL_RE = re.compile(r"\|\s*((?:Fable|Opus|Sonnet|Haiku)[^|]*?)\s*\|")
# 멈춘 뒤에 붙어도 되는 부속 줄(끝난 뒤 걸린 시간, 끼어드는 스크립트 결과, 요약, 알림)
NOISE_BULLET = re.compile(r"^●\s*(Ran \d+ .*hook|Remote Control|Update installed)", re.I)


# ── 화면 판정(순수 함수, 시험 대상) ─────────────────────────────────────────────

def parse_screen(lines: list) -> dict:
    """창 화면 글자 → {stalled, busy, input_empty, model, limit}.

    화면 아래는 [가로줄] 입력 칸 [가로줄] 상태줄 순서다. 입력 칸 위가 대화 내용이다.
    멈춤 = 대화 내용에 한도 문구가 있고, 그 뒤로 사람 입력(> 로 시작)이나 새 답변(●)이 없다.
    이어가기 줄을 넣으면 그 줄이 한도 문구 뒤에 '>' 로 찍히므로 같은 멈춤에 두 번 넣지 않는다.
    """
    lines = [str(l).replace("\xa0", " ").rstrip() for l in lines]
    rules = [i for i, l in enumerate(lines) if RULE_RE.match(l.strip())]
    res = {"stalled": False, "busy": False, "input_empty": False, "model": None, "limit": ""}
    if len(rules) < 2:
        return res
    top, bottom = rules[-2], rules[-1]
    box = [l.strip() for l in lines[top + 1:bottom]]
    res["input_empty"] = bool(box) and all(l in ("", ">", "❯") for l in box)
    for l in lines[bottom + 1:]:
        m = MODEL_RE.search(l)
        if m:
            res["model"] = m.group(1)
            break
    history = lines[:top]
    res["busy"] = any(BUSY_RE.search(l) for l in history[-8:])
    last = None
    for i, l in enumerate(history):
        if LIMIT_RE.search(l):
            last = i
    if last is None:
        return res
    res["limit"] = LIMIT_RE.search(history[last]).group(0)
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


def _console_type(k, text: str) -> bool:
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

    def keys(s):
        recs = []
        for ch in s:
            for down in (True, False):
                r = REC()
                r.EventType = 1   # KEY_EVENT
                r.Event.KeyEvent = KEY(down, 1, 0x0D if ch == "\r" else 0, 0, ch, 0)
                recs.append(r)
        return (REC * len(recs))(*recs), len(recs)

    h = k.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
    try:
        ok = True
        # 글자와 Enter 를 나눠 넣는다. 한 번에 넣으면 클로드가 붙여넣기로 보고 Enter 를 줄바꿈으로
        # 삼킨다(2026-09-10 실측: 75자 문장 끝의 Enter 가 입력 칸 줄바꿈이 됐다, 30자는 됐다).
        for part, pause in ((text, 0.8), ("\r", 0.0)):
            arr, cnt = keys(part)
            n = wt.DWORD()
            ok = ok and bool(k.WriteConsoleInputW(h, arr, cnt, ctypes.byref(n))) and n.value == cnt
            time.sleep(pause)
        return ok
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
                    res = {"ok": _console_type(k, NUDGE_LINE), "why": ""}
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


def claude_windows() -> list:
    """이 PC 의 사람 창 claude 프로세스 번호(부모가 셸인 것). 백그라운드 창 관리자·봇은 뺀다."""
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
    return sorted(pid for pid, (ppid, exe) in procs.items()
                  if exe == "claude.exe" and procs.get(ppid, (0, ""))[1] in SHELLS)


def _proc_start(pid: int) -> int:
    """프로세스 시작 시각(번호 재사용 구분용). 못 읽으면 0."""
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
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def nudge_stalled(usable, *, log=print, now: float | None = None, home: Path = CLAUDE_HOME,
                  state_file: Path = NUDGE_FILE, windows=None, reader=None, typer=None,
                  start_of=None) -> list:
    """한도로 멈춘 창에 이어가기 줄을 넣는다. 넣은 창 이름 목록.

    `usable(model)` 은 지금 계정으로 그 모델을 쓸 수 있으면 True. 못 쓰면 넣어 봐야 또 멈추므로
    넣지 않는다(다음 회차가 계정이 바뀐 뒤 넣는다).
    등록된 창(대화 기록이 있는 창)은 기록도 한도 거절로 끝나 있어야 넣는다(두 번 확인).
    """
    if os.name != "nt" and windows is None:
        return []
    now = time.time() if now is None else now
    pids = windows() if windows else claude_windows()
    if not pids:
        return []
    screens = (reader or (lambda ps: _run_helper(["--read", *map(str, ps)])))(pids) or {}
    typer = typer or (lambda pid: _run_helper(["--type", str(pid)]) or {"ok": False, "why": "도우미 실패"})
    start_of = start_of or _proc_start
    regs = _registered(home)
    rec = _load(state_file)
    changed, sent, ok_cache = False, [], {}
    for pid in pids:
        got = screens.get(pid) or screens.get(str(pid))
        if not got:
            continue
        scr = parse_screen(got.get("lines") or [])
        if not (scr["stalled"] and scr["input_empty"]):
            continue
        name = got.get("title") or str(pid)
        model = scr["model"]
        sess = regs.get(pid)
        if sess:
            tp = transcript_path(sess, home)
            if tp is not None:
                info = stall_info(_tail_records(tp))
                if not info:
                    continue    # 화면엔 한도 문구가 남아 있어도 대화는 이미 이어졌다
                if info["at"] is not None and now - info["at"] > MAX_AGE:
                    continue
                # 모델은 화면 상태줄이 우선이다. 기록에는 /model 로 바꾸기 전 이름이 남아 있을 수 있다
                model = model or info.get("model")
        key = f"{pid}:{start_of(pid)}"
        r = rec.get(key) or {}
        times = [t for t in (r.get("times") or []) if now - t < 86400]
        if times and now - max(times) < MIN_GAP:
            continue
        if len(times) >= DAY_CAP:
            if not r.get("capped"):
                log(f"이어가기: '{name}' 창은 오늘 {DAY_CAP}번 넣었는데 또 멈춰 더 넣지 않는다")
                rec[key] = dict(r, capped=True)
                changed = True
            continue
        if model not in ok_cache:
            try:
                ok_cache[model] = bool(usable(model))
            except Exception:
                ok_cache[model] = False
        if not ok_cache[model]:
            continue    # 지금 계정도 그 모델 한도가 막혀 있다
        res = typer(pid) or {}
        if not res.get("ok"):
            log(f"이어가기: '{name}' 창에 넣기 실패({res.get('why') or '알 수 없음'})")
            continue
        times.append(now)
        rec[key] = {"times": times, "name": name}
        changed = True
        sent.append(name)
        log(f"이어가기: 한도로 멈춘 '{name}' 창에 이어서 하라고 넣었다 ({scr['limit']}, 모델 {model or '모름'})")
    if changed:
        rec = {k: v for k, v in rec.items() if any(now - t < 2 * 86400 for t in (v.get("times") or [now]))}
        try:
            _save(state_file, rec)
        except OSError as e:
            log(f"이어가기: 넣은 기록 저장 실패({type(e).__name__})")
    return sent


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--read", "--type"):
        raise SystemExit(_helper_main(sys.argv[1:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--scan":
        # 사람이 확인하는 용도: 창마다 판정만 보여 주고 아무것도 넣지 않는다
        ps = claude_windows()
        scr = _run_helper(["--read", *map(str, ps)]) or {}
        for p in ps:
            g = scr.get(str(p)) or {}
            v = parse_screen(g.get("lines") or [])
            print(p, g.get("title", "?"), "멈춤" if v["stalled"] else "-", "일하는중" if v["busy"] else "",
                  "입력칸비어있음" if v["input_empty"] else "입력칸에글자", v["model"], v["limit"])
