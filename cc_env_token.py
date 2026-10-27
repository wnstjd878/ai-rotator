"""
== 운영 맥락 ==
용도: 맥 '환경변수 열쇠 방식'(creds/.env-token-mode) 에서 사람 창(ccauto.sh = cc)이 켜는 claude 에
      지금 계정의 접근 열쇠를 넘긴다. 봇 단발 호출은 claude_shim.py 가 같은 일을 이미 한다.
왜: 2026-10-25 이 방식으로 바꾼 뒤 순환기는 열쇠꾸러미·~/.claude/.credentials.json 을 더 갱신하지 않는다.
    그런데 ccauto.sh 는 열쇠를 안 넘겨서, 화면 세션의 cc(오르카 터미널 포함)가 낡은 열쇠꾸러미 자격을 읽고
    "Login expired · Please run /login" 으로 멈췄다(2026-10-27 오르카 폰 새 탭에서 발견).
호출: tok="$(/usr/bin/python3 cc_env_token.py)" — 열쇠는 출력 통로로만 흐른다. 화면·기록에 찍지 말 것.
      종료 코드 0 = 열쇠 출력, 2 = 이 방식이 꺼져 있음(아무것도 안 넘김), 1 = 못 받음(사유는 오류 통로).
사람 창은 몇 시간씩 돌아서 봇 기준(10분 남으면 갱신)으로 넘기면 곧 만료된다. 그래서 6시간보다 덜 남았으면
켜기 전에 새로 받는다. 갱신은 cred_refresh 의 잠금을 거쳐 한 번만 일어난다.
의도적 미구현: 창이 도는 중에 열쇠가 만료되면 다시 넣어 주기. claude 는 환경변수 열쇠를 스스로 갱신하지
      못하므로 그때는 창을 끝내면 ccauto.sh 가 새 열쇠로 다시 켠다.
마지막 점검: 2026-10-27
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_pool as cp  # noqa: E402
import pool_state as st  # noqa: E402

INTERACTIVE_MIN_LEFT = 6 * 3600

if not cp.env_token_mode():
    sys.exit(2)
cp.ENV_TOKEN_MIN_LEFT = INTERACTIVE_MIN_LEFT
label = st.load_state().get("current")
tok, why = cp._env_token_for(label)
if not tok:
    sys.stderr.write(f"[cc] '{label}' 계정 열쇠를 못 받았습니다({why}). cs 로 상태를 보세요.\n")
    sys.exit(1)
sys.stdout.write(tok)
