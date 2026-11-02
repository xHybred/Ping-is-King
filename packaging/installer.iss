; Inno Setup script. Run after pyinstaller packaging/build.spec.
;   iscc packaging\installer.iss

#define AppName "COD Ping King"
#define AppVersion "1.3.4"
#define AppExe "CODPingKing.exe"

; Override with iscc /DDistDir=..\dist-next when the default build is locked by a running copy.
#ifndef DistDir
  #define DistDir "..\dist"
#endif

[Setup]
AppId={{7E3B9C41-5B0A-4F2D-9C63-2D9A1E5B7C11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=COD Ping King
SetupIconFile=icon.ico
DefaultDirName={autopf}\COD Ping King
DefaultGroupName={#AppName}
OutputDir={#DistDir}\installer
OutputBaseFilename=COD-Ping-King-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
; Setup stops the WinDivert service before copying, but refuse to run against
; a live window as well so the user gets a clear message instead of a half copy.
AppMutex=Local\CODPingKing.SingleInstance
CloseApplications=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
UsePreviousAppDir=no
VersionInfoVersion=1.3.4.0
VersionInfoDescription={#AppName} setup

[InstallDelete]
; Renamed from CoD Region Filter; remove the old copy so two versions cannot run.
Type: filesandordirs; Name: "{autopf}\CoD Region Filter"
Type: files; Name: "{group}\CoD Region Filter.lnk"
Type: files; Name: "{autodesktop}\CoD Region Filter.lnk"

[Files]
; restartreplace is the fallback for WinDivert64.sys: if the service could not
; be stopped, the driver is swapped at reboot instead of failing the install.
Source: "{#DistDir}\{#AppName}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion restartreplace

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent shellexec

[Code]
{ WinDivert keeps WinDivert64.sys mapped while its kernel service exists, so a
  plain file replace fails with DeleteFile code 5. Setup is already elevated. }
procedure StopOneDriver(const ServiceName: String);
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\sc.exe'), 'stop ' + ServiceName, '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1200);
  Exec(ExpandConstant('{sys}\sc.exe'), 'delete ' + ServiceName, '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure StopDriverService();
begin
  StopOneDriver('WinDivert');
  StopOneDriver('WinDivert1.4');
  Sleep(300);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    StopDriverService();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
    StopDriverService();

  if CurUninstallStep = usPostUninstall then
  begin
    if MsgBox('Also remove your settings, server catalog and match history?',
      mbConfirmation, MB_YESNO) = IDYES then
    begin
      DataDir := ExpandConstant('{userappdata}\cod-ping-king');
      DelTree(DataDir, True, True, True);
      DataDir := ExpandConstant('{localappdata}\cod-ping-king');
      DelTree(DataDir, True, True, True);
      DataDir := ExpandConstant('{userappdata}\cod-region-filter');
      DelTree(DataDir, True, True, True);
      DataDir := ExpandConstant('{localappdata}\cod-region-filter');
      DelTree(DataDir, True, True, True);
    end;
  end;
end;



