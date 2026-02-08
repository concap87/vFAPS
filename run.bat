@echo off
echo Starting vFAPS...
cd /d "%~dp0"
python main.py
if errorlevel 1 (
    echo.
    echo Application exited with an error.
    echo Run install.bat first if you haven't already.
    pause
)
