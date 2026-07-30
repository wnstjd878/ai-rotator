"""
== 운영 맥락 ==
용도: 지금 쓰는 지피티 계정의 사용률을 주기적으로 보고, 주간/세션 한도가
      50% / 70% / 90% 를 넘을 때마다 텔레그램으로 남은량을 알린다.
      알림만 한다 — 사용률을 보고 미리 계정을 바꾸지는 않는다(2026-07-28 사용자 결정).
      계정 교체는 codex_pool 이 거절을 받은 뒤에만 한다.
실행: python codex_alert.py            (정기 실행: 10분마다)
      python codex_alert.py --preview  → 지금 값으로 알림 문구만 출력(전송·기록 안 함)
      python codex_alert.py --refresh-order → 계정 순서 기준만 갱신(알림 없음)
동작: 현재 계정(codex_state.current, 없으면 순차상 다음)의 사용량을 읽어 넘은 단계를
      그 초기화 구간에 한 번씩만 알린다(스팸 방지). 초기화되면 다시 무장된다.
      한 번에 여러 단계를 넘었으면 가장 높은 단계 하나만 알린다.
      Pro 요금제는 주간 창만 오므로 세션 줄은 저절로 빠진다.
상태: codex_alert_state.json 에 "이미 알린 (계정, 종류, 초기화구간, 단계)" 를 기록.
규칙은 클로드 쪽 usage_alert.py 와 같다(문구·단계·억제 방식). 상태와 자격만 따로 쓴다.
마지막 점검: 2026-07-28
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import pool_state as st  # noqa: E402

st.use("codex")

import codex_home  # noqa: E402
import codex_usage  # noqa: E402
import notify  # noqa: E402

st.SAVED_LABELS_FN = codex_home.logged_in_labels

ALERT_STATE = BASE / "codex_alert_state.json"
THRESHOLDS = [50, 70, 90]  # 넘을 때마다 알리는 단계
SWITCH_SOON = 10           # 남은량이 이 % 이하면 첫 줄에 다음 계정을 붙인다
HISTORY_KEEP = 80
PROVIDER = "codex"         # 공유 칠판에서 클로드 값과 갈라 쓰는 이름(codex_pool 과 같아야 한다)
FULL_SHARE_EVERY = 3600    # 이 간격으로 계정 전부를 훑어 공유 칠판에 올린다
_KIND_NAME = {"wk": "주간", "se": "세션"}

_pct = codex_usage.pct_text
_fmt_reset_full = codex_usage.fmt_reset_full


def _cred(label: str) -> Path:
    """그 계정의 자격 파일. 계정마다 codex 폴더가 따로다."""
    return codex_home.auth_path(label)


def _share_usage(label: str, usage: dict | None) -> None:
    """읽은 남은량을 공유 칠판에 올린다. 실패해도 조용히 넘어간다(표시용이라 없어도 된다).

    **Mac 은 사용량 조회가 앞단에서 막혀(HTML 응답) 자기 계정도 못 읽는다.** 여기서
    올려두면 Mac 화면에서도 계정별 남은량이 보인다. 계정을 고르거나 막는 판단에는 안 쓴다.
    """
    if not usage:
        return
    try:
        import shared_state

        if shared_state.enabled():
            shared_state.put_usage(label, usage, provider=PROVIDER)
    except Exception:
        pass


def _share_current(label: str | None) -> None:
    """이 기계가 지금 물려 있는 계정을 공유 칠판에 적는다. 실패해도 조용히 넘어간다."""
    if not label:
        return
    try:
        import shared_state

        if shared_state.enabled():
            shared_state.put_current(label, provider=PROVIDER)
    except Exception:
        pass


def _share_all_if_due(alerted: dict, label: str) -> None:
    """가끔 계정 전부의 남은량을 공유 칠판에 올린다.

    평소엔 지금 쓰는 계정만 올린다. 세 기계가 서로 다른 계정에 앉아 있으면 그것만으로
    칠판이 채워지지만, 다 같은 계정에 몰려 있으면 나머지 계정이 영영 안 올라간다.
    그렇다고 10분마다 전부 조회하면 429 가 난다 — 한 시간에 한 번만 훑는다.
    """
    now = time.time()
    try:
        last = float(alerted.get("_shared_all_at") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if now - last < FULL_SHARE_EVERY:
        return
    for l in st.account_labels():
        if l == label or not _cred(l).exists():
            continue
        _share_usage(l, codex_usage.get_usage(_cred(l)))
    alerted["_shared_all_at"] = now  # 맨 뒤에 넣어야 기록을 줄일 때 안 잘린다


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
    """넘은 단계 중 아직 안 알린 게 있으면 (종류, 남은%, 초기화). 넘은 단계는 모두 마킹."""
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
    """다른 계정들의 주간 남은량. 라벨 순서 그대로 [(라벨, 남은%)]."""
    out = []
    for l in st.account_labels():
        if l == label or not _cred(l).exists():
            continue
        u = codex_usage.get_usage(_cred(l))
        if not u or u.get("weekly_pct") is None:
            continue
        if seen is not None:
            seen.append((l, u.get("weekly_reset")))
        out.append((l, max(0.0, 100 - float(u["weekly_pct"]))))
    return out


def _next_label(label: str, others: list, state: dict | None = None):
    """이 계정이 막히면 순환기가 다음에 고를 계정. 순서 규칙은 pool_state 한 곳에서 온다."""
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
        hits = [wk]  # 주간이 다 차면 세션이 얼마 남았든 못 쓴다
    worst = min(hits, key=lambda h: h[1])
    head = f"[지피티] 계정 {label} {_KIND_NAME[worst[0]]} {_pct(worst[1])} 남음"
    if worst[1] <= SWITCH_SOON:
        if nxt:
            head += f", 한도에 걸리면 계정 {nxt}로 자동 전환"
        elif others:  # 목록이 비면 사용량을 못 읽은 것 — 없다고 단정하지 않는다
            head += ", 전환할 계정 없음"

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
    """알림 판정 없이 계정 순서 기준(주간 초기화 시각)만 갱신한다."""
    state = st.prune_blocked(st.load_state())
    cur = state.get("current")
    seen = []
    for l in st.account_labels():
        if not _cred(l).exists():
            continue
        u = codex_usage.get_usage_for(_cred(l), is_current=(l == cur))
        if u and u.get("weekly_reset"):
            seen.append((l, u["weekly_reset"]))
    if not seen:
        print(f"순서 갱신 실패: 사용량을 못 읽음 ({codex_usage.last_error or '사유 모름'})")
        return 1
    st.remember_resets(seen)
    print("순서 갱신: " + " → ".join(st.priority_order(st.prune_blocked(st.load_state()))))
    return 0


def _fill_missing_resets(state: dict, label: str, seen: list) -> None:
    """순서 기준(주간 초기화 시각)이 비어 있는 계정만 채운다. 매번 다 조회하면 429 가 난다."""
    now = time.time()
    resets = state.get("weekly_reset") or {}
    already = {l for l, _ in seen}
    for l in st.account_labels():
        if l == label or l in already or not _cred(l).exists():
            continue
        try:
            if float(resets.get(l)) > now:
                continue  # 이미 쓸 만한 값이 있다
        except (TypeError, ValueError):
            pass
        u = codex_usage.get_usage(_cred(l))
        if u and u.get("weekly_reset"):
            seen.append((l, u["weekly_reset"]))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if "--refresh-order" in sys.argv:
        return refresh_order()
    preview = "--preview" in sys.argv
    state = st.prune_blocked(st.load_state())
    label = state.get("current") or st.pick_next(state)
    if not label or not _cred(label).exists():
        return 0
    u = codex_usage.get_usage_for(_cred(label), is_current=True)
    if not u:
        # 조용히 넘어가면 경고가 통째로 멈춘 걸 아무도 모른다. 실행 기록에는 남긴다.
        print(f"사용량 못 읽음: {label} ({codex_usage.last_error or '사유 모름'})")
        return 0

    kinds = (("wk", "weekly_pct", "weekly_reset"), ("se", "session_pct", "session_reset"))

    if preview:  # 단계를 안 넘었어도 지금 값으로 문구를 만들어 보여준다
        hits = [(k, max(0.0, 100 - float(u[p])), u[r]) for k, p, r in kinds if u.get(p) is not None]
        if not hits:
            print("사용량 못 읽음")
            return 0
        print(build_message(label, hits, _others_weekly(label)))
        return 0

    # 다른 기계가 보라고 공유 칠판에 올린다(알림 여부와 무관하게 매번).
    _share_current(label)
    _share_usage(label, u)

    seen = [(label, u.get("weekly_reset"))]
    alerted = _load_alerted()
    _share_all_if_due(alerted, label)  # 한 시간에 한 번은 나머지 계정도 올린다
    hits = []
    for kind, pct_key, reset_key in kinds:
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


if __name__ == "__main__":
    raise SystemExit(main())
