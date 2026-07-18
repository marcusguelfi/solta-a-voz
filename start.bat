@echo off
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo [ERRO] Ambiente nao instalado ainda — rode o setup.bat primeiro.
  pause
  exit /b 1
)
echo === Solta a Voz — karaoke caseiro ===
echo Abrindo em http://localhost:8777 ...
start "" http://localhost:8777
.venv\Scripts\python.exe server\main.py
