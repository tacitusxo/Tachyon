@echo off
echo Starting disclosure_score.py...
cd /d "%~dp0"
python disclosure_score.py
if %errorlevel% neq 0 (
    echo Error occurred while running disclosure_score.py
    pause
    exit /b %errorlevel%
)
echo Program finished successfully.
pause