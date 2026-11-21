Set WshShell = CreateObject("WScript.Shell")
Set Fso = CreateObject("Scripting.FileSystemObject")
BaseDir = Fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = BaseDir
PythonW = "pythonw.exe"
If Fso.FileExists(BaseDir & "\.venv\Scripts\pythonw.exe") Then
    PythonW = """" & BaseDir & "\.venv\Scripts\pythonw.exe"""
End If
WshShell.Run PythonW & " run.py", 0, False
