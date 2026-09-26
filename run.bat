@echo off
cd /d "%~dp0"
echo.
echo  AgentHire — keep this window OPEN while you use the site.
echo  Open in your browser: http://127.0.0.1:5000/
echo.
where py >nul 2>&1
if %ERRORLEVEL% equ 0 (
  py -3 app.py
) else (
  python app.py
)
echo.
if %ERRORLEVEL% neq 0 echo Server exited with an error. Read the messages above.
pause
