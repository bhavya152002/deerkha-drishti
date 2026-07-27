@echo off
REM ---- Deerkha Drishti office server launcher ----
REM Point NSSM (service "DeerkhaServer") or Task Scheduler at this file.
REM
REM Resolves its own location, so the repo can live anywhere and this file does
REM not need editing per machine. %~dp0 is the directory containing this script
REM (with a trailing backslash).

cd /d "%~dp0"

REM Prefer the local venv; fall back to the conda env if that is how this box
REM was set up. Adjust only if neither exists.
if exist "%~dp0.venv\Scripts\python.exe" (
    set "DD_PY=%~dp0.venv\Scripts\python.exe"
) else (
    call D:\Anaconda\Scripts\activate.bat D:\Anaconda\envs\rfsam_env
    set "DD_PY=python"
)

REM Single worker is deliberate. At 7 devices the steady-state load is ~1 req/s;
REM the bottleneck was never worker count, and --workers N multiplies this
REM process's DB connections by N against a scarce Supabase connection budget.
"%DD_PY%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
