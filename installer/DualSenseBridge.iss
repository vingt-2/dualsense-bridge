; Inno Setup script for DualSense Bridge.
; Build: run build.ps1 from the repo root (or: ISCC.exe installer\DualSenseBridge.iss).
;
; usbip-win2 and HidHide are not bundled: if missing, setup downloads the latest
; official release of each from GitHub and runs its own installer.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "DualSense Bridge"
#define AppExe "DualSenseBridge.exe"
#define RepoUrl "https://github.com/vingt-2/dualsense-bridge"
#define TaskName "DualSenseBridge"

[Setup]
AppId={{DC3D5EF8-354C-4E70-8E3B-DE9A2B00423F}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=vingt-2
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}/issues
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=DualSenseBridge-Setup-{#AppVersion}
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
Compression=lzma2
SolidCompression=yes

[Tasks]
Name: "startup"; Description: "Start {#AppName} automatically when I log in"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "get-latest-release.ps1"; Flags: dontcopy

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; Same task name the app's own "Start with Windows" menu item uses. Runs elevated so
; there is no UAC prompt at every login.
Filename: "schtasks.exe"; \
  Parameters: "/create /tn {#TaskName} /tr ""\""{app}\{#AppExe}\"""" /sc onlogon /rl highest /f"; \
  Flags: runhidden; Tasks: startup
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; \
  Flags: postinstall nowait shellexec skipifsilent
; The app's own updater installs silently with /LAUNCH=1: start the new version afterwards.
Filename: "{app}\{#AppExe}"; Flags: nowait shellexec; Check: LaunchAfterUpdate

[UninstallRun]
; Quit cleanly first: the app detaches the virtual controller and un-hides the
; Bluetooth one on exit. Killing it would leave the controller hidden from games.
Filename: "{app}\{#AppExe}"; Parameters: "--quit"; Flags: runhidden waituntilterminated; RunOnceId: "QuitApp"
Filename: "schtasks.exe"; Parameters: "/delete /tn {#TaskName} /f"; Flags: runhidden; RunOnceId: "DeleteTask"

[Code]
var
  InstalledHidHideNow: Boolean;

function LaunchAfterUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:LAUNCH|0}') = '1');
end;

function UsbipInstalled: Boolean;
begin
  Result := FileExists(ExpandConstant('{commonpf64}\USBip\usbip.exe'));
end;

function HidHideInstalled: Boolean;
begin
  Result := FileExists(ExpandConstant('{commonpf64}\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe'));
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var
  Deps: String;
begin
  Result := MemoDirInfo + NewLine + NewLine + MemoTasksInfo;
  if not UsbipInstalled then
    Deps := Deps + Space + 'usbip-win2 (virtual USB port) - github.com/vadimgrn/usbip-win2' + NewLine;
  if not HidHideInstalled then
    Deps := Deps + Space + 'HidHide (hides the Bluetooth controller from games) - github.com/nefarius/HidHide' + NewLine;
  if Deps <> '' then
    Result := Result + NewLine + NewLine +
      'Required drivers to download and install (their own setup will open):' + NewLine + Deps;
end;

// Downloads the latest release asset of Repo matching Pattern and runs its installer.
procedure InstallDependency(const Name, Repo, Pattern, FileName: String);
var
  Script, OutFile: String;
  ResultCode: Integer;
begin
  Script := ExpandConstant('{tmp}\get-latest-release.ps1');
  OutFile := ExpandConstant('{tmp}\') + FileName;
  WizardForm.StatusLabel.Caption := 'Downloading ' + Name + '...';
  if not Exec('powershell.exe',
      '-NoProfile -ExecutionPolicy Bypass -File "' + Script + '" -Repo ' + Repo +
      ' -Pattern "' + Pattern + '" -OutFile "' + OutFile + '"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) or not FileExists(OutFile) then
  begin
    MsgBox('Could not download ' + Name + '. Please install it manually from' + #13#10 +
      'https://github.com/' + Repo + '/releases/latest', mbError, MB_OK);
    Exit;
  end;
  WizardForm.StatusLabel.Caption := 'Installing ' + Name + ' (follow its setup window)...';
  Exec(OutFile, '', '', SW_SHOW, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if not UsbipInstalled or not HidHideInstalled then
      ExtractTemporaryFile('get-latest-release.ps1');
    if not UsbipInstalled then
    begin
      InstallDependency('usbip-win2', 'vadimgrn/usbip-win2', 'USBip-*-x64.exe', 'usbip-win2-setup.exe');
      if not UsbipInstalled then
        MsgBox('usbip-win2 was not installed. DualSense Bridge needs it to create the virtual wired controller.' + #13#10 +
          'Get it from https://github.com/vadimgrn/usbip-win2/releases/latest', mbInformation, MB_OK);
    end;
    if not HidHideInstalled then
    begin
      InstallDependency('HidHide', 'nefarius/HidHide', 'HidHide_*_x64.exe', 'hidhide-setup.exe');
      InstalledHidHideNow := HidHideInstalled;
      if not InstalledHidHideNow then
        MsgBox('HidHide was not installed. Without it, games may see the controller twice.' + #13#10 +
          'Get it from https://github.com/nefarius/HidHide/releases/latest', mbInformation, MB_OK);
    end;
  end;
end;

// HidHide installs a filter driver on HID devices; offer a restart so it is active
// for devices that were already connected.
function NeedRestart: Boolean;
begin
  Result := InstalledHidHideNow;
end;

// Upgrading: ask a running copy to quit cleanly before its exe is replaced.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Exe: String;
  ResultCode: Integer;
begin
  Result := '';
  Exe := ExpandConstant('{app}\{#AppExe}');
  if FileExists(Exe) then
    Exec(Exe, '--quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
