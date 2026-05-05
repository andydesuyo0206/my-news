@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ================================
echo   マイ朝刊 を起動します
echo ================================
python app.py
pause
