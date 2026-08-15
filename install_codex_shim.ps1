# == Operational context ==
# Purpose: install/uninstall the account-rotation interceptor over the codex launcher on Windows.
#          Mirrors install_shim.ps1 (claude). Auto codex calls then go through account rotation.
# Usage:   powershell -ExecutionPolicy Bypass -File install_codex_shim.ps1            install
#          powershell -ExecutionPolicy Bypass -File install_codex_shim.ps1 -Uninstall revert
#          powershell -ExecutionPolicy Bypass -File install_codex_shim.ps1 -Status    show
# How:     codex ships THREE launchers in the node bin dir - codex.cmd, codex.ps1, and an
#          extensionless codex (sh, for git-bash). Earlier this script only replaced codex.cmd,
#          but PowerShell resolves `codex` to codex.ps1 first, so PowerShell/bots bypassed the
#          shim and used one account until it hit its limit (2026-08-15, same class as the Mac
#          nvm-shadow bug). Fix: codex.cmd becomes the shim; codex.ps1 is moved aside to
#          codex-real.ps1 and NOT recreated, so PowerShell falls through to codex.cmd (a .cmd
#          needs no ExecutionPolicy, unlike a .ps1 shim which the machine policy blocks); the
#          extensionless codex (sh, git-bash) becomes a shim routing to codex_shim.py.
#          Every shim uses CODEX_REAL_BIN pointing at codex-real.cmd (the form codex_shim.py runs).
# Revert:  -Uninstall restores every launcher from its codex-real.* copy.
#          A node/codex npm update recreates the real launchers; re-run this script afterward.
# Last checked: 2026-08-15
param(
  [switch]$Uninstall,
  [switch]$Status
)
$ErrorActionPreference = "Stop"

$Dir  = $PSScriptRoot
$Shim = Join-Path $Dir "codex_shim.py"

function Get-BinDir {
  # Folder that holds codex.cmd. Also found via codex-real.cmd if the shim is already installed.
  $c = Get-Command codex -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($c) { return (Split-Path $c.Source) }
  $c2 = Get-Command codex -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($c2 -and $c2.Source) { return (Split-Path $c2.Source) }
  foreach ($p in @(
      "$env:APPDATA\npm",
      "C:\Users\pc\AppData\Local\Microsoft\WinGet\Packages\OpenJS.NodeJS.LTS_Microsoft.Winget.Source_8wekyb3d8bbwe\node-v24.18.0-win-x64"
    )) {
    if (Test-Path (Join-Path $p "codex-real.cmd")) { return $p }
    if (Test-Path (Join-Path $p "codex.cmd")) { return $p }
  }
  throw "codex not found. Check the npm/WinGet node install on PATH."
}

$BinDir  = Get-BinDir
$Cmd     = Join-Path $BinDir "codex.cmd"
$Real    = Join-Path $BinDir "codex-real.cmd"
$Ps1     = Join-Path $BinDir "codex.ps1"
$Ps1Real = Join-Path $BinDir "codex-real.ps1"
$Sh      = Join-Path $BinDir "codex"
$ShReal  = Join-Path $BinDir "codex-real"

function Get-Python {
  $c = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($c) { return $c.Source }
  foreach ($p in @("C:\Program Files\Python312\python.exe", "C:\Program Files\Python311\python.exe")) {
    if (Test-Path $p) { return $p }
  }
  throw "python not found."
}

function Is-Shim($path) {
  if (-not (Test-Path $path)) { return $false }
  $b = Get-Content $path -Raw -ErrorAction SilentlyContinue
  return ($b -match "codex_shim")
}

function Test-CodexRuns {
  # .cmd must be called via cmd /c from PowerShell to run correctly.
  $out = cmd /c "`"$Cmd`" --version" 2>$null
  return ($out -join " ")
}

function Show-Status {
  if (Is-Shim $Cmd) { Write-Output "codex.cmd : shim installed" }
  elseif (Test-Path $Cmd) { Write-Output "codex.cmd : REAL (not shimmed)" }
  else { Write-Output "codex.cmd : missing" }
  # codex.ps1 should be moved aside so PowerShell falls through to codex.cmd.
  if (Test-Path $Ps1) { Write-Output "codex.ps1 : present (should be moved aside)" }
  else { Write-Output "codex.ps1 : moved aside (PowerShell falls through to codex.cmd)" }
  if (Is-Shim $Sh) { Write-Output "codex(sh) : shim installed" }
  elseif (Test-Path $Sh) { Write-Output "codex(sh) : REAL (not shimmed)" }
  else { Write-Output "codex(sh) : missing" }
  $resolved = Get-Command codex -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($resolved) { Write-Output ("PowerShell resolves codex -> " + $resolved.Source) }
}

function Restore-One($launcher, $realp) {
  if ((Is-Shim $launcher) -and (Test-Path $realp)) { Remove-Item $launcher -Force }
  if ((Test-Path $realp) -and -not (Test-Path $launcher)) { Move-Item $realp $launcher -Force }
}

function Do-Uninstall {
  Restore-One $Cmd $Real
  Restore-One $Ps1 $Ps1Real
  Restore-One $Sh $ShReal
  $v = Test-CodexRuns
  if (-not $v) { Write-Output "WARN: codex did not run after revert. Check the launchers." }
  else { Write-Output ("Reverted. (" + $v.Trim() + ")") }
  Show-Status
}

function Save-Real($launcher, $realp) {
  # Move the real launcher aside once. If already a shim, leave the saved real intact.
  if (Test-Path $launcher) {
    if (-not (Is-Shim $launcher)) {
      if (Test-Path $realp) { Remove-Item $launcher -Force }
      else { Move-Item $launcher $realp -Force }
    }
  }
}

function Do-Install {
  if (-not (Test-Path $Shim)) { throw "codex_shim.py missing: $Shim" }
  $py = Get-Python

  # 1) codex.cmd (primary; codex_shim.py runs CODEX_REAL_BIN which points here)
  Save-Real $Cmd $Real
  if (-not (Test-Path $Real)) { throw "real codex not found (codex-real.cmd missing)." }
  $cmdLines = @(
    '@echo off',
    'setlocal',
    ('set "CODEX_REAL_BIN=' + $Real + '"'),
    ('"' + $py + '" "' + $Shim + '" %*')
  )
  Set-Content -Path $Cmd -Value $cmdLines -Encoding ASCII

  # 2) codex.ps1: move aside and do NOT recreate. PowerShell then falls through to codex.cmd
  #    (the shim). A .ps1 shim would be blocked by the machine ExecutionPolicy; a .cmd is not.
  Save-Real $Ps1 $Ps1Real
  if (Test-Path $Ps1) { Remove-Item $Ps1 -Force }   # in case a stale shim .ps1 exists

  # 3) extensionless codex (git-bash/msys sh). Route through codex_shim.py.
  Save-Real $Sh $ShReal
  $shLines = @(
    '#!/bin/sh',
    '# codex_shim rotation launcher (auto-generated)',
    ('CODEX_REAL_BIN="' + $Real + '" exec "' + $py + '" "' + $Shim + '" "$@"')
  )
  Set-Content -Path $Sh -Value $shLines -Encoding ASCII

  $v = Test-CodexRuns
  if (-not $v) {
    Write-Output "codex failed to run after install. Reverting."
    Do-Uninstall
    throw "install failed"
  }
  Write-Output ("Installed. codex single-shot calls now go through account rotation. (" + $v.Trim() + ")")
  Show-Status
}

if ($Status) { Show-Status; return }
if ($Uninstall) { Do-Uninstall; return }
Do-Install
