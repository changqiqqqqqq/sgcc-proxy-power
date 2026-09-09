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
  echo 未找到 Python 3。
  pause
  exit /b 1
)
echo 选择操作：
echo   1 - 重新扫描各省目录并下载 PDF
echo   2 - 使用已有目录清单下载 PDF
echo   3 - 仅重新扫描目录
set /p ACTION=请输入 1、2 或 3：
if "%ACTION%"=="3" (%PYTHON_EXE% "%~dp0sgcc_proxy_power_downloader.py" --scan & pause & exit /b)
if "%ACTION%"=="2" (%PYTHON_EXE% "%~dp0sgcc_proxy_power_downloader.py" --download --workers 10 & pause & exit /b)
%PYTHON_EXE% "%~dp0sgcc_proxy_power_downloader.py" --all --workers 10
pause
endlocal
