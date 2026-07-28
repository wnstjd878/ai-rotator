# == 운영 맥락 ==
# 용도: winserver(Windows)의 claude 실행 파일 자리에 순환 가로채기를 설치·해제한다.
#       봇들이 claude 를 which/후보경로로 찾으면 전부 C:\Users\pc\.local\bin\claude.exe 로
#       수렴하므로(조사 확인), 그 파일을 옆으로 밀고 그 자리에 래퍼를 둔다.
# 실행: powershell -ExecutionPolicy Bypass -File install_shim.ps1            설치
#       powershell -ExecutionPolicy Bypass -File install_shim.ps1 -Uninstall 해제
#       powershell -ExecutionPolicy Bypass -File install_shim.ps1 -Status    상태
# 방식: 원본 claude.exe -> claude-real.exe 로 이동. 그 자리에 claude.cmd 래퍼 생성.
#       래퍼는 CLAUDE_REAL_BIN 을 원본으로 가리키고 python 으로 claude_shim.py 를 부른다.
#       claude.exe 가 사라지면 which/후보검사가 claude.cmd 로 떨어진다.
# 되돌리기: -Uninstall 로 claude.cmd 지우고 claude-real.exe 를 claude.exe 로 되돌린다.
#       claude 를 업데이트하면 claude.exe 가 다시 생길 수 있다. 그때 install 을 다시 돌린다.
# 마지막 점검: 2026-07-26
param(
  [switch]$Uninstall,
  [switch]$Status
)
$ErrorActionPreference = "Stop"

$Dir    = $PSScriptRoot
$BinDir = Join-Path $env:USERPROFILE ".local\bin"
$Link   = Join-Path $BinDir "claude.exe"
$Real   = Join-Path $BinDir "claude-real.exe"
$Cmd    = Join-Path $BinDir "claude.cmd"
$Shim   = Join-Path $Dir "claude_shim.py"

function Get-Python {
  $c = Get-Command python -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  foreach ($p in @("C:\Program Files\Python312\python.exe", "C:\Program Files\Python311\python.exe")) {
    if (Test-Path $p) { return $p }
  }
  throw "python 을 못 찾았습니다."
}

function Show-Status {
  Write-Output ("claude.exe   : " + (Test-Path $Link))
  Write-Output ("claude-real.exe: " + (Test-Path $Real))
  Write-Output ("claude.cmd   : " + (Test-Path $Cmd))
  if ((Test-Path $Cmd) -and (Test-Path $Real) -and -not (Test-Path $Link)) {
    Write-Output "상태: 순환 가로채기 설치됨"
  } else {
    Write-Output "상태: 원래 claude (가로채기 없음)"
  }
}

function Do-Uninstall {
  if (Test-Path $Cmd) { Remove-Item $Cmd -Force }
  if ((Test-Path $Real) -and -not (Test-Path $Link)) {
    Move-Item $Real $Link -Force
  }
  # 되돌린 뒤 실행 확인
  $v = & claude --version 2>$null
  if (-not $v) { Write-Output "경고: 되돌린 뒤 claude 실행 확인 실패. 수동 점검 필요." }
  else { Write-Output "원래대로 되돌렸습니다." }
  Show-Status
}

function Do-Install {
  if (-not (Test-Path $Shim)) { throw "claude_shim.py 가 없습니다: $Shim" }
  $py = Get-Python

  # 원본을 옆으로 민다(이미 밀려 있으면 유지)
  if (Test-Path $Link) {
    if (Test-Path $Real) { Remove-Item $Link -Force }  # 재설치: 새 claude.exe 는 버리고 기존 real 유지
    else { Move-Item $Link $Real -Force }
  }
  if (-not (Test-Path $Real)) { throw "원본 claude 를 못 찾았습니다(claude-real.exe 없음)." }

  # 래퍼 생성. 인자는 %* 로 그대로 넘긴다(각 인자를 따옴표로 보존).
  $lines = @(
    '@echo off',
    'setlocal',
    ('set "CLAUDE_REAL_BIN=' + $Real + '"'),
    ('"' + $py + '" "' + $Shim + '" %*')
  )
  Set-Content -Path $Cmd -Value $lines -Encoding ASCII

  $v = & claude --version 2>$null
  if (-not $v) {
    Write-Output "설치 후 claude 실행 실패. 되돌립니다."
    Do-Uninstall
    throw "설치 실패"
  }
  Write-Output "설치했습니다. 봇들의 단발 호출이 이제 계정 순환 보호를 받습니다."
  Show-Status
}

if ($Status) { Show-Status; return }
if ($Uninstall) { Do-Uninstall; return }
Do-Install
