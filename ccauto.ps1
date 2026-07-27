# 클로드 계정 자동 순환 래퍼 (claude 를 감싸는 코드)
# claude 대화형을 실행하고, 한도로 끝나면 자동으로 다음 계정 전환 후 다시 시작한다.
# 사용: cc   (PowerShell 프로필에 등록된 짧은 명령. claude 대신 이걸 쓴다)
$POOL = "C:\Users\JUNSEONG\ai-rotator\claude_pool.py"
$fails = 0
while ($true) {
    claude @args

    # claude 가 끝났다. 지금 계정이 한도/만료인지 조용히 확인(haiku ping, 소비 미미)
    $j = (("ping" | claude -p --model claude-haiku-4-5-20251001 --output-format json 2>&1) | Out-String)
    $limited = $j -match "weekly limit|usage limit reached|hit your .{0,40}limit|OAuth session expired|5-hour limit|rate.?limit"

    if ($limited) {
        Write-Host ""
        Write-Host "[자동순환] 한도 도달 감지 -> 다음 계정으로 전환합니다..." -ForegroundColor Yellow
        python $POOL --next
        Write-Host "[자동순환] 새 계정으로 claude 를 다시 시작합니다." -ForegroundColor Green
        Write-Host ""
        Start-Sleep -Milliseconds 400
        $fails++
        if ($fails -ge 8) {
            Write-Host "[자동순환] 전환이 너무 잦습니다. 계정 상태를 확인하세요 (cs)." -ForegroundColor Red
            break
        }
    } else {
        Write-Host "[자동순환] 정상 종료." -ForegroundColor Cyan
        break
    }
}
