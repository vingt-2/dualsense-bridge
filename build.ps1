# Builds dist\DualSenseBridge.exe and the installer dist\DualSenseBridge-Setup-<version>.exe.
# Needs Python 3.10+ and Inno Setup 6 (winget install JRSoftware.InnoSetup).
param([string] $Version = "1.0.0")
# Exit codes are checked explicitly: pip and PyInstaller log to stderr, which
# $ErrorActionPreference = 'Stop' would turn into errors when output is redirected.
Set-Location $PSScriptRoot

python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
python tests\test_usbip_device.py
if ($LASTEXITCODE -ne 0) { throw "self-test failed" }
python tests\test_shortcuts.py
if ($LASTEXITCODE -ne 0) { throw "shortcut self-test failed" }

# The version the app reports and compares with GitHub releases (see updater.py)
Set-Content -Encoding ascii _build_version.py "VERSION = `"$Version`""

# libopus for the controller's Bluetooth speaker and mic audio (see opus.py)
$opusDll = python -c "import importlib.util, os; print(os.path.join(importlib.util.find_spec('pyogg').submodule_search_locations[0], 'opus.dll'))"
if (-not (Test-Path $opusDll)) { throw "opus.dll not found (pip install PyOgg)" }

python -m PyInstaller --noconfirm --onefile --noconsole --uac-admin `
    --name DualSenseBridge --icon icon.ico --hidden-import pystray._win32 `
    --add-binary "$opusDll;." tray_app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }
& $iscc "/DAppVersion=$Version" installer\DualSenseBridge.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

Get-Item dist\DualSenseBridge.exe, "dist\DualSenseBridge-Setup-$Version.exe" | Format-Table Name, Length
