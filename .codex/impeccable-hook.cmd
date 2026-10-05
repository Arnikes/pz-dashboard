@echo off
setlocal
rem Codex may use PowerShell. Keep batch syntax behind an explicit cmd.exe.
rem Resolve the launcher relative to this file, including paths with spaces.
set "launcher=%~dp0..\.agents\skills\impeccable\scripts\impeccable.cmd"
if not exist "%launcher%" exit /b 0
call "%launcher%" hook
exit /b %errorlevel%
