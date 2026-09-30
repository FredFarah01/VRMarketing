@echo off
REM Windows: installs dependencies into a local virtualenv and starts the site.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version >nul 2>nul || (echo Python 3.10+ is required: https://www.python.org/downloads/ ^(tick "Add Python to PATH"^) & pause & exit /b 1)
if not exist .venv %PY% -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
pip install -q -r requirements.txt
echo Open http://localhost:5000/care-recruitment-compliance-checklist
start "" http://localhost:5000/care-recruitment-compliance-checklist
python app.py
