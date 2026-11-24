@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "D:\python\pythonw.exe" (
    start "" "D:\python\pythonw.exe" "%~dp0LetterStudio.pyw"
) else (
    start "" pythonw "%~dp0LetterStudio.pyw"
)
