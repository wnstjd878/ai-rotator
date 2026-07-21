# == 운영 맥락 ==
# 용도: 지금 쓰는 크롬을 "디버그 포트 9222"로 다시 띄운다. 로그인은 그대로 유지됨
#       (로그인 정보는 프로필 폴더 디스크에 저장돼 있어서 재시작해도 남음).
# 실행: 이 PC PowerShell 에서  powershell -File launch_chrome_debug.ps1  또는  ! 채널
# 주의: 크롬이 이미 떠 있으면 디버그 포트가 안 열린다. 그래서 먼저 크롬을 완전히 닫아야 함.
#       -Force 주면 열려있는 크롬을 강제로 닫고 재시작(열려있던 탭은 닫힘 — 크롬 시작페이지에서 복원 가능).

param(
    [switch]$Force,
    [string]$Profile = "Default"   # "Default" 또는 "Profile 1" 등. 계정별로 바꿔 실행.
)

$chrome   = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$userData = "$env:LOCALAPPDATA\Google\Chrome\User Data"
$port     = 9222

$running = Get-Process chrome -ErrorAction SilentlyContinue
if ($running) {
    if ($Force) {
        Write-Host "[launch] 열려있는 크롬을 닫습니다..."
        $running | Stop-Process -Force
        Start-Sleep -Seconds 2
    } else {
        Write-Host "[launch] 크롬이 이미 실행 중입니다. 디버그 포트가 안 열립니다."
        Write-Host "[launch] 크롬을 전부 닫고 다시 실행하거나, 이 명령을 -Force 로 실행하세요:"
        Write-Host "         powershell -File launch_chrome_debug.ps1 -Force"
        exit 1
    }
}

Write-Host "[launch] 크롬을 디버그 포트 $port 로 띄웁니다 (프로필: $Profile, 로그인 유지)..."
Start-Process $chrome -ArgumentList "--remote-debugging-port=$port", "--user-data-dir=`"$userData`"", "--profile-directory=`"$Profile`""
Start-Sleep -Seconds 2
Write-Host "[launch] 완료. 이제 python reader.py 를 실행하면 이 크롬에 붙어 사용량을 읽습니다."
