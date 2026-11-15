[Setup]
AppId=VeTay-Zaizai
AppName=Ve Tay
AppVersion=0.2.7
DefaultDirName={localappdata}\Programs\VeTay
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
OutputDir=dist
OutputBaseFilename=VeTay-Windows-Setup
Compression=lzma2/fast
CloseApplications=yes

[Files]
Source: "patch_payload\app.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "patch_payload\version.txt"; DestDir: "{app}"; Flags: ignoreversion

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app.py"""; WorkingDir: "{app}"; Flags: nowait

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Dir: String;
  InstalledVersion: AnsiString;
begin
  Result := '';
  Dir := ExpandConstant('{app}');
  if not FileExists(Dir + '\runtime\pythonw.exe') or
     not FileExists(Dir + '\engine\handdraw\render.py') or
     not FileExists(Dir + '\ffmpeg.exe') or
     not FileExists(Dir + '\app.py') then
    Result := 'Chon thu muc da cai Ve Tay 0.2.6.'
  else if not LoadStringFromFile(Dir + '\version.txt', InstalledVersion) then
    Result := 'Khong doc duoc phien ban Ve Tay.'
  else if (Trim(String(InstalledVersion)) <> '0.2.6') and
          (Trim(String(InstalledVersion)) <> '0.2.7') then
    Result := 'Ban va nay danh cho Ve Tay 0.2.6 hoac 0.2.7.';
end;
