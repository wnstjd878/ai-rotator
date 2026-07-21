#!/bin/bash
# == 운영 맥락 ==
# 용도: (Mac) 크롬을 디버그 포트 9222 로 재시작한다. 로그인은 프로필 폴더에 남아 유지됨.
# 실행: bash launch_chrome_debug.sh [프로필디렉토리] [-Force]
#       예) bash launch_chrome_debug.sh "Profile 1" -Force
# 주의: 크롬이 이미 떠 있으면 디버그 포트가 안 열린다. -Force 면 닫고 재시작(열린 탭은 닫힘).
#       Mac 본체 화면 세션에서 실행해야 크롬 GUI 가 뜬다.

PORT=9222
PROFILE="${1:-Default}"
FORCE="${2:-}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

if pgrep -x "Google Chrome" >/dev/null 2>&1; then
  if [ "$FORCE" = "-Force" ] || [ "$FORCE" = "force" ]; then
    echo "[launch] 열려있는 크롬을 닫습니다..."
    osascript -e 'quit app "Google Chrome"' >/dev/null 2>&1
    sleep 2
    pkill -x "Google Chrome" >/dev/null 2>&1
    sleep 1
  else
    echo "[launch] 크롬이 이미 실행 중입니다. 디버그 포트가 안 열립니다."
    echo "[launch] 전부 닫고 다시 실행하거나 -Force 로 실행하세요:"
    echo "         bash launch_chrome_debug.sh \"$PROFILE\" -Force"
    exit 1
  fi
fi

echo "[launch] 크롬을 디버그 포트 $PORT 로 띄웁니다 (프로필: $PROFILE, 로그인 유지)..."
open -na "Google Chrome" --args --remote-debugging-port="$PORT" --profile-directory="$PROFILE"
sleep 2
echo "[launch] 완료. 이제 python rotate.py 를 실행하면 이 크롬에 붙어 사용량을 읽습니다."
