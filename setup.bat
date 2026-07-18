@echo off
setlocal enabledelayedexpansion
cd /d %~dp0
echo ============================================
echo   Solta a Voz - instalacao (primeira vez)
echo ============================================
echo.

rem ---- Python 3.11-3.13 (o 3.14 AINDA quebra as dependencias de IA) ----
set "PY="
for %%v in (3.13 3.12 3.11) do (
  if not defined PY (
    py -%%v --version >nul 2>nul
    if not errorlevel 1 set "PY=py -%%v"
  )
)
if not defined PY (
  py -3.14 --version >nul 2>nul
  if not errorlevel 1 (
    echo [ERRO] Achei o Python 3.14, mas as dependencias de IA ainda nao rodam nele.
    echo Instale o 3.13 AO LADO ^(o 3.14 pode ficar^):
    echo    https://www.python.org/downloads/release/python-31310/
    echo Marque "py launcher" na instalacao e rode este setup de novo.
    pause
    exit /b 1
  )
  echo [ERRO] Python nao encontrado. Preciso do 3.11, 3.12 ou 3.13.
  echo Baixe em https://www.python.org/downloads/ e marque
  echo "Add python.exe to PATH" + "py launcher". Depois rode este setup de novo.
  pause
  exit /b 1
)
echo Python encontrado: %PY%

if not exist .venv (
  echo [1/3] Criando ambiente Python...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [ERRO] Falha ao criar o ambiente .venv
    pause
    exit /b 1
  )
)

echo [2/3] Instalando dependencias — pode levar VARIOS minutos e ~2GB...
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [ERRO] A instalacao de dependencias falhou — leia a mensagem acima.
  echo Dica: falha de rede e comum na 1a vez; rode o setup de novo que ele retoma.
  pause
  exit /b 1
)

if not exist tools\ffmpeg\bin\ffmpeg.exe (
  echo [3/3] Baixando ffmpeg portatil (~100MB)...
  if not exist tools mkdir tools
  powershell -NoProfile -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'tools\ffmpeg.zip'; Expand-Archive 'tools\ffmpeg.zip' -DestinationPath 'tools' -Force; Remove-Item 'tools\ffmpeg.zip'; Get-ChildItem 'tools' -Directory -Filter 'ffmpeg-*' | Rename-Item -NewName 'ffmpeg'"
  if not exist tools\ffmpeg\bin\ffmpeg.exe (
    echo [ERRO] Download do ffmpeg falhou. Rode o setup de novo, ou baixe
    echo https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
    echo e extraia como tools\ffmpeg ^(tem que existir tools\ffmpeg\bin\ffmpeg.exe^)
    pause
    exit /b 1
  )
) else (
  echo [3/3] ffmpeg ja presente.
)

echo.
echo Pronto! Use start.bat pra abrir o karaoke.
echo (os modelos de IA baixam sozinhos no primeiro preparo de musica)
pause
