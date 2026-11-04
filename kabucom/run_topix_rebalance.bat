@echo off
echo Starting topix_rebalance.py...
cd /d "%~dp0"
python topix_rebalance.py
if %errorlevel% neq 0 (
    echo Error occurred while running topix_rebalance.py
    pause
    exit /b %errorlevel%
)
echo Program finished successfully.
pause