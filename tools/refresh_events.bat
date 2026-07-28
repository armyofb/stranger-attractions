@echo off
REM Stranger Attractions event refresher - launched by Windows Task Scheduler.
REM Pulls new shows from Skeletix, updates events.js, pushes to GitHub.
REM Cloudflare Pages deploys the push automatically.

cd /d "%~dp0.."
python "%~dp0refresh_events.py" >> "%~dp0refresh-runner.log" 2>&1
exit /b %ERRORLEVEL%
