# 클로드 계정 자동 순환 래퍼 (claude 를 감싸는 코드)
# claude 대화형을 실행하고, 한도로 끝나면 자동으로 다음 계정 전환 후 다시 시작한다.
# 사용: cc   (PowerShell 프로필에 등록된 짧은 명령. claude 대신 이걸 쓴다)
# 자기가 놓인 폴더에서 찾는다. 머신마다 홈 경로가 달라 박아두면 다른 기기에서 안 돈다
# (2026-08-04 출장 노트북 C:\Users\wnstj 추가하며 바뀜).
$POOL = Join-Path $PSScriptRoot "claude_pool.py"
$fails = 0
# 첫 실행은 사용자가 준 인자 그대로. 한도로 갈아탄 뒤 다시 켤 때만 --continue 를 붙여
# 직전 대화를 이어받는다. (처음부터 --continue 를 박으면 새 대화를 못 연다)
$runArgs = @($args)
# 사용량 화면 색을 켠다. PowerShell 이 파이썬 출력을 가로채면 파이썬 쪽 판정이
# 거짓이 되어 막대 색이 통째로 빠진다(2026-09-04).
$env:AIROTATOR_COLOR = "1"
# 이 창이 쓰는 모델 칸을 표시해 둔다(2026-08-22). 봇 단발 호출이 계정을 갈아끼울 때
# 이 칸이 막힌 계정으로는 안 넘어간다. 창이 끝나면 지우고, 못 지워도 이 프로세스가
# 죽으면 순환기가 알아서 걷어낸다.
$prevClaimPid = $env:CLAUDE_ROTATOR_CLAIM_PID   # 원래 값(대개 없음)은 끝날 때 되돌린다
$env:CLAUDE_ROTATOR_CLAIM_PID = "$PID"          # 상태줄이 이 번호의 표시를 갱신한다
try {
while ($true) {
    # 켜기 직전 확인. 지금 계정이 이미 다 찼으면 켜기 전에 갈아끼운다.
    # (감싸는 코드는 claude 가 끝난 뒤에만 한도를 볼 수 있어서, 이게 없으면 다 찬 계정으로 한 번 켜진다)
    # 인자를 같이 넘긴다 — 그래야 --model 을 보고 그 모델 칸(예: Fable)까지 확인한다.
    # 인자에 없으면 순환기가 settings.json 에 고정된 모델을 대신 본다.
    python $POOL --ensure @runArgs
    # 계정이 정해진 뒤에 이 창의 모델 칸을 표시한다(같은 번호로 다시 부르면 갱신된다).
    python $POOL --claim $PID @runArgs
    # 켜기 직전 계정별 사용량을 화면에 먼저 보여준다(사용자 요구 2026-07-29).
    # claude 안에서는 이 값을 볼 수 없어서, 감싸는 코드가 뜨기 전에 찍는 자리가 유일하다.
    Write-Host ""
    Write-Host "[계정 사용량]" -ForegroundColor Cyan
    # 여기도 인자를 넘긴다 — 쓰려는 모델 칸이 전 계정에서 바닥이면 그 한 줄을 같이 찍는다.
    python $POOL --status @runArgs
    Write-Host ""
    claude @runArgs

    # claude 가 끝났다. 지금 계정이 한도/만료인지 조용히 확인(haiku ping, 소비 미미)
    $j = (("ping" | claude -p --model claude-haiku-4-5-20251001 --output-format json 2>&1) | Out-String)
    # "reached your ..." 는 모델별 한도 문구다(실제: "You've reached your Fable 5 limit").
    # 이게 없어서 2026-08-12 에 Fable 이 막혔는데도 자동 전환이 안 걸렸다.
    $limited = $j -match "weekly limit|usage limit reached|hit your .{0,40}limit|reached your .{0,40}limit|OAuth session expired|5-hour limit|rate.?limit"
    # haiku 로 찔러보면 Fable 칸만 바닥난 경우는 안 잡힌다(그 칸은 haiku 가 안 쓴다).
    # 그래서 사용량 조회(--ensure)를 한 번 더 한다. 종료코드 3 = 그 칸이 바닥나 이미 갈아탔음.
    $switchedAlready = $false
    if (-not $limited) {
        python $POOL --ensure @runArgs
        if ($LASTEXITCODE -eq 3) { $limited = $true; $switchedAlready = $true }
    }

    if ($limited) {
        Write-Host ""
        Write-Host "[자동순환] 한도 도달 감지 -> 다음 계정으로 전환합니다..." -ForegroundColor Yellow
        if (-not $switchedAlready) { python $POOL --next @runArgs }
        # 이어받기 인자가 아직 없고, 사용자가 --resume 으로 특정 대화를 고른 게 아니면 붙인다
        $hasResume = ($runArgs -contains "--continue") -or ($runArgs -contains "-c") -or ($runArgs -contains "--resume") -or ($runArgs -contains "-r")
        if (-not $hasResume) {
            $runArgs = @("--continue") + $runArgs
            Write-Host "[자동순환] 직전 대화를 이어받아 다시 켭니다 (--continue)." -ForegroundColor Green
        }
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
} finally {
    python $POOL --unclaim $PID | Out-Null
    # 이 값을 남기면 같은 PowerShell 창에서 그 뒤에 claude 를 직접 실행했을 때
    # 가로채기가 남의 번호를 이 창의 것으로 오해한다
    # (2026-08-22 코덱스 검수가 잡음. bash 쪽은 자식 프로세스라 저절로 사라진다).
    if ($null -eq $prevClaimPid) {
        Remove-Item Env:CLAUDE_ROTATOR_CLAIM_PID -ErrorAction SilentlyContinue
    } else {
        $env:CLAUDE_ROTATOR_CLAIM_PID = $prevClaimPid
    }
}
