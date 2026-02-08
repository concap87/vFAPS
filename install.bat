@echo off
echo ============================================
echo  vFAPS - Setup
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo Download from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

echo Installing core dependencies...
pip install PyQt6 PyQt6-Qt6 python-mpv openvr numpy

echo.
echo Installing optional Qt multimedia fallback (OK if this fails)...
pip install PyQt6-sip PyQt6-Multimedia PyQt6-MultimediaWidgets 2>nul
if errorlevel 1 (
    echo    Note: Qt Multimedia not available for your Python/platform.
    echo    This is fine - the app uses mpv for video playback instead.
)

echo.
echo ============================================
echo  IMPORTANT: Additional Setup Required
echo ============================================
echo.
echo 1. MPV VIDEO PLAYER (Required for best results):
echo    - Download mpv from: https://mpv.io/installation/
echo    - Or use: winget install mpv
echo    - Or use chocolatey: choco install mpv
echo    - The mpv-2.dll (or libmpv-2.dll) must be findable.
echo      Easiest: add mpv's install folder to your system PATH.
echo.
echo 2. FFMPEG (Required for beat detection):
echo    - Download from: https://ffmpeg.org/download.html
echo    - Or use: winget install ffmpeg
echo    - Or use chocolatey: choco install ffmpeg
echo    - ffmpeg.exe must be on your system PATH
echo.
echo 3. STEAMVR (Required for VR controller input):
echo    - Install Steam and SteamVR from Steam Store
echo    - Have SteamVR running before launching this app
echo    - Turn on your VR controller (Index, Quest via Link, Vive, etc.)
echo.
echo 4. WITHOUT VR HARDWARE:
echo    - The app works in Mouse Fallback mode
echo    - Move mouse vertically over the video area to set position
echo.
echo ============================================
echo  Setup complete! Run 'run.bat' to start.
echo ============================================
pause
