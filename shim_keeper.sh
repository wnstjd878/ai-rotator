#!/bin/bash
# == 운영 맥락 ==
# 용도: 클로드 CLI 가 업데이트되면 nvm 폴더의 claude 연결이 원본으로 되돌아가 계정 순환을
#       건너뛴다. 그 상태를 주기적으로 확인해서 풀려 있으면 가로채기를 다시 건다.
# 실행 시점: launchd com.user.claude-shim-keeper, 1시간 주기 + 부팅 시 (RunAtLoad)
# 입력: install_shim.sh --status 의 출력
# 출력: ~/.claude/claude-shim-keeper.log (다시 걸었을 때만 기록)
# 외부 의존: install_shim.sh, claude_shim.sh
# 의도적 미구현: 텔레그램 알림 (조용한 자가 치유라 매번 알리면 소음. 반복 재설치는 로그로 확인)
#              계정 전환 자체 (그건 claude_pool.py 담당 — 재구현 금지)
# 되돌리기: launchctl bootout gui/$UID/com.user.claude-shim-keeper 후 plist 삭제
# 마지막 점검: 2026-08-22
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$DIR/install_shim.sh"
LOG="$HOME/.claude/claude-shim-keeper.log"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG"; }

status_out="$(bash "$INSTALLER" --status 2>&1)"

# 두 자리 중 하나라도 풀려 있으면 다시 건다.
if printf '%s' "$status_out" | grep -q "안 덮임" \
   || printf '%s' "$status_out" | grep -q "상태: 원래 claude"; then
  log "가로채기가 풀려 있어 다시 겁니다."
  log "  풀린 상태: $(printf '%s' "$status_out" | tr '\n' ' | ')"
  if install_out="$(bash "$INSTALLER" 2>&1)"; then
    log "  다시 걸었습니다: $(printf '%s' "$install_out" | tr '\n' ' | ')"
  else
    log "  다시 걸기 실패: $(printf '%s' "$install_out" | tr '\n' ' | ')"
    exit 1
  fi
fi

exit 0
