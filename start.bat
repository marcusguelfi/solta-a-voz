@echo off
cd /d %~dp0
echo === Solta a Voz — karaoke caseiro ===
echo Abrindo em http://localhost:8777 ...
start "" http://localhost:8777
.venv\Scripts\python.exe server\main.py
