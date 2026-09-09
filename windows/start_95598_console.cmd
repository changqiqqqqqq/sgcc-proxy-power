@echo off
setlocal
cd /d "%~dp0"
set "PYTHON_EXE="
where py.exe >nul 2>nul
if not errorlevel 1 set "PYTHON_EXE=py.exe -3"
if not defined PYTHON_EXE (
  where python.exe >nul 2>nul
  if not errorlevel 1 set "PYTHON_EXE=python.exe"
)
if not defined PYTHON_EXE if exist "D:\python\python.exe" set "PYTHON_EXE=D:\python\python.exe"
if not defined PYTHON_EXE (
  echo 未找到 Python。请先安装 Python 3.11 或更高版本。
  pause
  exit /b 1
)
%PYTHON_EXE% "%~dp095598_interactive_console.py"
if errorlevel 1 (
  echo 控制台已退出，请查看 interactive_console.log。
  pause
)
endlocal
