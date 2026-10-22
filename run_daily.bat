@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
"D:\IDE\Python39\python.exe" fund.py check >> logs\run.log 2>&1
"D:\IDE\Python39\python.exe" index_watch.py --html docs\index.html >> logs\run.log 2>&1
