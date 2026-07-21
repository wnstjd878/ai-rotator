#!/bin/bash
# == 운영 맥락 ==
# 용도: claude 실행 파일 자리(/opt/homebrew/bin/claude)에 순환 가로채기를 설치·해제한다.
#       설치하면 봇들의 단발 호출이 전부 계정 순환 보호를 받는다 (봇 코드는 안 고침).
# 실행: bash install_shim.sh              설치
#       bash install_shim.sh --uninstall  원래대로 되돌림
#       bash install_shim.sh --status     지금 어느 쪽이 걸려 있는지
# 되돌리기: 언제든 --uninstall 로 원래 심볼릭 링크를 복원한다.
#       Claude Code 를 npm 으로 업데이트하면 링크가 원래대로 돌아갈 수 있다(그래도 봇은 정상 동작).
#       업데이트 후 순환이 필요하면 이 스크립트를 다시 실행한다.
# 마지막 점검: 2026-07-22
set -euo pipefail

LINK="/opt/homebrew/bin/claude"
REAL="/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHIM="$DIR/claude_shim.sh"
ORIGINAL="$DIR/.original_claude_link"   # 설치 전 연결 대상을 적어둔다(되돌리기용)

status() {
  if [ -L "$LINK" ]; then
    echo "지금 연결: $(readlink "$LINK")"
  elif [ -f "$LINK" ]; then
    echo "지금 연결: 일반 파일 ($LINK)"
  else
    echo "$LINK 이 없습니다."
    return 1
  fi
  if [ "$(readlink "$LINK" 2>/dev/null || true)" = "$SHIM" ]; then
    echo "상태: 순환 가로채기 설치됨"
  else
    echo "상태: 원래 claude (가로채기 없음)"
  fi
}

uninstall() {
  # 설치할 때 적어둔 원래 연결 대상으로 돌린다. 기록이 없을 때만 알려진 기본 경로를 쓴다.
  local target=""
  if [ -f "$ORIGINAL" ]; then
    target="$(cat "$ORIGINAL")"
  fi
  if [ -z "$target" ] || [ "$target" = "$SHIM" ]; then
    target="../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
  fi
  ln -sfn "$target" "$LINK"
  if ! "$LINK" --version >/dev/null 2>&1; then
    echo "되돌린 뒤 claude 가 실행되지 않습니다. 연결 대상을 확인하세요: $target" >&2
    status
    exit 1
  fi
  echo "원래대로 되돌렸습니다."
  status
}

install() {
  if [ ! -f "$REAL" ]; then
    echo "진짜 claude 를 못 찾았습니다: $REAL" >&2
    exit 1
  fi
  cat > "$SHIM" <<EOF
#!/bin/bash
# 자동 생성됨 (install_shim.sh). 직접 고치지 말 것.
exec /usr/bin/env python3 "$DIR/claude_shim.py" "\$@"
EOF
  chmod +x "$SHIM"

  # 설치 전에 실제로 동작하는지 확인한다. 실패하면 설치하지 않는다.
  if ! CLAUDE_REAL_BIN="$REAL" "$SHIM" --version >/dev/null 2>&1; then
    echo "가로채기 시험 실행에 실패해 설치를 중단합니다." >&2
    exit 1
  fi

  # 되돌릴 자리를 먼저 적어둔다. 이미 가로채기가 걸려 있으면 덮어쓰지 않는다.
  if [ -L "$LINK" ]; then
    cur="$(readlink "$LINK")"
    if [ "$cur" != "$SHIM" ]; then
      printf '%s' "$cur" > "$ORIGINAL"
    fi
  fi

  ln -sfn "$SHIM" "$LINK"
  if ! "$LINK" --version >/dev/null 2>&1; then
    echo "설치 후 claude 실행에 실패해 되돌립니다." >&2
    uninstall
    exit 1
  fi
  echo "설치했습니다. 봇들의 단발 호출이 이제 계정 순환 보호를 받습니다."
  status
}

case "${1:-}" in
  --uninstall) uninstall ;;
  --status) status ;;
  "") install ;;
  *) echo "사용법: bash install_shim.sh [--uninstall|--status]"; exit 1 ;;
esac
