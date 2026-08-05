@echo off
REM Stranger Attractions event refresher - launched by Windows Task Scheduler.
REM Pulls new shows from Skeletix, updates events.js, pushes to GitHub.
REM Cloudflare Pages deploys the push automatically.

cd /d "%~dp0.."

REM Facebook first (door-only shows with no presale). Non-fatal: if the browser
REM or the login profile is unavailable, the Skeletix refresh still runs.
python "%~dp0fb_scan.py" >> "%~dp0refresh-runner.log" 2>&1

python "%~dp0refresh_events.py" >> "%~dp0refresh-runner.log" 2>&1
exit /b %ERRORLEVEL%
