@echo off
echo Starting orb.py...
cd /d "%~dp0"
python orb.py
if %errorlevel% neq 0 (
    echo Error occurred while running orb.py
    pause
    exit /b %errorlevel%
)
echo Program finished successfully.
pause