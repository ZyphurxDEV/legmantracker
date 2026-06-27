@echo off
setlocal
title Legman Tracker Uninstaller

echo.
echo   Legman Tracker - Uninstaller
echo   ============================
echo.
echo   This removes everything Legman Tracker created for the current
echo   user account:
echo.
echo     - the app data folder  %%APPDATA%%\LegmanTracker
echo       (tracked games, update history, cached icons, log, settings)
echo     - the "start with Windows" autostart entry
echo     - the notification (AppUserModelID) registry keys
echo.
echo   It does NOT need administrator rights (everything is under HKCU).
echo   The program file LegmanTracker.exe is left alone - delete it
echo   yourself afterwards if you want.
echo.

choice /c YN /m "Remove Legman Tracker now"
if errorlevel 2 goto :cancel

echo.
echo   [1/4] Closing Legman Tracker if it's running...
taskkill /f /im LegmanTracker.exe >nul 2>&1

echo   [2/4] Removing autostart entry...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v LegmanTracker /f >nul 2>&1

echo   [3/4] Removing notification registry keys...
reg delete "HKCU\Software\Classes\AppUserModelId\Zyphurx.LegmanTracker" /f >nul 2>&1
reg delete "HKCU\Software\Classes\AppUserModelId\LegmanTracker" /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\Zyphurx.LegmanTracker" /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\LegmanTracker" /f >nul 2>&1

echo   [4/4] Deleting app data folder...
rmdir /s /q "%APPDATA%\LegmanTracker" >nul 2>&1

echo.
echo   Done - Legman Tracker has been removed.
echo.
echo   (If a toast icon still appears stale, sign out and back in once;
echo    Windows caches notification icons.)
echo.
pause
exit /b 0

:cancel
echo.
echo   Cancelled - nothing was changed.
echo.
pause
exit /b 1
