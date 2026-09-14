#define AppName "AS/400 Quantum Suite"
#define AppVersion "4.1.0"
#define AppPublisher "emxts-hub"
#define AppExeName "AS400QuantumSuite.exe"
#define AppBundleName "AS400QuantumSuite"

[Setup]
AppId={{B9B4F2A1-5F7D-4D5E-A9CF-7F50A2D6B1A8}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\AS400QuantumSuite
DefaultGroupName={#AppName}
OutputDir=dist
OutputBaseFilename=AS400QuantumSuite-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExeName}

[Files]
Source: "dist\{#AppBundleName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Offline-only release build: no Firebase service account JSON is bundled.
; The workflow removes any legacy credential file before packaging.

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
