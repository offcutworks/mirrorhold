; The setup program of a MirrorHold release, compiled by Inno Setup 7 from the assembled
; release folder the same build zips, after the zip (tools/build_release.py, which passes
; the defines below). It adds a way to install the release and changes nothing of it: the
; folder it installs holds the zip's files byte for byte, plus the uninstaller Inno Setup
; writes beside them.
;
; Install: for the user alone, without administrator rights, into the folder the user chooses,
; %LOCALAPPDATA%\Programs\MirrorHold unless they choose another; then, on a first install, the
; copy sets itself up as Set up does in a portable release (python -m mirrorhold install): its
; Start menu entry and its scheduled tasks. The setup program makes no shortcut, file
; association or Start menu entry of its own. A refusal by the program leaves no copy behind.
;
; The folder: a first install shows the folder page, which also shows a folder named by /DIR=
; (which winget install --location passes) or by a Dir in the file /LOADINF= names. A refused
; install and the uninstall both delete the folder with everything in it, so a first install
; takes only a folder that holds nothing of the user's and stays reachable: a full path on a
; drive Windows counts as fixed (a USB disk it counts so passes), not a drive's root, empty or
; not there yet, with nothing of the user's beside it under the name an update sets a folder
; aside by, where the user can write, and outside the folder that holds the jobs and their
; records, judged on the folder as Windows resolves it (a short name, a junction, a
; substituted drive). The page refuses any other when it is left; a silent install, which
; shows no page, refuses it at the start, before anything changes, and Setup ends with Inno
; Setup's status for a setup that did not initialize, 1.
;
; Update: the copy stays in the folder it was installed in, which Windows' list of installed
; apps records and the copy's setup names, and no folder page is shown; told any other folder,
; the setup program refuses at the start, as above. A newer setup program moves the copy's
; folder aside, installs the newer files into a fresh folder, then deletes the old one, so no
; file of the older release stays; the copy keeps its setup, which names the folder, and
; nothing is set up again. Should the update not finish, the old folder is put back.
;
; Uninstall, from Windows' list of installed apps: the copy removes its setup only if it is
; the one set up (python -m mirrorhold uninstall --if-set-up-here), so another copy's
; automatic runs stay; then the folder goes. A refusal by the program removes nothing. A copy
; that cannot run at all (python -m mirrorhold installation, which answers 0 whenever it
; runs, fails) is removed anyway, and the user is told what may be left. The jobs, their
; records and every backup stay.
;
; At work: install, update and uninstall are refused, before anything changes, while any
; process runs from the copy: its window, a run, a verification, a repair, the icon one of
; them shows. Every such process loads the copy's interpreter, and Windows refuses to open a
; loaded program file for writing, which is the test (measured on the development machine,
; 2026-11-01: a folder a process runs from can still be renamed unless it is that process's
; working folder, so a rename is no test). Inno Setup's own offer to close applications is
; switched off: the program's work is never cut short by its setup.
;
; Words: the program speaks for itself; a refusal shows the line the program printed.

#ifndef Release
  #error Release must name the assembled release folder
#endif
#ifndef Label
  #error Label must be the release's label, YEAR.MONTH.N
#endif
#ifndef TouchDate
  #error TouchDate must be the first day of the label's month, YYYY-MM-DD
#endif
#ifndef Copyright
  #error Copyright must be the licence's copyright line
#endif

; The identity Windows knows the installation by, generated once: an update finds the
; installation it replaces by it, so it never changes.
#define AppGuid "D1AB086A-4E20-49D0-A170-A7D64B8AEEE2"

; Inno Setup names an imported function function@library; the at sign is written through At
; so that this file holds nothing a text check would read as an e-mail address.
#define At "@"

[Setup]
AppId={{{#AppGuid}}
AppName=MirrorHold
AppVersion={#Label}
AppVerName=MirrorHold {#Label}
AppPublisher=offcutworks
AppCopyright={#Copyright}
VersionInfoVersion={#Label}.0
VersionInfoProductTextVersion={#Label}
VersionInfoCompany=offcutworks
VersionInfoProductName=MirrorHold
VersionInfoDescription=MirrorHold setup
UninstallDisplayName=MirrorHold
UninstallDisplayIcon={app}\mirrorhold.ico
SetupIconFile={#Release}\mirrorhold.ico
PrivilegesRequired=lowest
SetupArchitecture=x64
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
DefaultDirName={autopf}\MirrorHold
UsePreviousAppDir=yes
DisableDirPage=auto
DirExistsWarning=no
AllowRootDirectory=no
AllowNetworkDrive=no
AllowUNCPath=no
DisableProgramGroupPage=yes
DisableWelcomePage=yes
CloseApplications=no
RestartApplications=no
WizardStyle=modern
ShowLanguageDialog=no
Compression=lzma2/max
SolidCompression=yes
; Every file carries midnight UTC on the first day of the label's month: Inno Setup reads
; TouchDate and TouchTime as UTC under TimeStampsInUTC=yes, its default in Inno Setup 7, set
; here so that no other default changes it; every build and every installation then stamps
; the same moment. The zip's files carry that day's midnight with no
; time zone, which Windows reads as local time, so the two show alike only where local time is
; UTC.
TouchDate={#TouchDate}
TouchTime=00:00:00
TimeStampsInUTC=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Messages]
ReadyLabel1=Setup is ready to install [name] on this computer.
ReadyLabel2a=Choose Install to go on.
CannotContinue=Nothing has changed. Choose Back to try again once it is done, or Cancel to leave Setup.
ConfirmUninstall=Remove %1 from this computer?%n%nYour jobs, their records and your backups are kept.
UninstalledAll=%1 was removed from this computer. Your jobs and their records stay in your AppData\Local\MirrorHold folder, and your backups where they are.

[Files]
Source: "{#Release}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs touch

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Run]
Filename: "{app}\MirrorHold.exe"; Description: "Open MirrorHold"; Flags: postinstall nowait skipifsilent; Check: OfferOpening

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{#AppGuid}}_is1';
  AsideSuffix = '.previous';
  GenericWrite = $40000000;
  OpenExisting = 3;
  BackupSemantics = $02000000;  // what CreateFile needs to open a folder
  ShareAll = 7;
  DriveFixed = 3;           // GetDriveType's answer for a drive Windows counts as fixed
  Patience = 5000;          // ms a file or folder briefly held (a virus scanner) is waited for
  RefusedExitCode = 20;     // the program refused to be set up; nothing was left installed
  AtWork = 'MirrorHold is at work: its window, or a run, a verification or a repair started from this copy, ' +
           'or the icon one of them shows in the notification area, is still open. Close the window, and let ' +
           'the work end or cancel it.';

var
  PreviousLabel: String;    // the release this setup replaces; '' on a first install
  PreviousFolder: String;   // the folder the copy this setup replaces lies in; '' on a first install
  Updating: Boolean;
  MovedAside: Boolean;
  Completed: Boolean;
  Refusal: String;          // the program's words when it refused to be set up
  Warnings: String;         // what else the program said while it set itself up: warnings, notes

function CreateFile(lpFileName: String; dwDesiredAccess, dwShareMode: Cardinal; lpSecurityAttributes: NativeInt;
  dwCreationDisposition, dwFlagsAndAttributes: Cardinal; hTemplateFile: NativeInt): NativeInt;
  external 'CreateFileW{#At}kernel32.dll stdcall';
function CloseHandle(hObject: NativeInt): Boolean;
  external 'CloseHandle{#At}kernel32.dll stdcall';
function GetDriveType(lpRootPathName: String): Cardinal;
  external 'GetDriveTypeW{#At}kernel32.dll stdcall';
function GetFinalPathNameByHandle(hFile: NativeInt; lpszFilePath: String; cchFilePath, dwFlags: Cardinal): Cardinal;
  external 'GetFinalPathNameByHandleW{#At}kernel32.dll stdcall';

function AppFolder: String;
begin
  Result := ExpandConstant('{app}');
end;

function AsideFolder: String;
begin
  Result := AppFolder + AsideSuffix;
end;

// Whether a program file is loaded by a running process: Windows then refuses to open it
// for writing. Opening and closing it changes nothing.
function Loaded(const Path: String): Boolean;
var
  Handle: NativeInt;
begin
  Handle := CreateFile(Path, GenericWrite, 0, 0, OpenExisting, 0, 0);
  Result := Handle = -1;
  if not Result then
    CloseHandle(Handle);
end;

function AnyLoaded(const Folder, Pattern: String): Boolean;
var
  Found: TFindRec;
begin
  Result := False;
  if FindFirst(AddBackslash(Folder) + Pattern, Found) then
    try
      repeat
        if (Found.Attributes and FILE_ATTRIBUTE_DIRECTORY = 0) and Loaded(AddBackslash(Folder) + Found.Name) then
          Result := True;
      until Result or not FindNext(Found);
    finally
      FindClose(Found);
    end;
end;

function EngineLoaded(const App: String): Boolean;
var
  Found: TFindRec;
begin
  Result := False;
  if FindFirst(App + '\vendor\*', Found) then
    try
      repeat
        if (Found.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0) and (Found.Name <> '.') and (Found.Name <> '..') then
          Result := AnyLoaded(App + '\vendor\' + Found.Name, '*.exe');
      until Result or not FindNext(Found);
    finally
      FindClose(Found);
    end;
end;

// The interpreter's files, the launcher and the engine. The uninstaller beside them is left
// out: it runs while it asks.
function LoadedNow(const App: String): Boolean;
begin
  Result := AnyLoaded(App + '\runtime', '*.dll') or AnyLoaded(App + '\runtime', '*.exe') or
            (FileExists(App + '\MirrorHold.exe') and Loaded(App + '\MirrorHold.exe')) or EngineLoaded(App);
end;

// Whether any process runs from the copy in App. A file held for a moment by another
// program (a virus scanner) is waited for; one a process has loaded stays held.
function CopyAtWork(const App: String): Boolean;
var
  Waited: Integer;
begin
  Waited := 0;
  Result := LoadedNow(App);
  while Result and (Waited < Patience) do begin
    Sleep(500);
    Waited := Waited + 500;
    Result := LoadedNow(App);
  end;
end;

function Moved(const Source, Target: String): Boolean;
var
  Waited: Integer;
begin
  Waited := 0;
  Result := RenameFile(Source, Target);
  while not Result and (Waited < Patience) do begin
    Sleep(500);
    Waited := Waited + 500;
    Result := RenameFile(Source, Target);
  end;
end;

function Joined(const Lines: TArrayOfString): String;
var
  I: Integer;
begin
  Result := '';
  for I := 0 to GetArrayLength(Lines) - 1 do
    if Trim(Lines[I]) <> '' then begin
      if Result <> '' then
        Result := Result + #13#10;
      Result := Result + Trim(Lines[I]);
    end;
end;

// The program's own command line in the copy at App: its words come back as UTF-8.
function RunProgram(const App, Command: String; var ResultCode: Integer; var Output: TExecOutput): Boolean;
begin
  try
    Result := ExecAndCaptureOutput(App + '\runtime\python.exe', '-X utf8 -m mirrorhold ' + Command, App,
                                   SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode, Output);
  except
    Log(GetExceptionMessage);
    Result := False;
  end;
end;

// The folder the command line names for the install, '' where it names none: /DIR=, then a
// Dir in the file /LOADINF= names, which Inno Setup lets override it; either may start with
// expand:, whose constants Inno Setup expands. A value that cannot be expanded stays as
// written, which is no full path and no installed copy's folder, and is refused.
function RequestedFolder: String;
var
  I: Integer;
  Param, Inf: String;
begin
  Result := '';
  Inf := '';
  for I := 1 to ParamCount do begin
    Param := ParamStr(I);
    if CompareText(Copy(Param, 1, 5), '/DIR=') = 0 then
      Result := Copy(Param, 6, Length(Param))
    else if CompareText(Copy(Param, 1, 9), '/LOADINF=') = 0 then
      Inf := Copy(Param, 10, Length(Param));
  end;
  if Inf <> '' then
    Result := GetIniString('Setup', 'Dir', Result, ExpandFileName(Inf));
  if CompareText(Copy(Result, 1, 7), 'expand:') = 0 then
    try
      Result := ExpandConstant(Copy(Result, 8, Length(Result)));
    except
      Log(GetExceptionMessage);
    end;
end;

// Folders compared as written, without regard to case or a final backslash: the same folder
// reached another way (a short name, a junction) is refused too, and the refusal's remedy,
// naming no folder at all, updates it.
function SameFolder(const A, B: String): Boolean;
begin
  Result := CompareText(RemoveBackslashUnlessRoot(ExpandFileName(A)), RemoveBackslashUnlessRoot(ExpandFileName(B))) = 0;
end;

// Whether Inner is Outer or lies inside it, compared as written, without regard to case.
function Within(const Inner, Outer: String): Boolean;
var
  I, O: String;
begin
  I := AddBackslash(ExpandFileName(Inner));
  O := AddBackslash(ExpandFileName(Outer));
  Result := CompareText(Copy(I, 1, Length(O)), O) = 0;
end;

function HoldsAnything(const Folder: String): Boolean;
var
  Found: TFindRec;
begin
  Result := False;
  if FindFirst(AddBackslash(Folder) + '*', Found) then
    try
      repeat
        Result := (Found.Name <> '.') and (Found.Name <> '..');
      until Result or not FindNext(Found);
    finally
      FindClose(Found);
    end;
end;

// The nearest folder of Path that exists, and the rest of Path below it ('' where Path exists).
procedure SplitExisting(const Path: String; var Existing, Rest: String);
begin
  Existing := RemoveBackslashUnlessRoot(Path);
  Rest := '';
  while not DirExists(Existing) and (Length(Existing) > 3) do begin
    if Rest = '' then
      Rest := ExtractFileName(Existing)
    else
      Rest := ExtractFileName(Existing) + '\' + Rest;
    Existing := ExtractFileDir(Existing);
  end;
end;

// Path as Windows resolves it — a short name expanded, a junction, a link or a substituted
// drive followed — for the part of it that exists, the rest appended as written; a network
// target comes back as \\server\share. A part Windows cannot open stays as written.
function Resolved(const Path: String): String;
var
  Existing, Rest, Buffer: String;
  Handle: NativeInt;
  Count: Cardinal;
begin
  SplitExisting(RemoveBackslashUnlessRoot(ExpandFileName(Path)), Existing, Rest);
  Buffer := Existing;
  Handle := CreateFile(Existing, 0, ShareAll, 0, OpenExisting, BackupSemantics, 0);
  if Handle <> -1 then
    try
      SetLength(Buffer, 1024);
      Count := GetFinalPathNameByHandle(Handle, Buffer, 1024, 0);
      if (Count > 0) and (Count < 1024) then begin
        SetLength(Buffer, Count);
        if CompareText(Copy(Buffer, 1, 8), '\\?\UNC\') = 0 then
          Buffer := '\\' + Copy(Buffer, 9, Length(Buffer))
        else if Copy(Buffer, 1, 4) = '\\?\' then
          Buffer := Copy(Buffer, 5, Length(Buffer));
      end
      else
        Buffer := Existing;
    finally
      CloseHandle(Handle);
    end;
  Result := RemoveBackslashUnlessRoot(Buffer);
  if Rest <> '' then
    Result := AddBackslash(Result) + Rest;
end;

// Why the name beside Path that an update sets a folder aside by is in the way, '' where it is
// not. An empty folder, or one holding a MirrorHold release — what an earlier setup program
// left when it could not delete it — is deleted before the install, as an update does; any
// other is the user's.
function AsideRefusal(const Path: String): String;
var
  Aside: String;
begin
  Result := '';
  Aside := RemoveBackslashUnlessRoot(Path) + AsideSuffix;
  if FileExists(Aside) or (DirExists(Aside) and HoldsAnything(Aside) and
     not (FileExists(Aside + '\build.json') and FileExists(Aside + '\MirrorHold.exe'))) then
    Result := Aside + ' lies beside ' + RemoveBackslashUnlessRoot(Path) + '. An update moves the folder it replaces ' +
              'to that name and deletes it afterwards, so nothing of yours may be there under that name: move it, ' +
              'or choose another folder.';
end;

// Why a first install cannot take Folder, '' where it can. A refused install and the
// uninstall delete the folder with everything in it, and the jobs' automatic runs start the
// copy from it, so the folder must hold nothing of the user's and stay reachable; what it is
// and where it lies are judged on the folder as Windows resolves it. Whether the user can
// write there is tried last, by making a folder of its own in the nearest folder that exists
// and removing it.
function FolderRefusal(const Folder: String): String;
var
  Path, Real, Data, Parent, Rest, Probe: String;
  Waited: Integer;
begin
  Result := '';
  Path := Trim(Folder);
  StringChangeEx(Path, '/', '\', True);
  Path := RemoveBackslashUnlessRoot(Path);
  if (Length(ExtractFileDrive(Path)) <> 2) or (Copy(Path, 3, 1) <> '\') then begin
    Result := Path + ' is not a full path that starts with a drive letter, such as C:\Apps\MirrorHold.';
    Exit;
  end;
  Path := RemoveBackslashUnlessRoot(ExpandFileName(Path));
  Real := Resolved(Path);
  if (Length(Path) <= 3) or (Length(Real) <= 3) then begin
    Result := Path + ' is the root of a drive. Removing MirrorHold deletes its folder whole, so it needs a folder ' +
              'of its own.';
    Exit;
  end;
  if (Length(ExtractFileDrive(Real)) <> 2) or (GetDriveType(ExtractFileDrive(Real) + '\') <> DriveFixed) then begin
    Result := Path + ' is on a drive Windows counts as removable, network or optical. Your jobs'' automatic runs ' +
              'start MirrorHold from its folder, so it is installed only on a drive Windows counts as fixed.';
    Exit;
  end;
  Data := ExpandConstant('{localappdata}\MirrorHold');
  if Within(Real, Resolved(Data)) then begin
    Result := Path + ' lies in ' + Data + ', which keeps your jobs and their records. Removing MirrorHold deletes ' +
              'its folder whole, so it is installed outside that folder.';
    Exit;
  end;
  if FileExists(Path) then begin
    Result := Path + ' is a file, not a folder.';
    Exit;
  end;
  if DirExists(Path) and HoldsAnything(Path) then begin
    Result := Path + ' already holds files or folders. A refused install and removing MirrorHold delete its ' +
              'folder whole, so it is installed only into a new or an empty folder.';
    Exit;
  end;
  Result := AsideRefusal(Path);
  if Result <> '' then
    Exit;
  SplitExisting(Path, Parent, Rest);
  Probe := AddBackslash(Parent) + 'MirrorHold setup ' + GetDateTimeString('yyyymmddhhnnss', #0, #0) + ' ' +
           IntToStr(Random(1000000));
  if not CreateDir(Probe) then begin
    Result := 'Setup cannot create a folder in ' + Parent + '. MirrorHold is installed for you alone, without ' +
              'an administrator''s rights, so it goes only where you can write.';
    Exit;
  end;
  Waited := 0;
  while not RemoveDir(Probe) and (Waited < Patience) do begin
    Sleep(500);
    Waited := Waited + 500;
  end;
  if DirExists(Probe) then
    Log('The folder made to try writing could not be removed: ' + Probe);
end;

// An update stays in the copy's folder, and a command line naming another is refused. A
// first install shows the folder page, which refuses a folder it cannot take; a silent one
// shows none, so the folder it would take, named or not, is tried here.
function InitializeSetup: Boolean;
var
  Default, Requested, Folder, Words: String;
begin
  Default := ExpandConstant('{#SetupSetting("DefaultDirName")}');
  Updating := RegQueryStringValue(HKCU, UninstallKey, 'DisplayVersion', PreviousLabel);
  if Updating and not RegQueryStringValue(HKCU, UninstallKey, 'Inno Setup: App Path', PreviousFolder) then
    PreviousFolder := Default;
  Requested := RequestedFolder;
  Words := '';
  if Updating then begin
    if (Requested <> '') and not SameFolder(Requested, PreviousFolder) then
      Words := 'MirrorHold is installed in ' + PreviousFolder + ', and an update replaces it there: its Start menu ' +
               'entry and its scheduled tasks start it from that folder. Setup was asked to install it into ' +
               Requested + ' instead, so nothing was changed.' + #13#10#13#10 +
               'Run Setup again without choosing a folder (no /DIR=, no winget --location). To move MirrorHold, ' +
               'remove it from Windows'' list of installed apps first, then install it where you want it.';
  end
  else if WizardSilent then begin
    Folder := Requested;
    if Folder = '' then
      Folder := Default;
    Words := FolderRefusal(Folder);
    if Words <> '' then
      Words := 'MirrorHold was not installed, and nothing has changed.' + #13#10#13#10 + Words;
  end;
  if Words <> '' then begin
    Log(Words);
    SuppressibleMsgBox(Words, mbError, MB_OK, IDOK);
    Result := False;
    Exit;
  end;
  Result := True;
end;

// The folder page is left only for a folder a first install can take.
function NextButtonClick(CurPageID: Integer): Boolean;
var
  Words: String;
begin
  Result := True;
  if (CurPageID <> wpSelectDir) or Updating then
    Exit;
  Words := FolderRefusal(WizardDirValue);
  if Words <> '' then begin
    Log(Words);
    SuppressibleMsgBox(Words + #13#10#13#10 + 'Choose another folder.', mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Text: String;
begin
  if (CurPageID = wpReady) and Updating then begin
    WizardForm.PageNameLabel.Caption := 'Ready to Update';
    WizardForm.PageDescriptionLabel.Caption := 'Setup is ready to update MirrorHold on this computer.';
    WizardForm.ReadyLabel.Caption := 'Choose Update to go on.';
    WizardForm.NextButton.Caption := '&Update';
  end
  else if CurPageID = wpFinished then begin
    if Refusal <> '' then begin
      WizardForm.FinishedHeadingLabel.Caption := 'MirrorHold was not installed';
      Text := 'It could not be set up, so nothing was left installed:' + #13#10#13#10 + Refusal;
    end
    else if Updating then begin
      WizardForm.FinishedHeadingLabel.Caption := 'MirrorHold is updated';
      Text := 'MirrorHold {#Label} has replaced MirrorHold ' + PreviousLabel + '. Whether it is set up stays as it was.';
    end
    else begin
      WizardForm.FinishedHeadingLabel.Caption := 'MirrorHold is installed and set up';
      Text := 'Its Start menu entry opens it, and your jobs'' automatic runs start it from ' + AppFolder + '.';
      if Warnings <> '' then
        Text := Text + #13#10#13#10 + Warnings;
    end;
    WizardForm.FinishedLabel.Caption := Text;
    WizardForm.FinishedLabel.AdjustHeight;
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo,
  MemoGroupInfo, MemoTasksInfo: String): String;
begin
  if Updating then
    Result := 'MirrorHold {#Label} replaces MirrorHold ' + PreviousLabel + ' in' + NewLine + Space + AppFolder +
              NewLine + 'Whether it is set up stays as it is.'
  else
    Result := 'MirrorHold {#Label} is installed for you alone, without an administrator''s password, in' + NewLine +
              Space + AppFolder + NewLine + 'and set up: a MirrorHold entry in the Start menu, and the scheduled ' +
              'tasks your jobs'' automatic runs need.';
  Result := Result + NewLine + NewLine + 'Your jobs, their records and your backups are not touched.';
end;

procedure InitializeWizard;
begin
  WizardForm.ReadyMemo.WordWrap := True;
  WizardForm.ReadyMemo.ScrollBars := ssVertical;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  // On a first install the folder page refused a folder of that name holding what is the
  // user's; one that has appeared since is left alone too.
  if not Updating then begin
    Result := AsideRefusal(AppFolder);
    if Result <> '' then
      Exit;
  end;
  if DirExists(AsideFolder) and not DelTree(AsideFolder, True, True, True) then begin
    Result := 'A folder an earlier setup of MirrorHold left behind cannot be removed: ' + AsideFolder +
              '. Delete it, then try again.';
    Exit;
  end;
  if not DirExists(AppFolder) then
    Exit;
  if CopyAtWork(AppFolder) then begin
    Result := AtWork;
    Exit;
  end;
  if not Moved(AppFolder, AsideFolder) then begin
    Result := 'The folder of the MirrorHold this setup replaces cannot be moved aside: ' + AppFolder +
              '. Close whatever has it open, then try again.';
    Exit;
  end;
  // A process that started from the copy between the look and the move is caught here: the
  // folder goes back and nothing has changed.
  if CopyAtWork(AsideFolder) then begin
    if not Moved(AsideFolder, AppFolder) then
      Log('The folder could not be put back: ' + AsideFolder);
    Result := AtWork;
    Exit;
  end;
  MovedAside := True;
end;

// The copy in the new folder sets itself up as Set up does; on a refusal its words are kept,
// and on success whatever it said besides (a warning, a note on the job file) is shown.
procedure SetUp;
var
  ResultCode: Integer;
  Output: TExecOutput;
begin
  if not RunProgram(AppFolder, 'install', ResultCode, Output) then begin
    Refusal := 'MirrorHold could not be started to set itself up (' + SysErrorMessage(ResultCode) + ').';
    Exit;
  end;
  if ResultCode <> 0 then begin
    Refusal := Joined(Output.StdErr);
    if Refusal = '' then
      Refusal := Joined(Output.StdOut);
    if Refusal = '' then
      Refusal := 'MirrorHold ended with status ' + IntToStr(ResultCode) + ' and said nothing.';
    Exit;
  end;
  Warnings := Joined(Output.StdErr);
end;

// A refused first install leaves nothing: the new folder and the entry in the list of
// installed apps go, and whatever was in the folder before comes back.
procedure Undo;
begin
  if not DelTree(AppFolder, True, True, True) then
    Log('The new folder could not be removed completely: ' + AppFolder);
  if not RegDeleteKeyIncludingSubkeys(HKCU, UninstallKey) then
    Log('The entry in the list of installed apps could not be removed.');
  if MovedAside then begin
    if Moved(AsideFolder, AppFolder) then
      MovedAside := False
    else
      Log('The previous folder could not be put back: ' + AsideFolder);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    Exit;
  if not Updating then
    SetUp;
  if Refusal <> '' then begin
    Undo;
    if WizardSilent then
      SuppressibleMsgBox('MirrorHold was not installed: it could not be set up.' + #13#10#13#10 + Refusal,
                         mbError, MB_OK, IDOK);
    Exit;
  end;
  Completed := True;
  if MovedAside and not DelTree(AsideFolder, True, True, True) then
    Log('The previous release''s folder could not be removed completely: ' + AsideFolder);
  MovedAside := False;
end;

function OfferOpening: Boolean;
begin
  Result := Refusal = '';
end;

function GetCustomSetupExitCode: Integer;
begin
  if Refusal <> '' then
    Result := RefusedExitCode
  else
    Result := 0;
end;

// An install cancelled or failed before it finished has been rolled back by Inno Setup:
// the folder moved aside comes back.
procedure DeinitializeSetup;
begin
  if MovedAside and not Completed then begin
    if DirExists(AppFolder) then
      DelTree(AppFolder, True, True, True);
    if not Moved(AsideFolder, AppFolder) then
      Log('The previous folder could not be put back: ' + AsideFolder);
  end;
end;

// Whether the copy in App runs at all: installation answers with status 0 whenever it does.
// What it said, when it does not, comes back in Words.
function CopyRuns(const App: String; var Words: String): Boolean;
var
  ResultCode: Integer;
  Output: TExecOutput;
begin
  Words := '';
  if not RunProgram(App, 'installation', ResultCode, Output) then begin
    Words := SysErrorMessage(ResultCode);
    Result := False;
    Exit;
  end;
  Result := ResultCode = 0;
  if not Result then
    Words := Joined(Output.StdErr);
end;

// After the user confirmed, before anything is removed: refused while the copy works, and
// the copy's setup removed only if it is the one set up. A copy that cannot run at all can
// answer nothing, so its folder goes regardless, and the user is told; a copy that runs and
// refuses changes nothing.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  Output: TExecOutput;
  Words: String;
begin
  if CurUninstallStep <> usAppMutexCheck then
    Exit;
  if CopyAtWork(AppFolder) then begin
    SuppressibleMsgBox(AtWork + #13#10#13#10 + 'Nothing was removed. Remove MirrorHold again once it is done.',
                       mbError, MB_OK, IDOK);
    Abort;
  end;
  if not CopyRuns(AppFolder, Words) or
     not RunProgram(AppFolder, 'uninstall --if-set-up-here', ResultCode, Output) then begin
    if Words <> '' then
      Words := ' (' + Words + ')';
    SuppressibleMsgBox('MirrorHold cannot run from this copy, so it could not be asked whether its automatic runs ' +
                       'start it' + Words + '. Its folder is removed; if its automatic runs started it, remove them ' +
                       'with Preferences, Remove, in the copy you keep.', mbInformation, MB_OK, IDOK);
    Exit;
  end;
  if ResultCode <> 0 then begin
    Words := Joined(Output.StdErr);
    if Words = '' then
      Words := Joined(Output.StdOut);
    SuppressibleMsgBox('MirrorHold could not remove its setup:' + #13#10#13#10 + Words + #13#10#13#10 +
                       'Nothing was removed.', mbError, MB_OK, IDOK);
    Abort;
  end;
  Words := Joined(Output.StdErr);
  if Words <> '' then
    SuppressibleMsgBox('MirrorHold removed its setup, and said:' + #13#10#13#10 + Words, mbInformation, MB_OK, IDOK);
end;
