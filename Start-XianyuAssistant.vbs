Option Explicit

Dim shell, fileSystem, projectRoot, pythonExecutable, launcherScript, command

Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

projectRoot = fileSystem.GetParentFolderName(WScript.ScriptFullName)
pythonExecutable = fileSystem.BuildPath(projectRoot, ".venv\Scripts\python.exe")
launcherScript = fileSystem.BuildPath(projectRoot, "scripts\start_application.ps1")

If Not fileSystem.FileExists(pythonExecutable) Then
    MsgBox "The project virtual environment was not found. Follow README.md to install it first.", _
        vbCritical, "Xianyu Assistant"
    WScript.Quit 1
End If

shell.CurrentDirectory = projectRoot
command = "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " _
    & Chr(34) & launcherScript & Chr(34)

' Hide the short-lived PowerShell launcher. It creates Python with the Windows
' CREATE_NO_WINDOW flag, while the Qt application window remains visible.
shell.Run command, 0, False
