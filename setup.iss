#define AppName "WinMacOS"
#define AppVersion "4.0.0"
#define AppPublisher "emxts-hub"
#define AppExeName "WinMacOS.exe"

[Setup]
AppId={{B9B4F2A1-5F7D-4D5E-A9CF-7F50A2D6B1A8}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=dist
OutputBaseFilename=WinMacOS-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExeName}

[Files]
Source: "dist\WinMacOS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Offline-only release build: no Firebase service account JSON is bundled.
; The workflow removes any legacy credential file before packaging.

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
