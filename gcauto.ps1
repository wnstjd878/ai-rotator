# 지피티(Codex) 계정 자동 순환 래퍼 — codex 를 감싸는 코드
#
# 사용: 그냥 codex 를 치면 된다. PowerShell 프로필이 codex 를 이 스크립트로 바꿔 둔다.
#       (짧은 명령 gc 도 같은 것. 클로드의 cc 와 같은 자리)
#
# 하는 일 (사람이 명령을 고를 필요가 없게):
#   1. 켜기 전에 지금 계정이 이미 한도에 걸렸는지 보고, 걸렸으면 다음 계정으로 바꿔서 켠다
#   2. 지금 계정의 폴더(CODEX_HOME)를 걸고 codex 를 실행한다
#   3. codex 가 끝나면 한도에 걸린 상태인지 보고, 걸렸으면 다음 계정으로 바꿔 다시 켠다
#
# 계정 전환 = 자격 파일 교체가 아니라 CODEX_HOME(설정 폴더) 교체다.
# 한 폴더에서 계정을 갈아끼우면 이전 계정 자격이 서버에서 폐기되기 때문이다(2026-07-28 실측).
# 한도 확인은 서버 사용량을 그대로 읽는다. 확인에 구독을 안 쓴다.

$POOL = "C:\Users\JUNSEONG\ai-rotator\codex_pool.py"

# 진짜 codex 실행 파일. 프로필이 codex 를 함수로 덮어써도 여기서 되돌이에 빠지지 않게
# 반드시 실행 파일만 찾는다.
$real = (Get-Command codex -CommandType Application -ErrorAction SilentlyContinue |
         Select-Object -First 1).Source
if (-not $real) {
    Write-Host "codex 를 못 찾았습니다. npm 설치와 PATH 를 확인하세요." -ForegroundColor Red
    return
}

# 계정과 상관없는 관리·조회 명령은 순환을 태우지 않고 그대로 넘긴다
$first = ($args | Where-Object { $_ -notmatch '^-' } | Select-Object -First 1)
$passthrough = @('login', 'logout', 'mcp', 'app', 'completion', 'update', 'doctor', 'sandbox')
$isVersionish = ($args | Where-Object { $_ -in @('--version', '-V', '--help', '-h') }).Count -gt 0
# 단발 호출(exec/review)은 부르는 쪽이 출력을 읽어 파싱한다. 사용량 표시를 섞으면 배포 검수가
# 깨지므로 사람이 보는 대화형에서만 찍는다. 기준은 codex_shim.py 의 SINGLE_SHOT_CMDS 와 같다.
$isSingleShot = ($first -in @('exec', 'e', 'review'))

if ($first -eq 'logout') {
    Write-Host "[자동순환] codex logout 은 그 계정 자격을 지웁니다. 계정을 바꾸려면 gs 로 확인하고" -ForegroundColor Red
    Write-Host "           python C:\Users\JUNSEONG\ai-rotator\codex_login.py <라벨> 을 쓰세요." -ForegroundColor Red
    return
}
if ($isVersionish -or ($first -and $passthrough -contains $first)) {
    & $real @args
    return
}

$prevHome = $env:CODEX_HOME
$fails = 0
try {
    while ($true) {
        # 1) 켜기 전 확인 — 지금 계정이 이미 다 찼으면 켜기 전에 갈아탄다
        python $POOL --limited | Out-Null
        if ($LASTEXITCODE -eq 3) {
            Write-Host "[자동순환] 지금 계정이 한도에 걸려 있어 다음 계정으로 바꿔서 켭니다." -ForegroundColor Yellow
            python $POOL --next
            if ($LASTEXITCODE -eq 2) {
                Write-Host "[자동순환] 쓸 수 있는 계정이 없습니다. 초기화 시각까지 기다려야 합니다." -ForegroundColor Red
                break
            }
        }

        # 2) 지금 계정의 폴더를 걸고 실행
        #    ($home 은 PowerShell 예약 변수라 쓰면 안 된다 — 이름을 따로 둔다)
        $codexHome = (python $POOL --home | Select-Object -Last 1)
        if ($LASTEXITCODE -ne 0 -or -not $codexHome) {
            Write-Host "[자동순환] 쓸 수 있는 계정이 없습니다." -ForegroundColor Red
            Write-Host "           python C:\Users\JUNSEONG\ai-rotator\codex_login.py A" -ForegroundColor Red
            break
        }
        $env:CODEX_HOME = $codexHome.Trim()

        # 켜기 직전 계정별 남은 양을 화면에 먼저 보여준다(클로드 cc 와 같은 자리).
        # codex 안에서는 이 값을 볼 방법이 없어서 감싸는 코드가 유일한 자리다. 2초쯤 걸린다.
        if (-not $isSingleShot) {
            Write-Host ""
            Write-Host "[계정 사용량]" -ForegroundColor Cyan
            # 화면 출력으로 넘겨야 위 제목과 순서가 맞는다(표준 출력은 파이프를 타서 뒤늦게 합쳐진다).
            # 부르는 쪽이 출력을 모아 읽는 경우에도 이 줄들이 섞이지 않는다.
            python $POOL --status | ForEach-Object { Write-Host $_ }
            Write-Host ""
        }

        & $real @args

        # 3) 끝난 뒤 확인 — 한도면 다음 계정으로 바꿔 다시 켠다
        python $POOL --limited | Out-Null
        if ($LASTEXITCODE -ne 3) { break }

        Write-Host ""
        Write-Host "[자동순환] 한도 도달 -> 다음 계정으로 전환합니다..." -ForegroundColor Yellow
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
    }
}
finally {
    if ($null -eq $prevHome) { Remove-Item Env:\CODEX_HOME -ErrorAction SilentlyContinue }
    else { $env:CODEX_HOME = $prevHome }
}
