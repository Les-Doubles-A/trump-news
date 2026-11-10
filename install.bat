@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3.11+ est requis : https://www.python.org/downloads/ & exit /b 1)
if not exist .venv (python -m venv .venv || exit /b 1)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip || exit /b 1
pip install -r requirements.txt || exit /b 1
if not exist config.yaml copy config.example.yaml config.yaml >nul
if not exist .env copy .env.example .env >nul
python -m app.main init || exit /b 1
echo.
echo Installation terminee. Lancez run.bat check-llm pour tester LM Studio.
where ffmpeg >nul 2>nul || echo [Info] FFmpeg n'est pas dans le PATH : necessaire a partir de la Phase 7.
