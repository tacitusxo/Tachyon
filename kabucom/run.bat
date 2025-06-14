@echo off
echo Starting main.py...
cd /d "%~dp0"
python main.py
if %errorlevel% neq 0 (
    echo Error occurred while running main.py
    pause
    exit /b %errorlevel%
)
echo Program finished successfully.
pause