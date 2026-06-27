@echo off
REM ----------------------------------------------------------------------------
REM  Build LegmanTracker.exe (single file, no console, system-tray app).
REM  Just double-click this file. The exe ends up in dist\LegmanTracker.exe
REM ----------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

REM --- stop any running instance so we can overwrite the exe ---
taskkill /f /im LegmanTracker.exe >nul 2>&1

REM --- create the venv on first run ---
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv .venv || python -m venv .venv
)

set "PY=.venv\Scripts\python.exe"

echo Installing dependencies...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo.
    echo Dependency install failed.
    pause
    exit /b 1
)

echo Building exe...
"%PY%" -m PyInstaller --noconfirm --onefile --windowed ^
    --name LegmanTracker ^
    --icon icon.ico ^
    --add-data "icon.ico;." ^
    --collect-all windows_toasts ^
    --collect-all winrt ^
    --hidden-import winrt._winrt ^
    --hidden-import winrt.system ^
    --hidden-import winrt.windows.foundation ^
    --hidden-import winrt.windows.foundation.collections ^
    --hidden-import winrt.windows.data.xml.dom ^
    --hidden-import winrt.windows.ui.notifications ^
    --hidden-import winrt._winrt_windows_foundation ^
    --hidden-import winrt._winrt_windows_foundation_collections ^
    --hidden-import winrt._winrt_windows_data_xml_dom ^
    --hidden-import winrt._winrt_windows_ui_notifications ^
    --exclude-module PySide6.QtNetwork --exclude-module PySide6.QtQml ^
    --exclude-module PySide6.QtQuick --exclude-module PySide6.QtQuickWidgets ^
    --exclude-module PySide6.QtOpenGL --exclude-module PySide6.QtOpenGLWidgets ^
    --exclude-module PySide6.QtPrintSupport --exclude-module PySide6.QtSql ^
    --exclude-module PySide6.QtTest --exclude-module PySide6.QtDBus ^
    --exclude-module PySide6.QtPdf --exclude-module PySide6.QtPdfWidgets ^
    --exclude-module PySide6.QtMultimedia --exclude-module PySide6.QtMultimediaWidgets ^
    --exclude-module PySide6.QtConcurrent --exclude-module PySide6.QtXml ^
    --exclude-module PySide6.QtWebChannel --exclude-module PySide6.QtWebSockets ^
    --exclude-module PySide6.QtCharts --exclude-module PySide6.QtDataVisualization ^
    --exclude-module PySide6.QtPositioning --exclude-module PySide6.QtLocation ^
    --exclude-module PySide6.QtSerialPort --exclude-module PySide6.QtSensors ^
    --exclude-module PySide6.QtBluetooth --exclude-module PySide6.QtNfc ^
    --exclude-module PySide6.QtHelp --exclude-module PySide6.QtDesigner ^
    --exclude-module PySide6.QtUiTools --exclude-module PySide6.QtStateMachine ^
    --exclude-module tkinter ^
    legmantracker.py

if errorlevel 1 (
    echo.
    echo Build failed - see output above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Done!  ->  dist\LegmanTracker.exe
echo  Double-click it; the icon appears in the system tray
echo  (near the clock - you may need the ^^ "show hidden icons").
echo ============================================================
pause
