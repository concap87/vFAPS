@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo  vFAPS Build Script
echo ==========================================
echo.

:: -------------------------------------------
:: STEP 1: Clean old builds
:: -------------------------------------------
echo [1/5] Cleaning old builds...
if exist "dist" rmdir /s /q dist
if exist "build" rmdir /s /q build
echo       Done.
echo.

:: -------------------------------------------
:: STEP 2: Create hooks folder
:: -------------------------------------------
echo [2/5] Setting up hooks...
if not exist "hooks" mkdir hooks
if exist "hook-openvr.py" (
    copy /y "hook-openvr.py" "hooks\hook-openvr.py" >nul
    echo       hook-openvr.py ready.
) else (
    echo       WARNING: hook-openvr.py not found in project root.
    echo       Copy it here or VR will not work in the packaged app.
)
echo.

:: -------------------------------------------
:: STEP 3: Run PyInstaller
:: -------------------------------------------
echo [3/5] Running PyInstaller...
echo       This takes 1-3 minutes...
echo.

pyinstaller --noconfirm ^
    --name "vFAPS" ^
    --icon "vfaps-250.ico" ^
    --paths . ^
    --additional-hooks-dir hooks ^
    --hidden-import main_window ^
    --hidden-import video_player ^
    --hidden-import vr_controller ^
    --hidden-import recorder ^
    --hidden-import timeline_widget ^
    --hidden-import position_display ^
    --hidden-import calibration_wizard ^
    --hidden-import controller_viz ^
    --hidden-import funscript_io ^
    --hidden-import beat_detection ^
    --hidden-import stabilization ^
    --hidden-import vision_tracking ^
    --hidden-import json ^
    --hidden-import copy ^
    --hidden-import math ^
    --hidden-import threading ^
    --hidden-import time ^
    --hidden-import numpy ^
    --hidden-import PyQt6.QtWidgets ^
    --hidden-import PyQt6.QtCore ^
    --hidden-import PyQt6.QtGui ^
    --hidden-import PyQt6.sip ^
    --collect-all openvr ^
    main.py

if errorlevel 1 (
    echo.
    echo !! PyInstaller FAILED !!
    echo    Read the errors above.
    pause
    exit /b 1
)

if not exist "dist\vFAPS\vFAPS.exe" (
    echo !! No exe was created !!
    pause
    exit /b 1
)

echo.
echo       PyInstaller succeeded.
echo.

:: -------------------------------------------
:: STEP 4: Copy mpv DLL
:: -------------------------------------------
echo [4/5] Copying mpv library...

if exist "dist\vFAPS\_internal\libmpv-2.dll" (
    echo       libmpv-2.dll already bundled.
) else if exist "dist\vFAPS\libmpv-2.dll" (
    echo       libmpv-2.dll already bundled.
) else if exist "libmpv-2.dll" (
    copy /y "libmpv-2.dll" "dist\vFAPS\_internal\libmpv-2.dll" >nul
    echo       Copied libmpv-2.dll from project root.
) else if exist "mpv-2.dll" (
    copy /y "mpv-2.dll" "dist\vFAPS\_internal\mpv-2.dll" >nul
    echo       Copied mpv-2.dll from project root.
) else (
    echo.
    echo       WARNING: No mpv DLL found!
    echo       Video won't work. Copy libmpv-2.dll or mpv-2.dll into:
    echo         dist\vFAPS\_internal\
    echo.
)
echo.

:: -------------------------------------------
:: STEP 5: Verify
:: -------------------------------------------
echo [5/5] Verifying build...
echo.
echo       Checking key files:

if exist "dist\vFAPS\vFAPS.exe"                        (echo         [OK] vFAPS.exe) else (echo         [!!] vFAPS.exe MISSING)
if exist "dist\vFAPS\_internal\libmpv-2.dll"            (echo         [OK] libmpv-2.dll) else (
    if exist "dist\vFAPS\_internal\mpv-2.dll"           (echo         [OK] mpv-2.dll) else (echo         [!!] mpv DLL MISSING - video won't work)
)

:: Check for openvr DLL
set "OPENVR_OK=0"
if exist "dist\vFAPS\_internal\openvr\libopenvr_api_64.dll" set "OPENVR_OK=1"
if exist "dist\vFAPS\_internal\libopenvr_api_64.dll"        set "OPENVR_OK=1"
if "%OPENVR_OK%"=="1" (echo         [OK] openvr DLL) else (echo         [!!] openvr DLL MISSING - VR won't work)

echo.
echo ==========================================
echo  BUILD COMPLETE
echo ==========================================
echo.
echo  Your app is in: dist\vFAPS\
echo  Test it:        dist\vFAPS\vFAPS.exe
echo.
echo  Once it works, run Inno Setup on
echo  installer.iss to create the installer.
echo ==========================================
echo.
pause
