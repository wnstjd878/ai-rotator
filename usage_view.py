"""
== 운영 맥락 ==
용도: 계정별 남은 사용량을 화면에 적을 글자로 바꾼다. claude_pool·codex_pool 의 --status 가 쓴다.
실행 시점: 사람이 `cs`/`gs` 를 치거나 `cc`/`codex` 를 켤 때(표시 전용).
입력: 기록해둔 사용량 한 건(weekly_pct/weekly_reset/session_pct/session_reset/at/host)
출력: "주간 남은 35% (08/29 21:00 풀림), 세션 남은 100%(초기화됨) (Mac 3시간 전 기준)" 같은 한 줄
외부 의존: shared_state.host_label(기계 이름 바꾸기), usage_check.fmt_reset(풀리는 시각 문구).
          둘 다 없어도 그 부분만 빠지고 나머지는 그대로 나온다.

2026-08-27 에 더한 것: 아직 안 지난 초기화 시각을 칸마다 적는다. 그 전에는 지금 쓰는 계정만
    시각이 보이고(서버에서 방금 읽으니까) 나머지 계정은 안 보였다. 나머지 계정의 저장해둔
    접근 토큰이 만료돼 조회가 401 로 떨어지면 이 화면으로 오는데, 여기에 시각이 없었다.

핵심 규칙(2026-08-03 사용자 지적으로 바뀐 부분):
    기록에 적힌 초기화 시각이 이미 지났으면 그 창은 다시 채워졌다는 뜻이다.
    예전에는 그 값을 통째로 버려서 화면에 "사용량 못 읽음" 만 남았다(D 계정이 실제로는
    100% 쓸 수 있는데 못 읽음으로 보였다). 이제는 "남은 100%" 로 보여주고,
    실측이 아니라 초기화 시각에서 끌어낸 값이라는 것을 같이 적는다.

의도적 미구현: 이 값으로 계정을 고르거나 막지 않는다(표시 전용). 순서·차단 판단은
    pool_state 의 weekly_reset 과 실제 거절만 쓴다 — 낡은 값으로 계정을 막았다가
    2026-07-29 에 멀쩡한 4계정이 전부 막힌 사고가 있었다.
마지막 점검: 2026-08-03
"""

from __future__ import annotations

import time

KINDS = (("weekly_pct", "weekly_reset", "주간"),
         ("session_pct", "session_reset", "세션"))


def _num(value):
    """숫자로 못 바꾸면 None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def usage_parts(entry: dict, now: float):
    """[(이름, 남은 %, 초기화에서 끌어낸 값인지, 다시 채워지는 시각), ...] 와
    지나간 초기화 시각 중 가장 늦은 것.

    초기화 시각이 지난 창은 남은 100% 로 본다. 그 뒤에 누가 썼으면 그 기계가
    더 새 값을 올리므로(공유 칠판) 여기까지 오지 않는다.

    아직 안 지난 시각은 그대로 들고 나간다 — 화면에 "언제 풀리는지" 를 적으려면 필요하다.
    """
    parts, reset_at = [], None
    rows = [(_num(entry.get(pct_key)), _num(entry.get(reset_key)), name)
            for pct_key, reset_key, name in KINDS]

    # 모델 칸(Fable 등). 계정 전체가 여유여도 이 칸만 바닥날 수 있어서 따로 적는다.
    # 옛 기록에는 이 자리가 없다 — 없으면 그냥 안 적는다(예전 화면 그대로).
    for bucket, row in sorted((entry.get("scoped") or {}).items()):
        if isinstance(row, dict):
            rows.append((_num(row.get("pct")), _num(row.get("reset")), bucket))

    for pct, reset, name in rows:
        if pct is None:
            continue
        if reset is not None and reset <= now:
            parts.append((name, 100.0, True, None))
            reset_at = reset if reset_at is None else max(reset_at, reset)
        else:
            parts.append((name, max(0.0, 100.0 - pct), False, reset))
    return parts, reset_at


def _reset_text(reset) -> str:
    """' (MM/DD HH:MM 풀림)'. 문구는 usage_check 한 벌만 쓴다. 못 불러오면 시각을 뺀다."""
    if reset is None:
        return ""
    try:
        import usage_check

        return usage_check.fmt_reset(reset)
    except Exception:
        return ""


def format_parts(parts) -> str:
    """한 줄 글로. 실측과 초기화 추정이 섞여 있을 때만 어느 쪽이 추정인지 표시한다.

    아직 안 지난 초기화 시각은 칸마다 뒤에 적는다(서버에서 방금 읽었을 때와 같은 모양).
    이미 지나 100% 로 되살린 칸에는 안 적는다 — 지난 시각은 알려줄 게 없다.
    """
    if not parts:
        return ""
    mixed = any(d for _, _, d, _r in parts) and any(not d for _, _, d, _r in parts)
    return ", ".join(
        f"{name} 남은 {left:.0f}%"
        + ("(초기화됨)" if derived and mixed else "")
        + ("" if derived else _reset_text(reset))
        for name, left, derived, reset in parts
    )


def usage_line(entry: dict, now: float) -> str:
    """기록해둔 사용량 한 줄. 보여줄 게 없으면 빈 글."""
    parts, _ = usage_parts(entry, now)
    return format_parts(parts)


def ago_text(sec: float) -> str:
    """지난 시간을 '방금 / 12분 전 / 3시간 전 / 2일 전' 으로."""
    if sec < 60:
        return "방금"
    if sec < 3600:
        return f"{int(sec // 60)}분 전"
    if sec < 86400:
        return f"{int(sec // 3600)}시간 전"
    return f"{int(sec // 86400)}일 전"


def host_label(host) -> str:
    """기계 이름을 사람이 알아보는 이름으로(본체/Mac/winserver). 못 바꾸면 원래 이름."""
    try:
        import shared_state

        return shared_state.host_label(host)
    except Exception:
        return str(host or "다른 기기")


def _at(entry) -> float:
    return _num((entry or {}).get("at")) or 0.0


def pick_entry(state: dict, shared: dict | None, label: str):
    """이 기계의 기록과 다른 기계가 올린 값 중 더 최신인 것. (누가, 기록).

    둘 다 없으면 (None, None). 화면 글자와 모델 칸 요약이 **같은 기록**을 보게 하려고
    따로 뺐다. 서로 다른 기록을 보면 화면에는 남았다고 적히고 요약은 바닥이라고 말한다.
    """
    cands = []
    local = (state.get("usage_seen") or {}).get(label)
    if local:
        cands.append(("이 기기", local))
    remote = (shared or {}).get(label)
    if remote:
        cands.append((host_label(remote.get("host")), remote))
    if not cands:
        return None, None
    return max(cands, key=lambda pair: _at(pair[1]))


def scoped_left(entry: dict | None, bucket: str | None, now: float):
    """그 모델 칸의 남은 %와 다시 채워지는 시각. 정보가 없으면 (None, None).

    초기화 시각이 지난 기록은 다시 채워졌다는 뜻이라 남은 100% 로 본다(usage_parts 와 같은 규칙).
    **정보가 없는 것을 0% 로 보면 안 된다** — 멀쩡한 계정을 바닥으로 몰아 버린다.
    """
    if not entry or not bucket:
        return None, None
    row = (entry.get("scoped") or {}).get(bucket)
    if not isinstance(row, dict):
        return None, None
    pct = _num(row.get("pct"))
    if pct is None:
        return None, None
    reset = _num(row.get("reset"))
    if reset is not None and reset <= now:
        return 100.0, reset
    return max(0.0, 100.0 - pct), reset


def last_seen(state: dict, shared: dict | None, label: str, now: float) -> str:
    """지금 서버에서 못 읽을 때 화면에 적을 글.

    이 기계의 기록과 다른 기계가 올린 값 중 더 최신 것을 쓴다. 언제 어디서 읽은
    값인지 같이 적는다(낡은 값을 지금 값으로 오해하면 안 된다). 초기화 시각이 지나
    100% 로 되살린 값이면 그 사실을 대신 적는다.
    """
    who, best = pick_entry(state, shared, label)
    if best is None:
        return "사용량 못 읽음"
    parts, reset_at = usage_parts(best, now)
    line = format_parts(parts)
    if not line:
        return "사용량 못 읽음"

    if parts and all(d for _, _, d, _r in parts) and reset_at:
        when = time.strftime("%m/%d %H:%M", time.localtime(reset_at))
        return f"{line} ({when} 초기화된 뒤 읽은 값 없음)"

    seen_at = _at(best)
    return f"{line} ({who} {ago_text(now - seen_at) if seen_at else '이전'} 기준)"


def entry_and_note(state: dict, shared: dict | None, label: str, now: float):
    """(기록 한 건, 그 값의 출처 문구). last_seen 과 같은 규칙인데 둘을 갈라 준다.

    화면을 칸별로 나눠 그리면서 값은 막대로, 출처는 머리글 옆에 놓게 됐다.
    last_seen 은 둘을 한 문장으로 붙여 주므로 그 화면에서는 못 쓴다.
    """
    who, best = pick_entry(state, shared, label)
    if best is None:
        return None, "사용량 못 읽음"
    parts, reset_at = usage_parts(best, now)
    if not parts:
        return None, "사용량 못 읽음"
    if all(d for _, _, d, _r in parts) and reset_at:
        when = time.strftime("%m/%d %H:%M", time.localtime(reset_at))
        return best, f"{when} 초기화된 뒤 읽은 값 없음"
    seen_at = _at(best)
    return best, f"{who} {ago_text(now - seen_at) if seen_at else '이전'} 기준"


# ── 화면 그리기(2026-09-04 추가) ───────────────────────────────────────────
# 왜 있는가: 예전에는 계정 한 줄에 상태·주간·세션·모델 칸·풀리는 시각을 쉼표로 이어
#   붙였다. 한 줄이 200자를 넘어가 어느 숫자가 어느 칸인지 눈으로 못 골랐다
#   (2026-09-04 사용자 지적). 칸마다 줄을 나누고 남은 양을 막대로 그린다.
# 글자 고르는 기준: 이 화면은 cp949(한글 코드페이지) 창에도 그대로 나가야 한다.
#   █ ░ ▸ • 는 cp949 에 없어 프로그램이 멈춘다. ■ □ ▶ · → 만 쓴다.
# 정렬 규칙: ■ □ 는 창 설정에 따라 한 칸으로도 두 칸으로도 그려진다. 그래서 **모든
#   줄이 같은 개수**를 쓰게 하고(빈 칸도 □ 로 채운다) 줄끼리만 맞춘다. 막대가 없는
#   머리글 줄과는 맞추려 하지 않는다 — 맞출 수 없다.

BAR_WIDTH = 10
DANGER_LEFT = 20   # 남은 양이 이 아래면 붉게(곧 막힌다)
WARN_LEFT = 50     # 이 아래면 노랗게

_ESC = "\x1b"
_C = {"dim": _ESC + "[2m", "red": _ESC + "[91m", "yellow": _ESC + "[93m",
      "green": _ESC + "[92m", "cyan": _ESC + "[96m", "bold": _ESC + "[1m",
      "off": _ESC + "[0m"}


def supports_color(stream=None) -> bool:
    """색을 내도 되는 창인가. 파이프로 넘길 때는 끈다(글자에 제어부호가 섞인다).

    Windows 옛 창은 프로그램이 켜 줘야 색을 알아본다. 켜지지 않으면 색을 포기한다.
    """
    import os
    import sys

    stream = stream or sys.stdout
    try:
        if not stream.isatty():
            return False
    except Exception:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.name != "nt":
        return True
    if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
        return True
    try:
        import ctypes

        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


def _paint(text: str, *names, color: bool = True) -> str:
    if not color or not names:
        return text
    return "".join(_C[n] for n in names) + text + _C["off"]


def vis_width(text: str) -> int:
    """창에 그려질 칸 수. 한글·전각은 두 칸으로 센다(제어부호는 안 센다)."""
    import re
    import unicodedata

    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in plain)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - vis_width(text))


def _tone(left):
    if left is None:
        return "dim"
    if left <= DANGER_LEFT:
        return "red"
    if left <= WARN_LEFT:
        return "yellow"
    return "green"


def bar(left, width: int = BAR_WIDTH) -> str:
    """남은 양 막대. 조금이라도 남았으면 한 칸은 채운다(0 과 3% 를 구분하려고)."""
    if left is None:
        return "□" * width
    filled = int(round(width * float(left) / 100.0))
    filled = max(0, min(width, filled))
    if left > 0 and filled == 0:
        filled = 1
    return "■" * filled + "□" * (width - filled)


def when_text(epoch, now: float) -> str:
    """'오늘 16:00' / '내일 09:00' / '9/7 화 01:59'. 없거나 지났으면 빈 글."""
    import time

    if not epoch:
        return ""
    try:
        epoch = float(epoch)
    except (TypeError, ValueError):
        return ""
    if epoch <= now:
        return ""
    lt = time.localtime(epoch)
    today = time.localtime(now)
    same = (lt.tm_year, lt.tm_yday) == (today.tm_year, today.tm_yday)
    tomorrow = time.localtime(now + 86400)
    clock = time.strftime("%H:%M", lt)
    if same:
        return f"오늘 {clock}"
    if (lt.tm_year, lt.tm_yday) == (tomorrow.tm_year, tomorrow.tm_yday):
        return f"내일 {clock}"
    return f"{lt.tm_mon}/{lt.tm_mday} {'월화수목금토일'[lt.tm_wday]} {clock}"


def usage_lines(parts, now: float, *, color: bool = True, indent: str = "     ") -> list[str]:
    """칸(주간/세션/모델별)마다 한 줄씩. parts 는 usage_parts() 가 준 그대로."""
    if not parts:
        return []
    name_w = max(vis_width(name) for name, _l, _d, _r in parts)
    out = []
    for name, left, derived, reset in parts:
        tone = _tone(left)
        body = (f"{_pad(name, name_w)}  {_paint(bar(left), tone, color=color)}"
                f" {_paint(f'{left:>4.0f}%', tone, 'bold', color=color)}")
        tail = []
        if derived:
            tail.append("초기화된 뒤 안 읽음")
        when = when_text(reset, now)
        if when:
            tail.append(f"{when} 풀림")
        if tail:
            note = "  " + " · ".join(tail)
            # 여유 있는 칸의 시각은 흐리게 — 바닥난 칸으로 눈이 먼저 가게 한다.
            body += _paint(note, "dim", color=color) if tone in ("green", "dim") else note
        out.append(indent + body)
    return out


def account_block(label: str, marks, parts, now: float, *, current: bool = False,
                  color: bool = True, note: str = "") -> list[str]:
    """계정 하나를 머리글 한 줄 + 칸 줄들로."""
    head = _paint(f"{'▶' if current else ' '} {label}", "bold", "cyan", color=color) \
        if current else f"  {label}"
    bits = list(marks)
    if note:
        bits.append(note)
    if bits:
        head += "  " + _paint(" · ".join(bits), "dim", color=color)
    lines = [head]
    if parts:
        lines += usage_lines(parts, now, color=color)
    else:
        lines.append("     " + _paint("사용량 못 읽음", "dim", color=color))
    return lines
