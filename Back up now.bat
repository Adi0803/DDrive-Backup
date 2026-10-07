@echo off
title D-Drive Backup
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo This file must be in the backup program folder, next to the .venv folder
  echo ^(for example C:\DDriveOneDriveBackup^).
  echo.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" DDriveOneDriveBackup.py %*
echo.
pause
