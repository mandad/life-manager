' Launch push-position.ps1 with no console window.
'
' A scheduled task created by a standard user runs "only when user is logged on",
' i.e. in the interactive desktop, and powershell.exe creates its console window
' before it reads -WindowStyle Hidden, so the window flashes on every run.
' wscript.exe is a GUI host: window style 0 here means PowerShell never gets a
' window at all. Arguments pass straight through to push-position.ps1, and the
' script's exit code is returned so Task Scheduler's Last Run Result stays useful.
'
' Task action:  wscript.exe "C:\path\to\run-hidden.vbs" -NmeaMode TCP -NmeaHost 10.0.0.5 -NmeaPort 23
Option Explicit
Dim fso, shell, dir, args, a, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
args = ""
For Each a In WScript.Arguments
  args = args & " """ & a & """"
Next
cmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & dir & "\push-position.ps1""" & args
WScript.Quit shell.Run(cmd, 0, True)
