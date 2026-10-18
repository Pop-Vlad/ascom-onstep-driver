<#
.SYNOPSIS
	Build the Windows installer for the OnStep Alpaca driver.

.DESCRIPTION
	Freezes the driver with PyInstaller, then wraps the result with Inno Setup. The version
	comes from pyproject.toml so the executables, the installer filename and the entry in
	Apps & features can never disagree.

	Prerequisites:
		pip install -e ".[tray]" pyinstaller
		winget install JRSoftware.InnoSetup

.PARAMETER SkipFreeze
	Reuse the existing dist\onstep-alpaca folder and only rebuild the installer. Useful when
	iterating on installer.iss, which takes seconds rather than a minute.
#>
[CmdletBinding()]
param(
	[switch] $SkipFreeze
)

$ErrorActionPreference = 'Stop'

$Packaging = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Root = Split-Path -Parent $Packaging

function Find-Iscc {
	$command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
	if ($command) { return $command.Source }
	$candidates = @(
		"$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
		"${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
		"$env:ProgramFiles\Inno Setup 6\ISCC.exe"
	)
	foreach ($candidate in $candidates) {
		if (Test-Path $candidate) { return $candidate }
	}
	throw "Inno Setup not found. Install it with: winget install JRSoftware.InnoSetup"
}

# Read the version the same way the spec file does, so there is one source for it.
$Version = python -c "import tomllib,pathlib;print(tomllib.loads(pathlib.Path(r'$Root/pyproject.toml').read_text())['project']['version'])"
if ($LASTEXITCODE -ne 0) { throw "could not read the version from pyproject.toml" }
Write-Host "Building $Version" -ForegroundColor Cyan

if (-not $SkipFreeze) {
	Write-Host "`n[1/2] Freezing with PyInstaller..." -ForegroundColor Cyan
	# --noconfirm alone leaves stale modules behind when imports change, and a driver that
	# works only on a clean build is a bug waiting for the worst possible night.
	Remove-Item -Recurse -Force "$Root\build\onstep-alpaca" -ErrorAction SilentlyContinue
	Remove-Item -Recurse -Force "$Root\dist\onstep-alpaca" -ErrorAction SilentlyContinue
	# --log-level WARN: the default prints every module hook it considers, which buries
	# the one line that matters when something is actually missing from the bundle.
	python -m PyInstaller "$Packaging\onstep-alpaca.spec" --noconfirm --log-level WARN `
		--distpath "$Root\dist" --workpath "$Root\build"
	if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
} else {
	Write-Host "`n[1/2] Skipping the freeze, reusing dist\onstep-alpaca" -ForegroundColor Yellow
}

$Frozen = Join-Path $Root "dist\onstep-alpaca\onstep-alpaca-tray.exe"
if (-not (Test-Path $Frozen)) { throw "nothing to package: $Frozen is missing" }

Write-Host "`n[2/2] Building the installer with Inno Setup..." -ForegroundColor Cyan
$Iscc = Find-Iscc
& $Iscc "/DAppVersion=$Version" "$Packaging\installer.iss" | Select-Object -Last 6
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

$Installer = Join-Path $Root "dist\OnStepAlpaca-Setup-$Version.exe"
$Size = [math]::Round((Get-Item $Installer).Length / 1MB, 1)
Write-Host "`nDone: $Installer ($Size MB)" -ForegroundColor Green
