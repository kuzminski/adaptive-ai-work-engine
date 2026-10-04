@echo off
setlocal
set "ROOT=%~dp0"
set "LAUNCHER=%ROOT%launcher\aaw_launcher.py"

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%LAUNCHER%" stop %*
    goto :report
)

where python >nul 2>nul
if not errorlevel 1 (
    python "%LAUNCHER%" stop %*
    goto :report
)

echo.
echo Could not run the stop command: no Python interpreter found on PATH.
echo.
pause
exit /b 1

:report
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" pause
endlocal & exit /b %EXITCODE%
