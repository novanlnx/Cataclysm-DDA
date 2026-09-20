@echo off
setlocal
cd /d "%~dp0"

set "NOVA_BRIDGE_DIR=%CD%\nova-ipc-validation"
set "NOVA_VALIDATION_MODE=1"

echo ============================================================
echo NOVA DETERMINISTIC VALIDATION MODE
echo ============================================================
echo.
echo This is TEST-ONLY mode.
echo DO NOT load Nova Life 1 or any character you care about.
echo Use a fresh throwaway validation world/character.
echo.
if not exist "%CD%\cataclysm-tiles.exe" (
    echo ERROR: cataclysm-tiles.exe was not found in %CD%
    pause
    exit /b 2
)

if not exist "%CD%\nova_runtime\nova_validation.py" (
    echo ERROR: nova_runtime\nova_validation.py was not found.
    pause
    exit /b 2
)

if exist "%NOVA_BRIDGE_DIR%\command.json" del /q "%NOVA_BRIDGE_DIR%\command.json" >nul 2>&1

start "" "%CD%\cataclysm-tiles.exe"

echo.
echo CDDA has started with NOVA_VALIDATION_MODE=1.
echo Create/load a THROWAWAY validation character and enter normal gameplay.
echo Do NOT start nova_agent_beta.py.
echo.
pause

py -3 "%CD%\nova_runtime\nova_validation.py" --suite batch1
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo BATCH VALIDATION PASSED.
) else (
    echo BATCH VALIDATION FAILED with exit code %RC%.
)
echo Results are saved under:
echo   %NOVA_BRIDGE_DIR%
echo.
pause
exit /b %RC%
