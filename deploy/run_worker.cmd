@echo off
rem CardOrder KB worker: long-poll 145, answer with Codex, restart 10s after crash.
rem Exit code 3 = another worker already holds runtime\worker.lock -> stop this loop instead of respawning.
set KB_BASE=http://192.168.31.145:8090
set /p KB_WORKER_TOKEN=<D:\kb-worker\token.txt
set KB_WORKER_NAME=win-%COMPUTERNAME%
set PYTHONUTF8=1
cd /d D:\yuki\cardorder-kb\worker
:loop
D:\yuki\cardorder-kb\.venv\Scripts\python.exe kb_worker.py --repo D:\yuki\aitcg-flutter --runtime D:\kb-worker\runtime >> D:\kb-worker\worker.log 2>&1
if %errorlevel%==3 exit /b 0
timeout /t 10 /nobreak >nul
goto loop
