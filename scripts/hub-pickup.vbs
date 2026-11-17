' Run scripts/hub-pickup.py in WSL with no console window (Task Scheduler, every 15 min).
' wscript.exe is a GUI host, so window style 0 means wsl.exe never flashes a console.
' Same pattern as ship-locator/run-hidden.vbs. Exit code passes through to Last Run Result.
'
' Register:  schtasks /Create /F /SC MINUTE /MO 15 /TN "LifeManagerHubPickup" /TR "wscript.exe C:\Users\damia\OneDrive\Documents\LLM_Land\scripts\hub-pickup.vbs"
' Remove:    schtasks /Delete /TN "LifeManagerHubPickup" /F
Option Explicit
Dim shell
Set shell = CreateObject("WScript.Shell")
WScript.Quit shell.Run("wsl.exe -d Ubuntu -e python3 /mnt/c/Users/damia/OneDrive/Documents/LLM_Land/scripts/hub-pickup.py", 0, True)
