@echo off
echo Starting futures_close_momentum.py...
cd /d "%~dp0"
python futures_close_momentum.py
if %errorlevel% neq 0 (
    echo Error occurred while running futures_close_momentum.py
    pause
    exit /b %errorlevel%
)
echo Program finished successfully.
pause