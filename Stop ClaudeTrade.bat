@echo off
REM Double-click me (Windows). Stops ClaudeTrade. Your practice history is kept.
cd /d "%~dp0"
docker compose down
echo ClaudeTrade stopped. Your history is saved.
pause
