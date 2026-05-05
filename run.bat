@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

if not exist ".venv" (
  echo Creating virtual environment...
  py -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installing requirements...
py -m pip install --upgrade pip
py -m pip install -r requirements.txt

echo Starting Kawn Pulse Engine...
set PYTHONPATH=%CD%
uvicorn app.main:app --reload

endlocal
