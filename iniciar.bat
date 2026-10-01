@echo off
rem Abre Zak_light desde el codigo (sin instalar). Deja esta ventana abierta mientras uses la app: muestra los mensajes.
rem Para instalarlo de forma normal en el PC usa Zak_light_Setup.exe (o crear_instalador.bat para crearlo).
cd /d "%~dp0"

where python >nul 2>&1 || (
  echo No se encuentra Python 3.12. Instalalo desde https://www.python.org/downloads/
  echo ^(marca "Add python.exe to PATH"^) y vuelve a abrir este archivo.
  pause
  exit /b 1
)

rem Primera vez: instala las dependencias si faltan (aqui si se ve el progreso)
python -c "import numpy, fastapi, uvicorn, pystray, hid, webview, dxcam" >nul 2>&1 || (
  echo Preparando Zak_light por primera vez: instalando dependencias, tarda un par de minutos...
  python -m pip install -r src\requirements.txt || (
    echo No se pudieron instalar las dependencias.
    pause
    exit /b 1
  )
)

python src\app.py
