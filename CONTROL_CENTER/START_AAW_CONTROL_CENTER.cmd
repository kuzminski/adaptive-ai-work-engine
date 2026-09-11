@echo off
setlocal
python "%~dp0aaw_control_center.py"
if errorlevel 1 pause
endlocal
