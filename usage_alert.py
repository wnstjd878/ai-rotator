"""
== 운영 맥락 ==
용도: 지금 쓰는 계정의 사용률을 주기적으로 보고, 세션(5시간)/주간 한도가
      50% / 70% / 90% 를 넘을 때마다 텔레그램으로 남은량을 알린다. (선제 경고)
실행: python usage_alert.py   (winserver 작업 스케줄러 ai-rotator-usage-alert, 10분마다)
      python usage_alert.py --preview  → 지금 값으로 알림 문구만 출력(전송·기록 안 함)
      python usage_alert.py --refresh-order → 계정 순서 기준만 갱신(알림 없음).
        Mac LaunchAgent com.user.ai-rotator-order 가 1시간마다 실행. 봇만 도는 머신은
        사용량을 읽을 일이 없어 순서 기준이 안 쌓이기 때문이다.
동작: 현재 계정(state.current, 없으면 순차상 다음)의 usage 를 /api/oauth/usage 로 읽어
      주간·세션 각각 넘은 단계(50/70/90)를 그 리셋 구간에 한 번씩만 알린다(스팸 방지).
      리셋되면(리셋 시각이 바뀌면) 다시 무장돼 다음 구간에 또 알린다.
      한 번에 여러 단계를 넘었으면(예: 첫 확인에 92%) 가장 높은 단계 하나만 알린다.
문구: 두괄식. 첫 줄이 "어느 계정이 얼마 남았고 다음에 어디로 넘어가는가".
      전환은 순환기가 자동으로 하므로 사람에게 행동을 시키지 않는다(알림만).
      계정 목록은 라벨 순서 = 실제 전환 순서(A→B→C→D)로 두고, 다음 대상만 표시한다.
      잔여 0%(소진) 계정은 "사용 불가"로 갈라 아래에 둔다.
      주간이 0% 면 세션 줄은 빼고 주간만 보여준다(세션이 남아도 그 계정은 못 쓴다).
상태: alert_state.json 에 "이미 알린 (계정, 종류, 리셋구간, 단계)" 를 기록.
자격: 사용률만 읽고, 텔레그램 자격은 notify 가 파일에서 읽음.
의도적 미구현: 굵게·고정폭 표시 없음. notify.send 가 평문(parse_mode 없음)으로 보내므로
      마크다운을 넣으면 기호가 그대로 보인다. 붙이려면 notify.send 부터 고쳐야 한다.
마지막 점검: 2026-07-27
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import pool_state as st  # noqa: E402
import usage_check  # noqa: E402
import notify  # noqa: E402

ALERT_STATE = BASE / "alert_state.json"
THRESHOLDS = [50, 70, 90]  # 넘을 때마다 알리는 단계
SWITCH_SOON = 10           # 남은량이 이 % 이하면 첫 줄에 다음 계정을 붙인다
HISTORY_KEEP = 80
_KIND_NAME = {"wk": "주간", "se": "세션"}


_pct = usage_check.pct_text            # 남은량 표기 (공용)
_fmt_reset_full = usage_check.fmt_reset_full  # 초기화 시각 표기 (공용)


def _share_usage(label: str, usage: dict) -> None:
    """읽은 사용량을 공유 칠판에 올린다(보여주기 전용). 실패해도 알림 본 작업을 막지 않는다.

    이 머신이 쓰는 계정은 여기서만 읽히고, 다른 머신에서는 저장본 출입증이 8시간이라
    조회가 안 된다. 그래서 읽은 김에 올려두면 세 머신이 서로의 계정 잔여량을 볼 수 있다.
    """
    try:
        import shared_state

        if shared_state.enabled():
            shared_state.put_usage(label, usage)
    except Exception:
        pass


def _share_current(label: str) -> None:
    """이 머신이 지금 물려 있는 계정을 공유 칠판에 적는다(어느 화면에서나 보이게)."""
    try:
        import shared_state

        if shared_state.enabled():
            shared_state.put_current(label)
    except Exception:
        pass


def _load_alerted() -> dict:
    if not ALERT_STATE.exists():
        return {}
    try:
        return json.loads(ALERT_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_alerted(d: dict) -> None:
    if len(d) > HISTORY_KEEP:
        d = dict(list(d.items())[-HISTORY_KEEP:])
    try:
        ALERT_STATE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _check(kind: str, pct, reset, label: str, alerted: dict):
    """넘은 단계 중 아직 안 알린 게 있으면 (종류, 남은%, 리셋). 넘은 단계는 모두 마킹."""
    if pct is None:
        return None
    rr = round(float(reset or 0))
    crossed = [t for t in sorted(THRESHOLDS, reverse=True) if pct > t]
    if not crossed:
        return None
    fresh = [t for t in crossed if alerted.get(f"{kind}_{label}_{rr}_{t}") != 1]
    for t in crossed:  # 넘은 단계 전부 마킹(다음 실행에서 중복 안 함)
        alerted[f"{kind}_{label}_{rr}_{t}"] = 1
    if not fresh:
        return None
    return (kind, max(0.0, 100 - float(pct)), reset)


def _others_weekly(label: str, seen: list | None = None) -> list:
    """다른 계정들의 주간 남은량. 라벨 순서 그대로 [(라벨, 남은%)].

    읽은 김에 초기화 시각도 seen 에 모은다 — 다음 계정을 고르는 순서 기준이라 값이 필요하다.
    """
    out = []
    for l in st.account_labels():
        if l == label:
            continue
        cred = BASE / "creds" / f"{l}.json"
        if not cred.exists():
            continue
        u = usage_check.get_usage(cred)
        if not u or u.get("weekly_pct") is None:
            continue
        if seen is not None:
            seen.append((l, u.get("weekly_reset")))
        out.append((l, max(0.0, 100 - float(u["weekly_pct"]))))
    return out


def _next_label(label: str, others: list, state: dict | None = None):
    """이 계정이 막히면 순환기가 다음에 고를 계정.

    순서 규칙은 pool_state.priority_order 한 곳에서 온다(주간 초기화가 이른 것부터).
    이미 다 쓴 계정은 후보에서 뺀다 — 막힘 표시가 아직 안 붙었어도 못 쓰기 때문.
    """
    alive = [l for l, rem in others if rem > 0]
    if not alive:
        return None
    state = st.prune_blocked(st.load_state()) if state is None else state
    order = st.priority_order(state, alive)
    return order[0] if order else None


def build_message(label: str, hits: list, others: list) -> str:
    """알림 본문. 첫 줄에 결론, 그 아래 상세, 그 아래 계정 목록."""
    nxt = _next_label(label, others)
    wk = next((h for h in hits if h[0] == "wk"), None)
    if wk and wk[1] <= 0:
        hits = [wk]  # 주간이 다 차면 세션이 얼마 남았든 못 쓴다 — 세션 줄을 빼서 헷갈리지 않게
    worst = min(hits, key=lambda h: h[1])
    head = f"[클로드] 계정 {label} {_KIND_NAME[worst[0]]} {_pct(worst[1])} 남음"
    if worst[1] <= SWITCH_SOON:
        if nxt:
            head += f", 곧 계정 {nxt}로 자동 전환"
        elif others:  # 목록이 비면 사용량을 못 읽은 것 — 없다고 단정하지 않는다
            head += ", 자동 전환할 계정 없음"

    lines = [head, ""]
    for kind, rem, reset in hits:
        when = _fmt_reset_full(reset)
        lines.append(f"{_KIND_NAME[kind]} {_pct(rem)} 남음" + (f" ({when})" if when else ""))

    ok = [(l, r) for l, r in others if r > 0]
    dead = [(l, r) for l, r in others if r <= 0]
    if ok:
        lines += ["", "전환 가능"]
        lines += [f"계정 {l} {_pct(r)}" + (" (다음)" if l == nxt else "") for l, r in ok]
    if dead:
        lines += ["", "사용 불가"]
        lines += [f"계정 {l} {_pct(r)}" for l, r in dead]
    return "\n".join(lines)


def refresh_order() -> int:
    """알림 판정 없이 계정 순서 기준(주간 초기화 시각)만 갱신한다.

    봇만 도는 머신(Mac)에는 사용량을 읽을 일이 없어 이 값이 안 쌓인다. 비어 있으면
    그 계정이 순서 뒤로 밀려 세 머신이 서로 다른 계정을 고르게 된다.
    """
    state = st.prune_blocked(st.load_state())
    cur = state.get("current")
    if cur:
        _share_current(cur)
    seen = []
    for l in st.account_labels():
        cred = BASE / "creds" / f"{l}.json"
        if not cred.exists():
            continue
        u = usage_check.get_usage_for(cred, is_current=(l == cur))
        if u:
            _share_usage(l, u)  # 다른 머신 화면에서 이 계정 잔여량이 보이게 올려둔다
        if u and u.get("weekly_reset"):
            seen.append((l, u["weekly_reset"]))
    if not seen:
        print(f"순서 갱신 실패: 사용량을 못 읽음 ({usage_check.last_error or '사유 모름'})")
        return 1
    st.remember_resets(seen)
    order = st.priority_order(st.prune_blocked(st.load_state()))
    print("순서 갱신: " + " → ".join(order))
    return 0


def _ensure_shims() -> None:
    """순환 가로채기가 제자리에 있는지 확인하고, 벗겨졌으면 다시 깐다 (2026-08-15).

    codex/claude 를 npm 으로 업데이트하면 실행 파일 링크가 순환 가로채기에서 원래 것으로
    돌아가, 자동 호출이 한 계정만 쓰다 한도에 걸린다. 실제로 배포 검수 게이트가 nvm 을
    로드하면서 nvm 폴더의 codex 가 가로채기를 가려 jangsa-mkt 배포가 막혔다.
    이 검사는 homebrew·nvm 양쪽 링크가 다 가로채기를 가리키는지 보고, 하나라도 벗겨졌으면
    해당 설치 스크립트를 다시 돌린다. 어떤 문제가 생겨도 알림 작업을 멈추지 않는다(try 안).
    winserver(Windows)는 실행 파일이 codex.cmd/codex.ps1/확장자없음이라, .cmd 가 가로채기가
    아니면 .ps1 설치 스크립트를 다시 돌린다(node/codex 를 npm 으로 올리면 벗겨진다).
    """
    import glob
    import os
    import subprocess

    if os.name == "nt":
        win = [("codex", BASE / "install_codex_shim.ps1"),
               ("claude", BASE / "install_shim.ps1")]
        for tool, installer in win:
            try:
                if not installer.exists():
                    continue
                # tool.cmd 를 PATH(그리고 claude 는 ~/.local/bin)에서 찾는다
                cmd_path = None
                for d in (os.environ.get("PATH", "").split(os.pathsep)
                          + [os.path.expanduser("~/.local/bin")]):
                    if not d:
                        continue
                    p = os.path.join(d, tool + ".cmd")
                    if os.path.exists(p):
                        cmd_path = p
                        break
                if not cmd_path:
                    continue
                with open(cmd_path, encoding="utf-8", errors="ignore") as f:
                    body = f.read()
                if "_shim" in body:      # 이미 가로채기
                    continue
                print(f"[shim-guard] {installer.name}: 가로채기가 벗겨져 다시 설치한다")
                subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                "-File", str(installer)],
                               capture_output=True, text=True, timeout=120)
            except Exception as e:
                print(f"[shim-guard] {installer.name} 재설치 실패(무시): {type(e).__name__}")
        return

    tools = [
        (BASE / "codex_shim.sh", "/opt/homebrew/bin/codex",
         os.path.expanduser("~/.nvm/versions/node/*/bin/codex"),
         BASE / "install_codex_shim.sh"),
        (BASE / "claude_shim.sh", "/opt/homebrew/bin/claude",
         os.path.expanduser("~/.nvm/versions/node/*/bin/claude"),
         BASE / "install_shim.sh"),
    ]
    for shim, brew_link, nvm_glob, installer in tools:
        try:
            if not shim.exists() or not installer.exists():
                continue
            links = ([brew_link] if os.path.lexists(brew_link) else []) + glob.glob(nvm_glob)
            if not links:
                continue
            covered = all(os.path.realpath(l) == os.path.realpath(str(shim)) for l in links)
            if covered:
                continue
            print(f"[shim-guard] {installer.name}: 가로채기가 벗겨져 다시 설치한다")
            subprocess.run(["bash", str(installer)], capture_output=True, text=True, timeout=60)
        except Exception as e:
            print(f"[shim-guard] {installer.name} 재설치 실패(무시): {type(e).__name__}")


def check_scoped_reopen(preview: bool = False) -> list:
    """막혀 있던 모델 칸이 다시 열리면 한 번 알린다. 알린 칸 목록을 돌려준다.

    사용자 요청 2026-08-21: 소진 알림에 해제 시각이 적히기는 하지만, 그때까지 사람이
    시각을 외워두고 기다려야 했다. 이 확인은 사용량 조회와 무관하므로(막힘 목록만 본다)
    현재 계정 자격이 없거나 조회가 실패해도 그대로 돈다. 그래서 main 맨 앞에 둔다.

    세 머신이 각자 돌므로 claim_notice 로 통틀어 한 번만 보낸다.
    """
    try:
        with st.with_lock():
            state = st.prune_blocked(st.load_state())
            opened = st.note_scoped_full(state)
            if not preview:
                st.save_state(state)
            free = {b: [l for l in st.usable_labels()
                        if not st.is_blocked(state, l)
                        and not st.is_scoped_blocked(state, l, b)]
                    for b, _ in opened}
    except Exception as exc:   # 이 확인이 실패해도 사용량 경고 본 작업은 계속한다
        print(f"칸 회복 확인 실패: {exc}")
        return []

    sent = []
    for bucket, until in opened:   # 전송은 잠금 밖에서
        if preview:
            print(f"[미리보기] {bucket} 칸이 다시 열렸습니다. 쓸 계정: {free.get(bucket)}")
            continue
        if not st.claim_notice(f"scoped-open:{bucket}:{round(until)}"):
            continue
        who = ", ".join(free.get(bucket) or []) or "확인 필요"
        notify.send(f"클로드 {bucket} 칸이 다시 열렸습니다. 쓸 수 있는 계정: {who}. "
                    f"{bucket} 로 쓰려면 cc 를 다시 켜세요 "
                    f"(이미 떠 있는 창은 계정이 안 바뀝니다).")
        sent.append(bucket)
        print(f"칸 회복 알림 보냄: {bucket}")
    return sent


def main() -> int:
    _ensure_shims()
    preview = "--preview" in sys.argv
    # 막혔던 모델 칸이 열렸는지 먼저 본다. 사용량 조회와 무관해서, 자격이 없거나
    # 조회가 실패해 아래에서 일찍 빠지는 회차에도 이건 지나가야 한다.
    check_scoped_reopen(preview)
    if "--refresh-order" in sys.argv:
        return refresh_order()
    state = st.prune_blocked(st.load_state())
    label = state.get("current") or st.pick_next(state)
    if not label:
        return 0
    cred = BASE / "creds" / f"{label}.json"
    if not cred.exists():
        return 0
    u = usage_check.get_usage_for(cred, is_current=True)
    if not u:
        # 조용히 넘어가면 경고가 통째로 멈춘 걸 아무도 모른다. 실행 기록에는 남긴다.
        print(f"사용량 못 읽음: {label} ({usage_check.last_error or '사유 모름'})")
        return 0
    # 이 머신이 쓰는 계정의 잔여량을 공유 칠판에 올린다. 다른 머신은 이 계정의 출입증이
    # 만료돼 직접 못 읽으므로, 이게 있어야 어느 화면에서나 계정 넷이 다 보인다.
    _share_usage(label, u)
    _share_current(label)

    if preview:  # 단계를 안 넘었어도 지금 값으로 문구를 만들어 보여준다
        hits = [(k, max(0.0, 100 - float(u[p])), u[r])
                for k, p, r in (("wk", "weekly_pct", "weekly_reset"),
                                ("se", "session_pct", "session_reset"))
                if u.get(p) is not None]
        if not hits:
            print("사용량 못 읽음")
            return 0
        print(build_message(label, hits, _others_weekly(label)))
        return 0

    seen = [(label, u.get("weekly_reset"))]  # 다음 계정 고르는 순서 기준으로 쌓아둔다
    alerted = _load_alerted()
    hits = []
    for kind, pct_key, reset_key in (("wk", "weekly_pct", "weekly_reset"),
                                     ("se", "session_pct", "session_reset")):
        hit = _check(kind, u.get(pct_key), u.get(reset_key), label, alerted)
        if hit:
            hits.append(hit)

    if hits:
        notify.send(build_message(label, hits, _others_weekly(label, seen)))
        _save_alerted(alerted)
        print(f"알림 보냄: {label} " + ", ".join(f"{_KIND_NAME[k]} {r:.0f}% 남음" for k, r, _ in hits))
    else:
        _save_alerted(alerted)  # 마킹 갱신은 저장
        print(f"새 단계 없음: {label} (주간 {u.get('weekly_pct')}%, 세션 {u.get('session_pct')}%)")
    _fill_missing_resets(state, label, seen)
    st.remember_resets(seen)
    return 0


def _fill_missing_resets(state: dict, label: str, seen: list) -> None:
    """순서 기준(주간 초기화 시각)이 비어 있는 계정만 채운다.

    이게 비어 있으면 그 계정은 뒤로 밀려 순서가 틀어진다. 봇 머신은 사용량을 읽을 일이
    적어서 저절로 안 쌓인다. 다만 매번 다 조회하면 429 가 나므로 없는 것만 읽는다.
    초기화가 지나면 그 값이 과거가 되어 자동으로 다시 채우게 된다.
    """
    import time
    now = time.time()
    resets = state.get("weekly_reset") or {}
    already = {l for l, _ in seen}
    for l in st.account_labels():
        if l == label or l in already:
            continue
        try:
            if float(resets.get(l)) > now:
                continue  # 이미 쓸 만한 값이 있다
        except (TypeError, ValueError):
            pass
        cred = BASE / "creds" / f"{l}.json"
        if not cred.exists():
            continue
        u = usage_check.get_usage(cred)
        if u and u.get("weekly_reset"):
            seen.append((l, u["weekly_reset"]))


if __name__ == "__main__":
    raise SystemExit(main())
