#Requires -Version 7.0
<#
.SYNOPSIS
Creates or removes the Startup shortcut that runs the timeexisting collector at every interactive logon.

.DESCRIPTION
Creates "timeexisting collector.lnk" in the current user's Startup folder. The shortcut runs the repository's own venv interpreter, .venv\Scripts\pythonw.exe, with "-m timeexisting collect", working directory the repository root. The repository is resolved from this script's location, never from the current directory. pythonw.exe has no console, so nothing opens at logon.

Refuses with a clear message if the venv or its pythonw.exe is missing. Running it again replaces the shortcut.

.PARAMETER Uninstall
Remove the shortcut instead of creating it.

.PARAMETER StartupFolder
Where the shortcut goes. Defaults to the user's Startup folder; exists so the script can be tested without touching it.

.EXAMPLE
pwsh scripts\install-startup.ps1

.EXAMPLE
pwsh scripts\install-startup.ps1 -WhatIf

.EXAMPLE
pwsh scripts\install-startup.ps1 -Uninstall
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [switch] $Uninstall,
    [string] $StartupFolder = [Environment]::GetFolderPath('Startup')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ShortcutName = 'timeexisting collector.lnk'
$Arguments = '-m timeexisting collect'

function Stop-Refused([string] $Message) {
    [Console]::Error.WriteLine("install-startup: $Message")
    exit 1
}

if (-not $IsWindows) {
    Stop-Refused 'the Startup shortcut is Windows only.'
}
if (-not $StartupFolder) {
    Stop-Refused 'could not resolve the Startup folder.'
}

$Repository = Split-Path -Parent $PSScriptRoot
$Shortcut = Join-Path $StartupFolder $ShortcutName

if ($Uninstall) {
    if (-not (Test-Path -LiteralPath $Shortcut)) {
        Write-Output "No shortcut at $Shortcut; nothing to remove."
        exit 0
    }
    if ($PSCmdlet.ShouldProcess($Shortcut, 'Remove Startup shortcut')) {
        Remove-Item -LiteralPath $Shortcut
        Write-Output "Removed $Shortcut"
    }
    exit 0
}

$Venv = Join-Path $Repository '.venv'
$Pythonw = Join-Path $Venv 'Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $Venv -PathType Container)) {
    Stop-Refused "no venv at $Venv. Create it first: python -m venv .venv, then pip install -e "".[dev,windows]""."
}
if (-not (Test-Path -LiteralPath $Pythonw -PathType Leaf)) {
    Stop-Refused "no pythonw.exe at $Pythonw. The venv is incomplete; recreate it."
}

if ($PSCmdlet.ShouldProcess($Shortcut, "Create Startup shortcut to $Pythonw $Arguments")) {
    if (-not (Test-Path -LiteralPath $StartupFolder -PathType Container)) {
        New-Item -ItemType Directory -Path $StartupFolder | Out-Null
    }
    $Shell = New-Object -ComObject WScript.Shell
    try {
        $Link = $Shell.CreateShortcut($Shortcut)
        $Link.TargetPath = $Pythonw
        $Link.Arguments = $Arguments
        $Link.WorkingDirectory = $Repository
        $Link.Description = 'timeexisting collector'
        $Link.Save()
    }
    finally {
        [void] [Runtime.InteropServices.Marshal]::ReleaseComObject($Shell)
    }
    Write-Output "Created $Shortcut"
    Write-Output "  runs $Pythonw $Arguments"
    Write-Output "  in   $Repository"
}
