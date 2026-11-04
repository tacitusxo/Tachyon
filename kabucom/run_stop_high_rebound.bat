@echo off
echo Starting stop_high_rebound.py...
cd /d "%~dp0"
python stop_high_rebound.py
if %errorlevel% neq 0 (
    echo Error occurred while running stop_high_rebound.py
    pause
    exit /b %errorlevel%
)
echo Program finished successfully.
pause