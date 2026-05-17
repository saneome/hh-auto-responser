#define AppName "HH Auto Response"
#define AppVersion "1.0.0"
#define AppPublisher "hh-auto-response"
#define AppURL "https://github.com/saneome/hh-auto-responser"
#define AppExeName "hh-auto-response.exe"

[Setup]
AppId={{B8F3D7A1-692C-4E9A-BF45-3C7A12D8E6F1}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
LicenseFile=LICENSE
OutputDir=installer-output
OutputBaseFilename=hh-auto-response-setup-{#AppVersion}
SetupIconFile=icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#AppName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\hh-auto-response\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "config.example.yaml"; DestDir: "{app}"; Flags: ignoreversion
Source: ".env.example"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "--gui"; IconFilename: "{app}\_internal\icon.ico"; Flags: dontcloseonexit
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "--gui"; IconFilename: "{app}\_internal\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\_internal\playwright\driver\node.exe"; Parameters: """{app}\_internal\playwright\driver\package\cli.js"" install chromium"; StatusMsg: "Installing Chromium browser..."; Flags: runhidden runascurrentuser waituntilterminated
Filename: "{app}\{#AppExeName}"; Parameters: "--gui"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandorsubdirs; Name: "{app}"
