@echo off
setlocal
set "ROOT=%~dp0"
set "LAUNCHER=%ROOT%launcher\aaw_launcher.py"

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%LAUNCHER%" start %*
    goto :report
)

where python >nul 2>nul
if not errorlevel 1 (
    python "%LAUNCHER%" start %*
    goto :report
)

echo.
echo AAW could not start.
echo.
echo No Python interpreter was found on PATH ^(tried "py" and "python"^).
echo Install Python 3.12 or newer from https://www.python.org/downloads/ and try again.
echo.
pause
exit /b 1

:report
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
    echo.
    echo See the log for details:
    echo %ROOT%.runtime\launcher\launcher.log
    echo.
    pause
)
endlocal & exit /b %EXITCODE%
