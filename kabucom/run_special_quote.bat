@echo off
echo Starting special_quote.py...
cd /d "%~dp0"
python special_quote.py
if %errorlevel% neq 0 (
    echo Error occurred while running special_quote.py
    pause
    exit /b %errorlevel%
)
echo Program finished successfully.
pause