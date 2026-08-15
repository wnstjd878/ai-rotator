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

# nvm 로 깐 node 폴더에도 codex 심링크가 따로 있다. 배포 검수 게이트(verify_then_codex_review.sh)
# 는 빌드용으로 nvm 을 로드하는데, 그러면 nvm 의 bin 이 PATH 맨 앞에 와서 이 codex 가
# /opt/homebrew/bin/codex(순환 래퍼)를 가려버린다. 그 결과 게이트가 순환기를 건너뛰고 진짜
# codex 를 바로 불러 한 계정만 쓰다 한도에 걸렸다(2026-08-15 실측, jangsa-mkt 배포 동결).
# 그래서 이 경로들도 같이 래퍼로 덮는다. 되돌릴 때는 표준 node_modules 대상으로 복구한다.
NVM_TARGET="../lib/node_modules/@openai/codex/bin/codex.js"
nvm_codex_links() {
  ls -d "$HOME"/.nvm/versions/node/*/bin/codex 2>/dev/null || true
}

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
  for l in $(nvm_codex_links); do
    if [ "$(readlink "$l" 2>/dev/null || true)" = "$SHIM" ]; then
      echo "nvm codex: 래퍼 덮임 ($l)"
    else
      echo "nvm codex: 안 덮임 — 게이트가 순환기를 건너뛸 수 있음 ($l)"
    fi
  done
}

uninstall() {
  local target=""
  [ -f "$ORIGINAL" ] && target="$(cat "$ORIGINAL")"
  if [ -z "$target" ] || [ "$target" = "$SHIM" ]; then
    target="../lib/node_modules/@openai/codex/bin/codex.js"
  fi
  ln -sfn "$target" "$LINK"
  # nvm 쪽 codex 도 표준 대상으로 복구한다.
  for l in $(nvm_codex_links); do
    [ "$(readlink "$l" 2>/dev/null || true)" = "$SHIM" ] && ln -sfn "$NVM_TARGET" "$l"
  done
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
  # nvm 폴더의 codex 도 래퍼로 덮는다 (게이트가 nvm 을 로드해도 순환기를 타게).
  for l in $(nvm_codex_links); do
    ln -sfn "$SHIM" "$l"
  done
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
