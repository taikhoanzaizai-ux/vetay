#define AppName "Ve Tay"
#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
[Setup]
AppId=VeTay-Zaizai
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\VeTay
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
OutputDir=dist
OutputBaseFilename=VeTay-Windows-Setup
Compression=lzma2/fast
SolidCompression=no
CloseApplications=yes
UninstallDisplayIcon={app}\VeTay.ico
[Files]
Source: "bundle\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
[Icons]
Name: "{autoprograms}\Ve Tay"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app.py"""; WorkingDir: "{app}"; IconFilename: "{app}\VeTay.ico"
Name: "{autodesktop}\Ve Tay"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app.py"""; WorkingDir: "{app}"; IconFilename: "{app}\VeTay.ico"
[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app.py"""; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent; Check: not IsVetayUpdate

Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app.py"""; WorkingDir: "{app}"; Flags: nowait; Check: IsVetayUpdate

[Code]
function IsVetayUpdate: Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/RESTARTVETAY') = 0 then
    begin
      Result := True;
      Exit;
    end;
end;
