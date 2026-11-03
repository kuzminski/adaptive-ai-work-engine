@echo off
setlocal
set "ROOT=%~dp0"

if exist "%ROOT%AAW.exe" (
    "%ROOT%AAW.exe" %*
    goto :report
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%ROOT%AAW.py" %*
    goto :report
)

where python >nul 2>nul
if not errorlevel 1 (
    python "%ROOT%AAW.py" %*
    goto :report
)

echo.
echo AAW could not start.
echo.
echo Neither AAW.exe nor a suitable Python interpreter was found on PATH.
echo Install Python 3.12 or newer from https://www.python.org/downloads/ and try again,
echo or make sure AAW.exe is present in this folder.
echo.
pause
exit /b 1

:report
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
    echo.
    echo AAW exited with error code %EXITCODE%.
    echo.
    pause
)
endlocal & exit /b %EXITCODE%
