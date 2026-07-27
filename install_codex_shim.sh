#!/bin/bash
# == 운영 맥락 ==
# 용도: codex 실행 파일 자리(/opt/homebrew/bin/codex)에 순환 가로채기를 설치·해제한다.
#       설치하면 자동 호출(codex exec / codex review)이 전부 계정 순환 보호를 받는다.
#       대화형 codex 는 그대로 통과한다.
# 실행: bash install_codex_shim.sh              설치
#       bash install_codex_shim.sh --uninstall  원래대로 되돌림
#       bash install_codex_shim.sh --status     지금 어느 쪽이 걸려 있는지
# 전제: 그 머신에서 계정별로 codex login 후 codex_save.py 로 저장돼 있어야 실제로 순환한다.
#       저장본이 2개 미만이면 가로채기는 아무것도 안 하고 통과만 시킨다(무해).
# 되돌리기: 언제든 --uninstall. npm 으로 codex 를 업데이트하면 링크가 원래대로 돌아갈 수
#       있다(그래도 codex 는 정상 동작, 순환만 꺼짐). 업데이트 후 이 스크립트를 다시 실행.
# 마지막 점검: 2026-07-28
set -euo pipefail

LINK="/opt/homebrew/bin/codex"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHIM="$DIR/codex_shim.sh"
ORIGINAL="$DIR/.original_codex_link"   # 설치 전 연결 대상을 적어둔다(되돌리기용)

find_real() {
  for c in /opt/homebrew/lib/node_modules/@openai/codex/bin/codex.js \
           /usr/local/lib/node_modules/@openai/codex/bin/codex.js; do
    [ -f "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}

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
    echo "상태: 원래 codex (가로채기 없음)"
  fi
}

uninstall() {
  local target=""
  [ -f "$ORIGINAL" ] && target="$(cat "$ORIGINAL")"
  if [ -z "$target" ] || [ "$target" = "$SHIM" ]; then
    target="../lib/node_modules/@openai/codex/bin/codex.js"
  fi
  ln -sfn "$target" "$LINK"
  if ! "$LINK" --version >/dev/null 2>&1; then
    echo "되돌린 뒤 codex 가 실행되지 않습니다. 연결 대상을 확인하세요: $target" >&2
    status
    exit 1
  fi
  echo "원래대로 되돌렸습니다."
  status
}

install() {
  REAL="$(find_real)" || { echo "진짜 codex 를 못 찾았습니다." >&2; exit 1; }
  cat > "$SHIM" <<EOF
#!/bin/bash
# 자동 생성됨 (install_codex_shim.sh). 직접 고치지 말 것.
exec /usr/bin/env python3 "$DIR/codex_shim.py" "\$@"
EOF
  chmod +x "$SHIM"

  # 설치 전에 실제로 동작하는지 확인한다. 실패하면 설치하지 않는다.
  if ! CODEX_REAL_BIN="$REAL" "$SHIM" --version >/dev/null 2>&1; then
    echo "가로채기 시험 실행에 실패해 설치를 중단합니다." >&2
    exit 1
  fi

  # 되돌릴 자리를 먼저 적어둔다. 이미 가로채기가 걸려 있으면 덮어쓰지 않는다.
  if [ -L "$LINK" ]; then
    cur="$(readlink "$LINK")"
    [ "$cur" != "$SHIM" ] && printf '%s' "$cur" > "$ORIGINAL"
  fi

  ln -sfn "$SHIM" "$LINK"
  if ! "$LINK" --version >/dev/null 2>&1; then
    echo "설치 후 codex 실행에 실패해 되돌립니다." >&2
    uninstall
    exit 1
  fi
  echo "설치했습니다. codex exec / codex review 가 이제 계정 순환 보호를 받습니다."
  status
}

case "${1:-}" in
  --uninstall) uninstall ;;
  --status) status ;;
  "") install ;;
  *) echo "사용법: bash install_codex_shim.sh [--uninstall|--status]"; exit 1 ;;
esac
