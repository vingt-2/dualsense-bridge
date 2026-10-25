"""Update check: compares this build's version with the latest GitHub release, and
downloads and runs the new installer when asked.

The installer closes the running app itself (DualSenseBridge.exe --quit, see
installer/DualSenseBridge.iss) and, run with /SILENT /LAUNCH=1, starts the new one.
"""
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass

REPO = "vingt-2/dualsense-bridge"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = re.compile(r"^DualSenseBridge-Setup-[\d.]+\.exe$")

try:
    from _build_version import VERSION   # written by build.ps1
except ImportError:
    VERSION = None                       # running from source


@dataclass
class Release:
    version: str        # e.g. "1.6.0"
    page: str           # release notes on GitHub
    installer: str      # download URL of the setup .exe
    size: int


def parse(version):
    return tuple(int(n) for n in re.findall(r"\d+", version)[:3])


def latest():
    """The latest published release, or None if it has no installer attached."""
    req = urllib.request.Request(LATEST, headers={"Accept": "application/vnd.github+json",
                                                  "User-Agent": "DualSenseBridge"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    asset = next((a for a in data.get("assets", []) if ASSET.match(a["name"])), None)
    if asset is None:
        return None
    return Release(data["tag_name"].lstrip("v"), data["html_url"],
                   asset["browser_download_url"], asset["size"])


def newer(release):
    return VERSION is not None and release is not None and parse(release.version) > parse(VERSION)


def download(release):
    """Download the installer to a temp folder and return its path."""
    folder = tempfile.mkdtemp(prefix="DualSenseBridge-update-")
    path = os.path.join(folder, f"DualSenseBridge-Setup-{release.version}.exe")
    req = urllib.request.Request(release.installer, headers={"User-Agent": "DualSenseBridge"})
    with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)
    if os.path.getsize(path) != release.size:
        raise OSError(f"download incomplete ({os.path.getsize(path)} of {release.size} bytes)")
    return path


CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def install(path):
    """Run the installer silently; it quits this app, installs, and starts the new version.

    The frozen app runs inside a job object (PyInstaller's one-file launcher) that kills
    whatever it started once it exits, and the installer has to close the app. So start
    the installer outside the job where Windows allows it; the installer also moves itself
    out of the job when it can't (see InitializeSetup in installer/DualSenseBridge.iss)."""
    cmd = [path, "/SILENT", "/SUPPRESSMSGBOXES", "/LAUNCH=1"]
    try:
        subprocess.Popen(cmd, close_fds=True, creationflags=CREATE_BREAKAWAY_FROM_JOB)
    except OSError:
        subprocess.Popen(cmd, close_fds=True)
