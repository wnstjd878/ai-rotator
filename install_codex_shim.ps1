# == 운영 맥락 ==
# 용도: winserver(Windows)의 codex 실행 파일 자리에 순환 가로채기를 설치·해제한다.
#       클로드 쪽 install_shim.ps1 과 같은 방식이다(원본을 옆으로 밀고 그 자리에 래퍼).
# 실행: powershell -ExecutionPolicy Bypass -File install_codex_shim.ps1            설치
#       powershell -ExecutionPolicy Bypass -File install_codex_shim.ps1 -Uninstall 해제
#       powershell -ExecutionPolicy Bypass -File install_codex_shim.ps1 -Status    상태
# 방식: 원본 codex.cmd -> codex-real.cmd 로 이동. 그 자리에 codex.cmd 래퍼 생성.
#       래퍼는 CODEX_REAL_BIN 을 원본으로 가리키고 python 으로 codex_shim.py 를 부른다.
#       codex 는 npm 전역 설치라 실행 파일이 .cmd 하나뿐이다(클로드는 .exe 였다).
# 함정: 이 기계는 PowerShell 에서 .cmd 를 직접 부르면 조용히 죽는다(npm·codex 둘 다 확인).
#       그래서 설치 확인은 cmd /c 로 부른다. 파이썬 subprocess 로는 정상이라
#       순환기 본체(codex_shim.py)는 영향이 없다.
# 되돌리기: -Uninstall 로 codex.cmd 를 지우고 codex-real.cmd 를 codex.cmd 로 되돌린다.
#       node/codex 를 업데이트하면 원본 codex.cmd 가 되살아난다. 그때 설치를 다시 돌린다.
# 마지막 점검: 2026-07-31
param(
  [switch]$Uninstall,
  [switch]$Status
)
$ErrorActionPreference = "Stop"

$Dir  = $PSScriptRoot
$Shim = Join-Path $Dir "codex_shim.py"

function Get-BinDir {
  # codex.cmd 가 사는 폴더. 가로채기를 이미 깔았으면 codex-real.cmd 로도 찾는다.
  $c = Get-Command codex -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($c) { return (Split-Path $c.Source) }
  foreach ($p in @(
      "$env:APPDATA\npm",
      "C:\Users\pc\AppData\Local\Microsoft\WinGet\Packages\OpenJS.NodeJS.LTS_Microsoft.Winget.Source_8wekyb3d8bbwe\node-v24.18.0-win-x64"
    )) {
    if (Test-Path (Join-Path $p "codex-real.cmd")) { return $p }
  }
  throw "codex 를 못 찾았습니다. npm 전역 설치와 PATH 를 확인하세요."
}

$BinDir = Get-BinDir
$Cmd    = Join-Path $BinDir "codex.cmd"
$Real   = Join-Path $BinDir "codex-real.cmd"

function Get-Python {
  $c = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($c) { return $c.Source }
  foreach ($p in @("C:\Program Files\Python312\python.exe", "C:\Program Files\Python311\python.exe")) {
    if (Test-Path $p) { return $p }
  }
  throw "python 을 못 찾았습니다."
}

function Test-CodexRuns {
  # .cmd 는 이 기계에서 PowerShell 직접 호출이 안 되므로 cmd /c 로 확인한다.
  $out = cmd /c "`"$Cmd`" --version" 2>$null
  return ($out -join " ")
}

function Show-Status {
  Write-Output ("codex.cmd      : " + (Test-Path $Cmd))
  Write-Output ("codex-real.cmd : " + (Test-Path $Real))
  if ((Test-Path $Cmd) -and (Test-Path $Real)) {
    $body = Get-Content $Cmd -Raw -ErrorAction SilentlyContinue
    if ($body -match "codex_shim") {
      Write-Output "상태: 순환 가로채기 설치됨"
      return
    }
  }
  Write-Output "상태: 원래 codex (가로채기 없음)"
}

function Do-Uninstall {
  if ((Test-Path $Cmd) -and (Test-Path $Real)) {
    $body = Get-Content $Cmd -Raw -ErrorAction SilentlyContinue
    if ($body -match "codex_shim") { Remove-Item $Cmd -Force }
  }
  if ((Test-Path $Real) -and -not (Test-Path $Cmd)) { Move-Item $Real $Cmd -Force }
  $v = Test-CodexRuns
  if (-not $v) { Write-Output "경고: 되돌린 뒤 codex 실행 확인 실패. 수동 점검 필요." }
  else { Write-Output ("원래대로 되돌렸습니다. (" + $v.Trim() + ")") }
  Show-Status
}

function Do-Install {
  if (-not (Test-Path $Shim)) { throw "codex_shim.py 가 없습니다: $Shim" }
  $py = Get-Python

  # 원본을 옆으로 민다. 이미 밀려 있으면(재설치) 새로 생긴 원본은 버리고 기존 real 을 지킨다.
  if (Test-Path $Cmd) {
    $body = Get-Content $Cmd -Raw -ErrorAction SilentlyContinue
    if ($body -notmatch "codex_shim") {
      if (Test-Path $Real) { Remove-Item $Cmd -Force }
      else { Move-Item $Cmd $Real -Force }
    }
  }
  if (-not (Test-Path $Real)) { throw "원본 codex 를 못 찾았습니다(codex-real.cmd 없음)." }

  # 래퍼 생성. 인자는 %* 로 그대로 넘긴다(각 인자를 따옴표로 보존).
  $lines = @(
    '@echo off',
    'setlocal',
    ('set "CODEX_REAL_BIN=' + $Real + '"'),
    ('"' + $py + '" "' + $Shim + '" %*')
  )
  Set-Content -Path $Cmd -Value $lines -Encoding ASCII

  $v = Test-CodexRuns
  if (-not $v) {
    Write-Output "설치 후 codex 실행 실패. 되돌립니다."
    Do-Uninstall
    throw "설치 실패"
  }
  Write-Output ("설치했습니다. codex 단발 호출이 이제 계정 순환 보호를 받습니다. (" + $v.Trim() + ")")
  Show-Status
}

if ($Status) { Show-Status; return }
if ($Uninstall) { Do-Uninstall; return }
Do-Install
