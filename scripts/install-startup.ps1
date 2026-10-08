#Requires -Version 7.0
<#
.SYNOPSIS
Creates or removes the Startup shortcut that runs the production timeexisting collector at every interactive logon.

.DESCRIPTION
Creates "timeexisting collector.lnk" in the current user's Startup folder. The shortcut runs the uv tool install of timeexisting, never a development venv: its target is <uv tool dir>\timeexisting\Scripts\pythonw.exe with "-m timeexisting collect", working directory %USERPROFILE%. The tool directory comes from "uv tool dir". pythonw.exe has no console, so nothing opens at logon.

Install the tool first: uv tool install git+https://github.com/gsaronni/timeexisting@<tag>. Refuses with a clear message if uv or the tool's pythonw.exe is missing. Running it again replaces the shortcut of the same name, so exactly one collector starts at logon.

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
$ToolName = 'timeexisting'

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

if (-not (Get-Command uv -CommandType Application -ErrorAction SilentlyContinue)) {
    Stop-Refused 'uv is not on PATH. Install uv, then: uv tool install git+https://github.com/gsaronni/timeexisting@<tag>.'
}
$UvOutput = @(& uv tool dir 2>$null)
if ($LASTEXITCODE -ne 0 -or $UvOutput.Count -eq 0 -or -not "$($UvOutput[0])".Trim()) {
    Stop-Refused '"uv tool dir" did not return a tool directory.'
}
$ToolDir = "$($UvOutput[0])".Trim()
$Pythonw = Join-Path $ToolDir "$ToolName\Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $Pythonw -PathType Leaf)) {
    Stop-Refused "no pythonw.exe at $Pythonw. Install the tool first: uv tool install git+https://github.com/gsaronni/timeexisting@<tag>."
}
$WorkingDirectory = $env:USERPROFILE
if (-not $WorkingDirectory) {
    Stop-Refused 'USERPROFILE is not set.'
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
        $Link.WorkingDirectory = $WorkingDirectory
        $Link.Description = 'timeexisting collector'
        $Link.Save()
    }
    finally {
        [void] [Runtime.InteropServices.Marshal]::ReleaseComObject($Shell)
    }
    Write-Output "Created $Shortcut"
    Write-Output "  runs $Pythonw $Arguments"
    Write-Output "  in   $WorkingDirectory"
}
