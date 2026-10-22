@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if not exist logs mkdir logs
python index_watch.py --refresh --html docs\index.html >> logs\run.log 2>&1
python fund.py check >> logs\run.log 2>&1
