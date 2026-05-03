@echo off
set "PYTHON_EXE=%LLM_HANDOFF_PYTHON%"
if "%PYTHON_EXE%"=="" set "PYTHON_EXE=%~dp0..\venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"
"%PYTHON_EXE%" -m llm_handoff.supervisor %*
exit /b %ERRORLEVEL%
