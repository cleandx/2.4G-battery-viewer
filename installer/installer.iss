; -*- conf -*-
; 2.4G设备电量 安装包脚本（由 build_installer.py 生成版本号后编译）
; 编译：ISCC installer.iss  （一般用 python build_installer.py 一条龙）
;
; 安装形态：每用户安装（默认装到 LocalAppData\Programs，无需管理员权限，
; 程序的配置/devices.json 写在 exe 旁边，选可写目录就不会有权限问题）。

#define MyAppName "2.4G设备电量"
#define MyAppNameEn "2.4G Device Battery Check"
#define MyAppVersion "__VERSION__"
#define MyAppPublisher "2.4G Battery Viewer"
#define MyAppExeName "2.4G设备电量.exe"
#define MyAppId "{{8D9A5C31-6B4E-4F2A-9C7D-0E1F2A3B4C5D}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=2.4G设备电量-Setup-v{#MyAppVersion}
SetupIconFile=..\assets\app.ico
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; 安装/卸载时程序可能正在跑：静默关掉比让文件占用报错友好
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chs"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\2.4G设备电量.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\注意事项.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清运行期的临时文件；devices.json / tray_config.json 是用户数据，卸载保留
Type: files; Name: "{app}\tray_pid.txt"
