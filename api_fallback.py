"""
== 운영 맥락 ==
용도: 구독 계정이 전부 한도에 걸렸을 때, 같은 요청을 Anthropic API 로 대신 보낸다.
      봇 입장에서는 claude CLI 가 답한 것과 똑같이 보이게 출력 형식을 맞춘다.
호출: claude_shim.py 가 AllAccountsExhausted 를 잡았을 때만. 평소에는 아무 일도 안 한다.
왜 있나: 2026-07-27 오후, 구독 4개(A~D)가 전부 한도에 걸려 회원 질문 12건이 한 시간 동안
      답을 못 받았다(이세진·조동형 님). 구독이 다 떨어져도 서비스는 굴러가야 한다.
입력: CLI 인자(모델·출력형식·스키마)와 프롬프트
출력: CLI 와 같은 모양. --output-format json 이면 {"type":"result","result":...,"usage":...},
      아니면 답변 텍스트만.
외부 의존: api.anthropic.com. 키는 아래 순서로 찾는다(값은 로그에 절대 남기지 않는다).
      ①환경변수 CLAUDE_API_KEY/ANTHROPIC_API_KEY ②~/.config/ai-rotator/env
      ③~/projects/main-agent/.env (봇 공용키)
의도적 미구현: 도구 사용(--allowedTools), 대화 이어가기(--resume). API 대체는 단발 답변만
      맡는다. 그 기능이 필요한 호출은 구독이 풀릴 때까지 기다리는 게 맞다.
안전장치: 하루 사용 상한(기본 5달러)을 넘으면 대체하지 않는다 — 구독이 오래 막혔을 때
      요금이 무한정 늘지 않게. 사용액은 usage.json 에 누적한다.
마지막 점검: 2026-07-27
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
USAGE_PATH = os.path.join(HERE, "api_usage.json")
LOG_PATH = os.path.join(HERE, "rotator.log")

DAILY_USD_CAP = float(os.environ.get("API_FALLBACK_DAILY_USD", "5"))
DEFAULT_MODEL = os.environ.get("API_FALLBACK_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.environ.get("API_FALLBACK_MAX_TOKENS", "8000"))

# 100만 토큰당 달러. 사용액을 어림해 상한을 지키려는 것이지 청구서가 아니다.
PRICE = {
    "opus": (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku": (0.8, 4.0),
}


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " [API 대체] " + msg
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _read_env_file(path: str, keys: tuple[str, ...]) -> str | None:
    """파일에서 키 값을 읽는다. 값은 돌려주기만 하고 어디에도 기록하지 않는다."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() in keys:
                    v = v.strip().strip('"').strip("'")
                    if v:
                        return v
    except Exception:
        pass
    return None


def find_api_key() -> str | None:
    keys = ("CLAUDE_API_KEY", "ANTHROPIC_API_KEY")
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v
    for path in (os.path.expanduser("~/.config/ai-rotator/env"),
                 os.path.expanduser("~/projects/main-agent/.env"),
                 os.path.expanduser("~/projects/mkt-agent/.env")):
        v = _read_env_file(path, keys)
        if v:
            return v
    return None


def _usage_today() -> float:
    today = time.strftime("%Y-%m-%d")
    try:
        with open(USAGE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get(today) or 0)
    except Exception:
        return 0.0


def _add_usage(usd: float) -> float:
    today = time.strftime("%Y-%m-%d")
    data = {}
    try:
        with open(USAGE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data[today] = round(float(data.get(today) or 0) + usd, 6)
    # 지난 30일치만 남긴다
    for k in sorted(data)[:-30]:
        data.pop(k, None)
    try:
        tmp = USAGE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, USAGE_PATH)
    except Exception:
        pass
    return data[today]



# 검사와 누적이 떨어져 있으면 동시에 넘어온 호출이 전부 통과한다(2026-07-29: 상한 5달러인데
# 64달러가 나갔다). 잠금으로 묶고, 부르기 전에 예상 비용을 미리 달아둔다.
LOCK_PATH = os.path.join(HERE, ".api_fallback.lock")
RESERVE_USD = float(os.environ.get("API_FALLBACK_RESERVE_USD", "0.5"))


def _reserve_usage(cap: float):
    """상한 안이면 예상 비용을 미리 달고 True. 넘었으면 (False, 지금 사용액)."""
    import fcntl
    with open(LOCK_PATH, "a+") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX)
        except Exception:
            pass
        used = _usage_today()
        if used >= cap:
            return False, used
        _add_usage(RESERVE_USD)      # 선반영 — 뒤따라온 호출은 이 값을 보고 멈춘다
        return True, used


def _settle_usage(actual_usd: float):
    """실제 비용으로 보정한다(미리 단 값을 빼고 실제를 더한다)."""
    import fcntl
    with open(LOCK_PATH, "a+") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX)
        except Exception:
            pass
        return _add_usage(actual_usd - RESERVE_USD)


def _price_of(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    for key, pair in PRICE.items():
        if key in m:
            return pair
    return PRICE["sonnet"]


def parse_cli_args(argv: list[str]) -> dict:
    """CLI 인자에서 API 호출에 필요한 것만 뽑는다."""
    out = {"model": None, "output_format": "text", "json_schema": None,
           "system": None, "prompt_arg": None, "unsupported": []}
    i = 0
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if a == "--model" and nxt:
            out["model"] = nxt; i += 2; continue
        if a == "--output-format" and nxt:
            out["output_format"] = nxt; i += 2; continue
        if a == "--json-schema" and nxt:
            try:
                out["json_schema"] = json.loads(nxt)
            except Exception:
                out["json_schema"] = None
            i += 2; continue
        if a in ("--system-prompt", "--append-system-prompt") and nxt:
            out["system"] = ((out["system"] or "") + "\n" + nxt).strip(); i += 2; continue
        if a in ("--allowedTools", "--allowed-tools", "--tools", "--mcp-config", "--resume"):
            out["unsupported"].append(a); i += 2; continue
        if a in ("-p", "--print"):
            i += 1; continue
        if a.startswith("-"):
            i += 2 if nxt and not nxt.startswith("-") else 1
            continue
        out["prompt_arg"] = a
        i += 1
    return out


def _post(key: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json",
                 "x-api-key": key,
                 "anthropic-version": "2023-06-01"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def run(argv: list[str], prompt: str | None, timeout: int = 180) -> tuple[int, str, str]:
    """구독 대신 API 로 한 번 답한다. (종료코드, 표준출력, 오류출력)."""
    key = find_api_key()
    if not key:
        return 1, "", "[API 대체] 키를 못 찾았습니다(CLAUDE_API_KEY).\n"

    opts = parse_cli_args(argv)
    if opts["unsupported"]:
        return 1, "", f"[API 대체] 지원하지 않는 기능: {', '.join(opts['unsupported'])}\n"

    text = prompt if prompt is not None else (opts["prompt_arg"] or "")
    if not text.strip():
        return 1, "", "[API 대체] 보낼 내용이 없습니다.\n"

    ok, used = _reserve_usage(DAILY_USD_CAP)
    if not ok:
        return 1, "", (f"[API 대체] 오늘 사용액이 상한({DAILY_USD_CAP}달러)에 닿아 "
                       f"대체하지 않습니다(현재 {used:.2f}달러).\n")

    model = opts["model"] or DEFAULT_MODEL
    system = opts["system"]
    if opts["json_schema"]:
        # CLI 의 --json-schema 와 같은 결과를 얻으려면 형식을 말로 지시한다.
        # (API 쪽 구조화 출력 규격이 판마다 달라, 어느 판에서나 되는 방식을 쓴다.)
        hint = ("반드시 아래 JSON 형식 하나만 출력한다. 설명·코드블록 표시 없이 JSON 만.\n"
                + json.dumps(opts["json_schema"], ensure_ascii=False))
        system = (system + "\n\n" + hint) if system else hint

    body = {"model": model, "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": text}]}
    if system:
        body["system"] = system

    try:
        data = _post(key, body, timeout)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        log(f"호출 실패 {e.code}")
        return 1, "", f"[API 대체] 호출 실패({e.code}) {detail}\n"
    except Exception as e:
        log(f"호출 실패 {type(e).__name__}")
        return 1, "", f"[API 대체] 호출 실패: {type(e).__name__}\n"

    answer = "".join(b.get("text") or "" for b in (data.get("content") or [])
                     if b.get("type") == "text").strip()
    u = data.get("usage") or {}
    in_tok = int(u.get("input_tokens") or 0)
    out_tok = int(u.get("output_tokens") or 0)
    p_in, p_out = _price_of(model)
    cost = in_tok / 1_000_000 * p_in + out_tok / 1_000_000 * p_out
    total = _settle_usage(cost)
    log(f"구독 대신 API 로 답했습니다. 모델 {model}, "
        f"들어간 토큰 {in_tok}, 나온 토큰 {out_tok}, 이번 {cost:.4f}달러, 오늘 누적 {total:.2f}달러")

    if opts["output_format"] != "json":
        return 0, answer + "\n", ""

    # CLI 가 뱉는 것과 같은 모양으로 감싼다. 봇들이 result·usage 를 읽는다.
    structured = None
    if opts["json_schema"]:
        try:
            s = answer.strip()
            if s.startswith("```"):
                s = s.split("```", 2)[1]
                s = s.split("\n", 1)[1] if "\n" in s else s
            structured = json.loads(s[s.find("{"):s.rfind("}") + 1])
        except Exception:
            structured = None
    out = {"type": "result", "subtype": "success", "is_error": False,
           "result": answer, "session_id": "", "total_cost_usd": round(cost, 6),
           "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
           "served_by": "api_fallback"}
    if structured is not None:
        out["structured_output"] = structured
    return 0, json.dumps(out, ensure_ascii=False) + "\n", ""
