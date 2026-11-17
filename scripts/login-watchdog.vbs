' Run scripts/login-watchdog.py in WSL with no console window (Task Scheduler: daily 06:00 and at logon, catch-up after a missed start).
' Same pattern as hub-pickup.vbs. Exit code passes through to Last Run Result.
'
' Register (PowerShell):
'   $a = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "C:\Users\damia\OneDrive\Documents\LLM_Land\scripts\login-watchdog.vbs"
'   $t = @((New-ScheduledTaskTrigger -Daily -At 6am), (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME))
'   $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
'   Register-ScheduledTask -TaskName "LifeManagerLoginWatchdog" -Action $a -Trigger $t -Settings $s -Force
' Remove:    schtasks /Delete /TN "LifeManagerLoginWatchdog" /F
Option Explicit
Dim shell
Set shell = CreateObject("WScript.Shell")
WScript.Quit shell.Run("wsl.exe -d Ubuntu -e python3 /mnt/c/Users/damia/OneDrive/Documents/LLM_Land/scripts/login-watchdog.py", 0, True)
