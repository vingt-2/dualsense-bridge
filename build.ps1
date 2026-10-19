# Builds dist\DualSenseBridge.exe and the installer dist\DualSenseBridge-Setup-<version>.exe.
# Needs Python 3.10+ and Inno Setup 6 (winget install JRSoftware.InnoSetup).
param([string] $Version = "1.0.0")
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

python -m pip install --quiet -r requirements.txt
python tests\test_usbip_device.py
if ($LASTEXITCODE -ne 0) { throw "self-test failed" }

python -m PyInstaller --noconfirm --onefile --noconsole --uac-admin `
    --name DualSenseBridge --icon icon.ico --hidden-import pystray._win32 tray_app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }
& $iscc "/DAppVersion=$Version" installer\DualSenseBridge.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

Get-Item dist\DualSenseBridge.exe, "dist\DualSenseBridge-Setup-$Version.exe" | Format-Table Name, Length
