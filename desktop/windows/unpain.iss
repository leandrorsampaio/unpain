; Inno Setup 6 script for the UnPAIN Windows installer. desktop/build.py runs it with the
; /D values below; it does not need editing for a release.
;
; Per-user install (no administrator prompt) into %LOCALAPPDATA%\Programs\UnPAIN.
; The data lives in %USERPROFILE%\UnPAIN, so uninstalling or upgrading never touches it.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef DisplayVersion
  #define DisplayVersion AppVersion
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\pyinstaller\UnPAIN"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef OutputName
  #define OutputName "UnPAIN-setup"
#endif
#ifndef IconFile
  #define IconFile "..\..\build\desktop\unpain.ico"
#endif

[Setup]
; Never change AppId: Windows uses it to recognise an upgrade of the same app.
AppId={{33E3F1BD-E8DF-4F45-A9CD-E5B7D8FA80F3}
AppName=UnPAIN
AppVersion={#AppVersion}
AppVerName=UnPAIN {#DisplayVersion}
AppPublisher=UnPAIN
AppPublisherURL=https://github.com/leandrorsampaio/unpain
AppSupportURL=https://github.com/leandrorsampaio/unpain/issues
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\UnPAIN
DefaultGroupName=UnPAIN
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; The launcher holds this mutex while it runs, so setup asks to close UnPAIN first.
AppMutex=UnPAINDesktop
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\UnPAIN.exe
UninstallDisplayName=UnPAIN
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=auto

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; An upgrade replaces the bundle as a whole; files a newer version no longer ships must go.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\UnPAIN"; Filename: "{app}\UnPAIN.exe"
Name: "{autodesktop}\UnPAIN"; Filename: "{app}\UnPAIN.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\UnPAIN.exe"; Description: "{cm:LaunchProgram,UnPAIN}"; Flags: nowait postinstall skipifsilent
