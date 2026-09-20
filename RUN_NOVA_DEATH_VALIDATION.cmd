@echo off
setlocal
cd /d "%~dp0"

set "NOVA_BRIDGE_DIR=%CD%\nova-ipc-validation"
set "NOVA_VALIDATION_MODE=1"

echo ============================================================
echo NOVA DESTRUCTIVE DEATH VALIDATION
echo ============================================================
echo.
echo WARNING: THIS TEST KILLS THE LOADED CHARACTER ON PURPOSE.
echo NEVER LOAD NOVA LIFE 1 FOR THIS TEST.
echo Use a disposable validation character only.
echo.
choice /C YN /N /M "Is the loaded character disposable? [Y/N] "
if errorlevel 2 exit /b 1

py -3 "%CD%\nova_runtime\nova_validation.py" --suite death
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
