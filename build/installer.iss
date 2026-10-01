; Inno Setup script - compiled by crear_instalador.bat after PyInstaller.
; Per-user install (no administrator prompt): %LOCALAPPDATA%\Programs\Zak_light

#define AppName "Zak_light"
#ifndef AppVersion
  #define AppVersion "1.2.0"   ; crear_instalador.bat passes the real one from src/version.py (/DAppVersion=...)
#endif
#define AppExe "Zak_light.exe"

[Setup]
AppId={{6F2B7C1E-4D53-4B0A-9C7E-5A1D3E8F2B40}
AppName={#AppName}
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
AppPublisher=Zak
AppPublisherURL=https://github.com/Zak208/Zak_Light
AppSupportURL=https://github.com/Zak208/Zak_Light/issues
AppUpdatesURL=https://github.com/Zak208/Zak_Light/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
; Installs for the current user without asking for administrator rights; the first page offers "all users" too
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=Zak_light_Setup
SetupIconFile=zak_light.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardImageFile=wizard-side.bmp,wizard-side@2x.bmp
WizardSmallImageFile=wizard-small.bmp,wizard-small@2x.bmp
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; The engine has no window (tray only), so Windows' Restart Manager cannot be relied on to find it:
; [Code] below closes it cleanly (LEDs off) before any file is replaced or removed.
CloseApplications=no
; The questions: where to install, whether to start with Windows, and a summary. The shortcuts (desktop and
; Start menu) are always created, so there is no Start-menu-folder page.
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=yes
DisableReadyPage=no
WizardStyle=modern

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "startup"; Description: "&Iniciar {#AppName} con Windows, en segundo plano"; GroupDescription: "Al encender el PC:"

[Files]
Source: "..\dist\Zak_light\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"

[Registry]
; Start with Windows, hidden in the tray. Removed on uninstall.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; \
    ValueData: """{app}\{#AppExe}"" --minimized"; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "Abrir {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
{ Closes a running Zak_light: first politely (--quit turns the LEDs off), then by force for anything left
  (the window process, or an engine that is stuck). The installer itself is Zak_light_Setup.exe, so it is not affected. }
procedure CloseRunningApp();
var
  ResultCode: Integer;
  Exe: String;
begin
  Exe := ExpandConstant('{app}\{#AppExe}');
  if FileExists(Exe) then
    Exec(Exe, '--quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1500);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(500);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  CloseRunningApp();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  CloseRunningApp();
  Result := True;
end;

{ The user's calibration (LED positions, wiring), profiles and log live in %APPDATA%\Zak_light.
  They are only removed if the user says so. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
    if MsgBox('¿Quieres borrar también tus ajustes, perfiles y registro de {#AppName}?' + #13#10 +
              'Si piensas volver a instalarlo, conviene conservarlos.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{userappdata}\{#AppName}'), True, True, True);
end;
