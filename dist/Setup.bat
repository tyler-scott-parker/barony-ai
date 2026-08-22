@echo off
setlocal EnableDelayedExpansion
title Adorcism - Setup

rem ===========================================================================
rem  Adorcism setup (Windows)
rem
rem  This does NOT copy anything into your Barony install. The modded game runs
rem  from this folder and reads Barony's files where they already are, so Steam
rem  can verify or update Barony without ever touching or breaking the mod, and
rem  uninstalling is deleting this one folder.
rem
rem  All this script does is find Barony and write "Play Adorcism.bat" with the
rem  path filled in. It is plain text on purpose -- read it before running it.
rem ===========================================================================

echo.
echo   Adorcism - setup
echo   ----------------
echo.

set "BARONY="

rem --- 1. Where is Steam? -----------------------------------------------------
set "STEAM="
for /f "tokens=2*" %%a in ('reg query "HKCU\Software\Valve\Steam" /v SteamPath 2^>nul') do set "STEAM=%%b"
if not defined STEAM (
  for /f "tokens=2*" %%a in ('reg query "HKLM\SOFTWARE\WOW6432Node\Valve\Steam" /v InstallPath 2^>nul') do set "STEAM=%%b"
)
if defined STEAM set "STEAM=!STEAM:/=\!"

rem --- 2. Steam can keep games on several drives; every library is listed here.
if defined STEAM (
  if exist "!STEAM!\steamapps\common\Barony\barony.exe" set "BARONY=!STEAM!\steamapps\common\Barony"
  set "VDF=!STEAM!\steamapps\libraryfolders.vdf"
  if exist "!VDF!" (
    rem Split on the QUOTE, not on whitespace: a library path routinely contains spaces
    rem ("C:\\Program Files (x86)\\Steam"), and token-splitting on space would cut it in half.
    rem The line reads:  <tab>"path"<tab><tab>"C:\\Program Files (x86)\\Steam"
    rem so quote-delimited token 4 is the path itself, spaces and all.
    for /f "tokens=4 delims=^"" %%L in ('findstr /i /c:"\"path\"" "!VDF!"') do (
      set "P=%%L"
      rem Steam escapes backslashes in the vdf: C:\\Games -> C:\Games
      set "P=!P:\\=\!"
      if exist "!P!\steamapps\common\Barony\barony.exe" (
        if not defined BARONY set "BARONY=!P!\steamapps\common\Barony"
      )
    )
  )
)

rem --- 3. Last resorts: the usual places, then ask. ---------------------------
if not defined BARONY (
  for %%D in (
    "%ProgramFiles(x86)%\Steam\steamapps\common\Barony"
    "%ProgramFiles%\Steam\steamapps\common\Barony"
    "C:\Program Files (x86)\Steam\steamapps\common\Barony"
    "D:\SteamLibrary\steamapps\common\Barony"
    "E:\SteamLibrary\steamapps\common\Barony"
  ) do if not defined BARONY if exist "%%~D\barony.exe" set "BARONY=%%~D"
)

if not defined BARONY (
  echo   I could not find Barony automatically.
  echo.
  echo   In Steam: right-click Barony ^> Manage ^> Browse local files.
  echo   Drag that folder onto this window and press Enter.
  echo.
  set /p "BARONY=  Barony folder: "
  set "BARONY=!BARONY:"=!"
)

if not exist "!BARONY!\barony.exe" (
  echo.
  echo   That folder does not contain barony.exe, so it is not the right one.
  echo   Nothing has been changed. Run this again when you have the path.
  echo.
  pause
  exit /b 1
)

echo   Found Barony:
echo     !BARONY!
echo.

rem --- 4. Write the launcher. The mod folder stays wherever you put it. -------
> "%~dp0Play Adorcism.bat" echo @echo off
>>"%~dp0Play Adorcism.bat" echo rem Written by Setup.bat. Re-run Setup if you move your Steam library.
>>"%~dp0Play Adorcism.bat" echo cd /d "!BARONY!"
>>"%~dp0Play Adorcism.bat" echo start "" "%%~dp0Adorcism.exe" %%*

echo   Created "Play Adorcism.bat".
echo.

rem --- 5. A desktop shortcut, because a .bat in a folder is easy to lose. -----
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Adorcism.lnk');" ^
  "$s.TargetPath='%~dp0Play Adorcism.bat'; $s.WorkingDirectory='%~dp0'; $s.Description='Barony with Adorcism'; $s.Save()" >nul 2>&1
if errorlevel 1 (
  echo   ^(Could not make a desktop shortcut - no matter, use "Play Adorcism.bat".^)
) else (
  echo   Added "Adorcism" to your desktop.
)

echo.
echo   Done. Start Steam, then run Adorcism from your desktop.
echo   Your host needs to be hosting; you just join their game as normal.
echo.

rem --- 6. Optional self-removal. -----------------------------------------------
rem  Off by default on purpose. Keeping Setup.bat costs nothing and re-running it
rem  is the fix if you ever move your Steam library or reinstall Barony -- and a
rem  file you can read is easier to trust than one that erases itself.
set "GONE="
set /p "GONE=  Delete this setup script now? (y/N): "
if /i "!GONE!"=="y" (
  echo   Removing setup script.
  pause
  del "%~f0"
  exit /b 0
)
pause
exit /b 0
