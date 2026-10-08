#Requires -Version 7.0
<#
.SYNOPSIS
Points the development venv at its own config and state, so dev never touches the real ledger.

.DESCRIPTION
Writes "timeexisting_devstate.pth" into the venv's site-packages. Python runs it at every interpreter start in that venv, activated or not: python.exe, pythonw.exe, te.exe, pytest, and any collector they spawn. It sets TIMEEXISTING_STATE_DIR and TIMEEXISTING_CONFIG_DIR with os.environ.setdefault to <repo>\.devstate\state and <repo>\.devstate\config, so an explicit value, such as the one every test sets, still wins.

site-packages is resolved from the venv's own python.exe, never assumed. The repository is resolved from this script's location, never from the current directory. Running it again rewrites the same single file; nothing else in the venv is touched.

.PARAMETER Venv
The venv to configure. Defaults to the repository's .venv; exists so the script can be tested against a throwaway venv.

.PARAMETER Uninstall
Remove the .pth file instead of writing it.

.EXAMPLE
pwsh scripts\dev-setup.ps1

.EXAMPLE
pwsh scripts\dev-setup.ps1 -WhatIf

.EXAMPLE
pwsh scripts\dev-setup.ps1 -Uninstall
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $Venv,
    [switch] $Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$PthName = 'timeexisting_devstate.pth'

function Stop-Refused([string] $Message) {
    [Console]::Error.WriteLine("dev-setup: $Message")
    exit 1
}

$Repository = Split-Path -Parent $PSScriptRoot
if (-not $Venv) {
    $Venv = Join-Path $Repository '.venv'
}
$Python = if ($IsWindows) { Join-Path $Venv 'Scripts\python.exe' } else { Join-Path $Venv 'bin/python' }

if (-not (Test-Path -LiteralPath $Venv -PathType Container)) {
    Stop-Refused "no venv at $Venv. Create it first: python -m venv .venv, then pip install -e "".[dev]""."
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    Stop-Refused "no interpreter at $Python. The venv is incomplete; recreate it."
}

$SitePackages = & $Python -I -c 'import sysconfig; print(sysconfig.get_path("purelib"))'
if ($LASTEXITCODE -ne 0 -or -not $SitePackages -or -not (Test-Path -LiteralPath $SitePackages -PathType Container)) {
    Stop-Refused "could not resolve site-packages from $Python."
}
$Pth = Join-Path $SitePackages $PthName

if ($Uninstall) {
    if (-not (Test-Path -LiteralPath $Pth)) {
        Write-Output "No $PthName in $SitePackages; nothing to remove."
        exit 0
    }
    if ($PSCmdlet.ShouldProcess($Pth, 'Remove dev state .pth')) {
        Remove-Item -LiteralPath $Pth
        Write-Output "Removed $Pth"
    }
    exit 0
}

$DevState = Join-Path $Repository '.devstate'
$StateDir = Join-Path $DevState 'state'
$ConfigDir = Join-Path $DevState 'config'
foreach ($Directory in $StateDir, $ConfigDir) {
    if ($Directory.Contains('"')) {
        Stop-Refused "cannot write a path containing a double quote: $Directory"
    }
}

# A .pth line runs only when it starts with "import", and it must stay on one line.
$Line = "import os; os.environ.setdefault(""TIMEEXISTING_STATE_DIR"", r""$StateDir""); os.environ.setdefault(""TIMEEXISTING_CONFIG_DIR"", r""$ConfigDir"")"
$Content = "$Line`n"

if ((Test-Path -LiteralPath $Pth -PathType Leaf) -and ((Get-Content -LiteralPath $Pth -Raw) -ceq $Content)) {
    Write-Output "Unchanged $Pth"
}
elseif ($PSCmdlet.ShouldProcess($Pth, "Write dev state .pth for $DevState")) {
    [IO.File]::WriteAllText($Pth, $Content, [Text.UTF8Encoding]::new($false))
    Write-Output "Wrote $Pth"
}
else {
    exit 0
}
Write-Output "  state  $StateDir"
Write-Output "  config $ConfigDir"
