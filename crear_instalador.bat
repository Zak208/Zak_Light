@echo off
rem Crea Zak_light_Setup.exe (en esta misma carpeta): el instalador de la app para Windows (con la opcion de arrancar con el PC).
rem Solo necesita Python 3.12: instala por su cuenta las librerias y Inno Setup (el programa que crea el instalador).
setlocal
cd /d "%~dp0"

rem Preparacion automatica: Python y las librerias, y el programa que crea el instalador (Inno Setup)
where python >nul 2>&1 || (
  echo No se encuentra Python 3.12. Instalalo desde https://www.python.org/downloads/ ^(marca "Add python.exe to PATH"^).
  goto :fail
)
echo Comprobando las librerias necesarias...
python -m pip install -q -r src\requirements.txt -r build\requirements-dev.txt || goto :fail
set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" (
  echo Instalando Inno Setup, el programa que crea el instalador ^(Windows puede pedir permiso^)...
  winget install -e --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements
)
set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" (
  echo No se pudo instalar Inno Setup. Descargalo de https://jrsoftware.org/isdl.php y vuelve a ejecutar este archivo.
  goto :fail
)

rem 0. Revision estatica de errores reales (se omite si ruff no esta instalado)
python -m ruff --version >nul 2>&1 && (python -m ruff check --config build\ruff.toml . || goto :fail)

rem 1. Las pruebas deben pasar antes de empaquetar nada
python -m pytest tests -q -W ignore --ignore=tests/check_audio_loopback.py || goto :fail

rem 2. La version sale de src\version.py (fuente unica)
for /f %%v in ('python -c "import sys; sys.path.insert(0, 'src'); from version import VERSION; print(VERSION)"') do set VER=%%v

python build\make_icon.py || goto :fail
if exist dist\Zak_light rmdir /s /q dist\Zak_light
echo Empaquetando la app (tarda un par de minutos; los avisos de 'android' son normales)...
python -m PyInstaller build\zak_light.spec --noconfirm --log-level ERROR --distpath dist --workpath build\work || goto :fail

"%ISCC%" /Q /DAppVersion=%VER% build\installer.iss || goto :fail

rem El instalador final queda aqui, junto a iniciar.bat
copy /y dist\Zak_light_Setup.exe Zak_light_Setup.exe >nul || goto :fail

echo.
echo ============================================================
echo  Listo: Zak_light_Setup.exe (version %VER%)
echo  Esta en esta misma carpeta, junto a iniciar.bat.
echo  Haz doble clic en el: se abre el asistente para elegir
echo  carpeta, acceso en el escritorio e inicio con Windows.
echo ============================================================
call :wait
exit /b 0

:fail
echo.
echo ERROR: fallo la compilacion.
call :wait
exit /b 1

:wait
rem Si se abrio con doble clic, deja la ventana abierta para poder leer el resultado
echo %CMDCMDLINE% | find /i "/c" >nul && pause
exit /b 0
