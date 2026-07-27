# 지피티(Codex) 계정 자동 순환 래퍼 (codex 를 감싸는 코드)
# codex 대화형을 실행하고, 한도로 끝나면 자동으로 다음 계정 전환 후 다시 시작한다.
# 사용: gc   (PowerShell 프로필에 등록된 짧은 명령. codex 대신 이걸 쓴다)
#
# 클로드 쪽 ccauto.ps1 과 다른 점: 한도 확인을 별도 호출로 하지 않고 서버 사용량을
# 그대로 읽는다(codex_pool.py --limited). 확인에 구독을 안 쓴다.
$POOL = "C:\Users\JUNSEONG\ai-rotator\codex_pool.py"
$fails = 0
while ($true) {
    # 이전 세션에서 codex 가 자격을 갱신했으면 사본에 반영해 둔다(낡은 사본은 나중에 거절당함)
    python $POOL --sync | Out-Null

    codex @args

    # codex 가 끝났다. 지금 계정이 한도에 걸린 상태인지 확인(사용량 조회, 구독 소비 없음)
    python $POOL --limited
    $limited = ($LASTEXITCODE -eq 3)

    python $POOL --sync | Out-Null

    if ($limited) {
        Write-Host ""
        Write-Host "[자동순환] 한도 도달 감지 -> 다음 계정으로 전환합니다..." -ForegroundColor Yellow
        python $POOL --next
        if ($LASTEXITCODE -eq 2) {
            Write-Host "[자동순환] 갈아탈 계정이 없습니다. 초기화 시각까지 기다려야 합니다." -ForegroundColor Red
            break
        }
        Write-Host "[자동순환] 새 계정으로 codex 를 다시 시작합니다." -ForegroundColor Green
        Write-Host ""
        Start-Sleep -Milliseconds 400
        $fails++
        if ($fails -ge 6) {
            Write-Host "[자동순환] 전환이 너무 잦습니다. 계정 상태를 확인하세요 (gs)." -ForegroundColor Red
            break
        }
    } else {
        Write-Host "[자동순환] 정상 종료." -ForegroundColor Cyan
        break
    }
}
