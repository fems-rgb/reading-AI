@echo off
rem 启动服务（双击即可）；有 .venv 用 .venv，否则用系统 Python
chcp 65001 >nul
cd /d "%~dp0.."
set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
"%PY%" run.py
if errorlevel 1 pause
