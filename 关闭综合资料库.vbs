Option Explicit
Dim shell, fso, folder, python, base, command, machine
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
machine = ""
If WScript.Arguments.Count > 1 Then
  MsgBox "Pass at most one absolute machine JSON file path.", 16, "Reference Library"
  WScript.Quit 2
End If
If WScript.Arguments.Count = 1 Then
  machine = WScript.Arguments(0)
  ' Quotes and line breaks are invalid filename characters; never invoke a shell.
  If InStr(machine, Chr(34)) > 0 Or InStr(machine, vbCr) > 0 Or InStr(machine, vbLf) > 0 Then WScript.Quit 2
  If Len(machine) < 3 Then WScript.Quit 2
  ' Require a drive-qualified absolute path or an explicit UNC path.
  If Not ((Mid(machine, 2, 2) = ":\" And InStr("ABCDEFGHIJKLMNOPQRSTUVWXYZ", UCase(Left(machine, 1))) > 0) Or Left(machine, 2) = "\\") Then
    MsgBox "The machine JSON path must be absolute.", 16, "Reference Library"
    WScript.Quit 2
  End If
  If Not fso.FileExists(machine) Then
    MsgBox "The specified machine JSON file does not exist.", 16, "Reference Library"
    WScript.Quit 2
  End If
  machine = fso.GetAbsolutePathName(machine)
End If
python = ""
On Error Resume Next
base = shell.RegRead("HKCU\Software\Python\PythonCore\3.14\InstallPath\")
If Err.Number = 0 Then python = base & "pythonw.exe"
Err.Clear
If Not fso.FileExists(python) Then
  base = shell.RegRead("HKLM\Software\Python\PythonCore\3.14\InstallPath\")
  If Err.Number = 0 Then python = base & "pythonw.exe"
  Err.Clear
End If
On Error GoTo 0
If Not fso.FileExists(python) Then python = shell.ExpandEnvironmentStrings("%LocalAppData%\Programs\Python\Python314\pythonw.exe")
If Not fso.FileExists(python) Then python = shell.ExpandEnvironmentStrings("%SystemDrive%\Python314\pythonw.exe")
If Not fso.FileExists(python) Then
  MsgBox "Please install standard 64-bit CPython 3.14.8 or a later 3.14 patch. This package uses your local Python.", 16, "Reference Library"
  WScript.Quit 1
End If
shell.CurrentDirectory = folder
command = Chr(34) & python & Chr(34) & " -B " & Chr(34) & folder & "\launch.py" & Chr(34) & " stop"
If machine <> "" Then command = command & " --machine " & Chr(34) & machine & Chr(34)
If shell.Environment("Process")("REFERENCE_LIBRARY_NO_BROWSER") = "1" Then command = command & " --no-browser"
shell.Run command, 0, False
