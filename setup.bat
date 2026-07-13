@echo off
cd /d %~dp0
echo ============================================
echo   Solta a Voz - instalacao (primeira vez)
echo ============================================
echo.

py -3.13 --version >nul 2>nul
if errorlevel 1 (
  echo [ERRO] Python 3.13 nao encontrado.
  echo Instale em https://www.python.org/downloads/ ^(marque "py launcher"^) e rode de novo.
  pause
  exit /b 1
)

if not exist .venv (
  echo [1/3] Criando ambiente Python...
  py -3.13 -m venv .venv
)

echo [2/3] Instalando dependencias (pode levar varios minutos)...
.venv\Scripts\pip install -r requirements.txt "audio-separator[cpu]" stable-ts soundfile numpy

if not exist tools\ffmpeg\bin\ffmpeg.exe (
  echo [3/3] Baixando ffmpeg portatil (~100MB)...
  if not exist tools mkdir tools
  powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'tools\ffmpeg.zip'; Expand-Archive 'tools\ffmpeg.zip' -DestinationPath 'tools' -Force; Remove-Item 'tools\ffmpeg.zip'; Get-ChildItem 'tools' -Directory -Filter 'ffmpeg-*' | Rename-Item -NewName 'ffmpeg'"
) else (
  echo [3/3] ffmpeg ja presente.
)

echo.
echo Pronto! Use start.bat pra abrir o karaoke.
echo (os modelos de IA baixam sozinhos no primeiro preparo de musica)
pause
