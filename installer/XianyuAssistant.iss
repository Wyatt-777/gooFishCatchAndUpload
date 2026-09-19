#define AppName "闲鱼铺货助手"
#define AppVersion "0.1.2"
#define AppPublisher "Xianyu Assistant Team"
#define AppExeName "XianyuAssistant.exe"

[Setup]
AppId={{62B4C2F8-29EC-4E08-921D-85EE9BB27E32}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\XianyuAssistant
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=XianyuAssistant-Setup-win64
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#AppExeName}
SetupLogging=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "..\build\installer-assets\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
Source: "..\dist\XianyuAssistant\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The database contains business data and non-secret settings. It is seeded only
; on a clean install and is deliberately retained on upgrade and uninstall.
Source: "..\build\installer-assets\xianyu_assistant.db"; DestDir: "{localappdata}\XianyuAssistant"; Flags: ignoreversion onlyifdoesntexist uninsneveruninstall
Source: "..\build\installer-assets\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "..\build\installer-assets\googlechromestandaloneenterprise64.msi"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "正在补齐 Microsoft Visual C++ 运行库…"; Flags: runhidden waituntilterminated; Check: not VCRuntimeAvailable
Filename: "{sys}\msiexec.exe"; Parameters: "/i ""{tmp}\googlechromestandaloneenterprise64.msi"" /qn /norestart"; StatusMsg: "未检测到 Chrome 或 Edge，正在安装 Chrome…"; Flags: runhidden waituntilterminated; Check: not SupportedBrowserAvailable
Filename: "{app}\{#AppExeName}"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
function SupportedBrowserAvailable: Boolean;
var
  BrowserPath: String;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe', '', BrowserPath) or
    RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe', '', BrowserPath) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe', '', BrowserPath) or
    RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe', '', BrowserPath) or
    FileExists(ExpandConstant('{pf}\Google\Chrome\Application\chrome.exe')) or
    FileExists(ExpandConstant('{pf32}\Google\Chrome\Application\chrome.exe')) or
    FileExists(ExpandConstant('{pf}\Microsoft\Edge\Application\msedge.exe')) or
    FileExists(ExpandConstant('{pf32}\Microsoft\Edge\Application\msedge.exe')) or
    FileExists(ExpandConstant('{localappdata}\Google\Chrome\Application\chrome.exe')) or
    FileExists(ExpandConstant('{localappdata}\Microsoft\Edge\Application\msedge.exe'));
end;

function VCRuntimeAvailable: Boolean;
var
  Installed: Cardinal;
begin
  Result :=
    RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) and
    (Installed = 1);
end;

function InitializeSetup: Boolean;
begin
  Result := IsWin64;
  if not Result then
    MsgBox('闲鱼铺货助手仅支持 64 位 Windows。', mbError, MB_OK);
end;
