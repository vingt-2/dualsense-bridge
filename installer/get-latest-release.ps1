# Downloads the newest asset matching -Pattern from a GitHub repo's latest release.
# Used by the installer to fetch usbip-win2 and HidHide from their official releases.
param(
    [Parameter(Mandatory)] [string] $Repo,      # e.g. vadimgrn/usbip-win2
    [Parameter(Mandatory)] [string] $Pattern,   # e.g. USBip-*-x64.exe
    [Parameter(Mandatory)] [string] $OutFile
)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with the progress bar

$release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -UseBasicParsing
$asset = $release.assets | Where-Object { $_.name -like $Pattern } | Select-Object -First 1
if (-not $asset) { throw "No asset matching '$Pattern' in $Repo $($release.tag_name)" }
Invoke-WebRequest $asset.browser_download_url -OutFile $OutFile -UseBasicParsing
Write-Output "$($asset.name) ($($release.tag_name))"
