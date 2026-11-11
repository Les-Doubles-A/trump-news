; Installeur Inno Setup de trump-news. Compilation : iscc installer\trump-news.iss
; Le setup est cree dans installer\Output\trump-news-Setup-0.8.1.exe
#define MyAppName "trump-news"
#define MyAppVersion "0.8.1"
#define MyAppURL "https://github.com/Les-Doubles-A/trump-news"

[Setup]
AppId={{010A6A5D-E809-5B92-944A-1F9C306917B1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Les-Doubles-A
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=Output
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\assets\icon.ico

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\app\*"; DestDir: "{app}\app"; Excludes: "__pycache__,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\assets\*"; DestDir: "{app}\assets"; Excludes: "__pycache__,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\prompts\*"; DestDir: "{app}\prompts"; Excludes: "__pycache__,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\config.example.yaml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\.env.example"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\install.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\run.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements-dev.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{app}\data"

[Icons]
Name: "{autoprograms}\Trump News"; Filename: "{app}\run.bat"; Parameters: "ui"; WorkingDir: "{app}"; IconFilename: "{app}\assets\icon.ico"
Name: "{autoprograms}\Trump News - dossier"; Filename: "{app}"

[Run]
Filename: "{app}\install.bat"; WorkingDir: "{app}"; Description: "Installer les dependances Python (Python 3.11+ requis)"; Flags: postinstall skipifsilent waituntilterminated

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\app\__pycache__"
