#!/bin/bash
# == 운영 맥락 ==
# 용도: 클로드 대화형 창을 켜고, 한도로 끝나면 다음 계정으로 갈아끼워 자동으로 다시 켠다.
#       윈도우 PowerShell 의 ccauto.ps1 과 같은 일을 맥에서 한다(Orca 원격 터미널 포함).
# 실행: cc                                   (.zshrc 에 등록된 짧은 명령)
#       bash ~/projects/ai-rotator/ccauto.sh [claude 인자...]
# 설치: ~/.zshrc 는 이 저장소 밖이라 백업이 안 된다. 맥을 새로 깔면 아래 세 줄을 다시 넣는다.
#       cc() { bash "$HOME/projects/ai-rotator/ccauto.sh" "$@"; }
#       cn() { /usr/bin/python3 "$HOME/projects/ai-rotator/claude_pool.py" --next && claude; }
#       cs() { /usr/bin/python3 "$HOME/projects/ai-rotator/claude_pool.py" --status; }
# 외부 의존: claude_pool.py (--ensure / --status / --next), 진짜 claude 실행 파일
# 의도적 미구현: 봇 단발 호출(-p) 순환은 claude_shim.py 가 맡는다. 여기는 사람 창 전용.
#              (두 곳에서 같이 순환시키면 계정이 이중으로 튄다 — 재구현 금지)
# 마지막 점검: 2026-10-27 (맥 환경변수 열쇠 방식이면 cc_env_token.py 로 열쇠를 넘김)

POOL="$HOME/projects/ai-rotator/claude_pool.py"
PY=/usr/bin/python3

CLAUDE_BIN="$(command -v claude || true)"
if [ -z "$CLAUDE_BIN" ]; then
  echo "claude 실행 파일을 못 찾았습니다. PATH 를 확인하세요." >&2
  exit 1
fi

fails=0
# 첫 실행은 사용자가 준 인자 그대로. 한도로 갈아탄 뒤 다시 켤 때만 --continue 를 붙여
# 직전 대화를 이어받는다. (처음부터 --continue 를 박으면 새 대화를 못 연다)
run_args=("$@")

# 이 창이 쓰는 모델 칸을 표시해 둔다(2026-08-22). 봇 단발 호출이 계정을 갈아끼울 때
# 이 칸이 막힌 계정으로는 안 넘어간다. 가로채기(claude_shim)도 같은 표시를 남기지만,
# 가로채기가 벗겨진 사이에 켜진 창까지 지키려고 여기서 한 번 더 남긴다.
# 창이 끝나면 지운다. 못 지워도 이 프로세스가 죽으면 순환기가 알아서 걷어낸다.
trap '"$PY" "$POOL" --unclaim "$$" >/dev/null 2>&1' EXIT
# 창 안에서 도는 상태줄이 이 번호의 표시를 갱신한다(/model 로 모델을 바꿨을 때).
export CLAUDE_ROTATOR_CLAIM_PID="$$"

while true; do
  # 켜기 직전 확인. 지금 계정이 이미 다 찼으면 켜기 전에 갈아끼운다.
  # 인자를 같이 넘긴다 — 그래야 --model 을 보고 그 모델 칸(예: Fable)까지 확인한다.
  # 인자에 없으면 순환기가 settings.json 에 고정된 모델을 대신 본다.
  "$PY" "$POOL" --ensure "${run_args[@]}"
  # 계정이 정해진 뒤에 이 창의 모델 칸을 표시한다(같은 번호로 다시 부르면 갱신된다).
  "$PY" "$POOL" --claim "$$" "${run_args[@]}"
  # 켜기 직전 계정별 사용량을 화면에 먼저 보여준다. claude 안에서는 이 값을 볼 수 없다.
  echo ""
  echo "[계정 사용량]"
  # 여기도 인자를 넘긴다 — 쓰려는 모델 칸이 전 계정에서 바닥이면 그 한 줄을 같이 찍는다.
  AIROTATOR_COLOR=1 "$PY" "$POOL" --status "${run_args[@]}"
  echo ""

  # 맥 환경변수 열쇠 방식이면 지금 계정 열쇠를 넘긴다(2026-10-27). 안 넘기면 화면 세션의 claude 가
  # 순환기가 더는 갱신하지 않는 열쇠꾸러미 자격을 읽어 "Login expired" 로 멈춘다. 값은 화면에 안 찍는다.
  # 계정을 갈아탄 뒤 다시 켤 때도 새 계정 열쇠로 바꿔야 해서 반복마다 새로 받는다.
  set_cc_token() {
    unset CLAUDE_CODE_OAUTH_TOKEN
    [ -n "${CLAUDE_CONFIG_DIR:-}" ] && return 0
    local tok
    tok="$("$PY" "$HOME/projects/ai-rotator/cc_env_token.py")" && [ -n "$tok" ] && export CLAUDE_CODE_OAUTH_TOKEN="$tok"
    return 0
  }
  set_cc_token

  "$CLAUDE_BIN" "${run_args[@]}"

  # claude 가 끝났다. 지금 계정이 한도/만료인지 조용히 확인(haiku 한 번, 소비 미미).
  # 가로채기(claude_shim)가 이 확인을 대신 처리하면 판정이 흐려지므로 꺼두고 부른다.
  # 확인 전에 열쇠를 다시 받는다. 창이 오래 돌아 넘겨 둔 열쇠가 만료됐으면 그것만으로 '만료'로 오판한다.
  set_cc_token
  probe="$(echo "ping" | CLAUDE_ROTATOR_DISABLED=1 "$CLAUDE_BIN" -p --model claude-haiku-4-5-20251001 --output-format json 2>&1)"

  # "reached your ..." 는 모델별 한도 문구다(실제: "You've reached your Fable 5 limit").
  # 이게 없어서 2026-08-12 에 Fable 이 막혔는데도 자동 전환이 안 걸렸다.
  limited=0
  switched_already=0
  if printf '%s' "$probe" | grep -qiE "weekly limit|usage limit reached|hit your .{0,40}limit|reached your .{0,40}limit|OAuth session expired|5-hour limit|rate.?limit"; then
    limited=1
  else
    # haiku 로 찔러보면 Fable 칸만 바닥난 경우는 안 잡힌다(그 칸은 haiku 가 안 쓴다).
    # 그래서 사용량 조회(--ensure)를 한 번 더 한다. 종료코드 3 = 그 칸이 바닥나 이미 갈아탔음.
    "$PY" "$POOL" --ensure "${run_args[@]}"
    if [ "$?" -eq 3 ]; then limited=1; switched_already=1; fi
  fi

  if [ "$limited" -eq 1 ]; then
    echo ""
    echo "[자동순환] 한도 도달 감지 -> 다음 계정으로 전환합니다..."
    if [ "$switched_already" -eq 0 ]; then "$PY" "$POOL" --next "${run_args[@]}"; fi

    has_resume=0
    for a in "${run_args[@]}"; do
      case "$a" in
        --continue|-c|--resume|-r) has_resume=1 ;;
      esac
    done
    if [ "$has_resume" -eq 0 ]; then
      run_args=("--continue" "${run_args[@]}")
      echo "[자동순환] 직전 대화를 이어받아 다시 켭니다 (--continue)."
    fi
    echo "[자동순환] 새 계정으로 claude 를 다시 시작합니다."
    echo ""
    sleep 0.4
    fails=$((fails + 1))
    if [ "$fails" -ge 8 ]; then
      echo "[자동순환] 전환이 너무 잦습니다. 계정 상태를 확인하세요 (cs)."
      break
    fi
  else
    echo "[자동순환] 정상 종료."
    break
  fi
done
