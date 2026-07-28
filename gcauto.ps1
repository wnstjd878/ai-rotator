# 지피티(Codex) 계정 자동 순환 래퍼 (codex 를 감싸는 코드)
# codex 대화형을 지금 계정의 폴더로 실행하고, 한도로 끝나면 다음 계정으로 바꿔 다시 시작한다.
# 사용: gc   (PowerShell 프로필에 등록된 짧은 명령. codex 대신 이걸 쓴다)
#
# 계정 전환 = 자격 파일 교체가 아니라 CODEX_HOME(설정 폴더) 교체다.
# 한 폴더에서 계정을 갈아끼우면 이전 계정 자격이 서버에서 폐기되기 때문이다(2026-07-28 실측).
# 한도 확인도 서버 사용량을 그대로 읽는다(codex_pool.py --limited). 확인에 구독을 안 쓴다.
$POOL = "C:\Users\JUNSEONG\ai-rotator\codex_pool.py"
$fails = 0
$prevHome = $env:CODEX_HOME
while ($true) {
    # 지금 써야 할 계정의 폴더를 받아 이번 실행에만 건다
    $home = (python $POOL --home | Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0 -or -not $home) {
        Write-Host "[자동순환] 쓸 수 있는 계정이 없습니다. python codex_login.py A" -ForegroundColor Red
        break
    }
    $env:CODEX_HOME = $home.Trim()

    codex @args

    # codex 가 끝났다. 지금 계정이 한도에 걸린 상태인지 확인(사용량 조회, 구독 소비 없음)
    python $POOL --limited
    $limited = ($LASTEXITCODE -eq 3)

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
if ($null -eq $prevHome) { Remove-Item Env:\CODEX_HOME -ErrorAction SilentlyContinue }
else { $env:CODEX_HOME = $prevHome }
