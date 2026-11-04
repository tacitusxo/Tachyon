@echo off
echo Starting no_news_reversal.py...
cd /d "%~dp0"
python no_news_reversal.py
if %errorlevel% neq 0 (
    echo Error occurred while running no_news_reversal.py
    pause
    exit /b %errorlevel%
)
echo Program finished successfully.
pause