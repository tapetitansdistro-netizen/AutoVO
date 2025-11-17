@echo off
setlocal ENABLEDELAYEDEXPANSION

rem ============================
rem CONFIG: base refs directory
rem ============================
rem Change this to wherever you want your *_refs folders to live.
rem If you mirror /home/administrator/voices into Windows, point this there.
rem Example:
rem   set "REF_ROOT=D:\voices"
rem or a WSL share:
rem   set "REF_ROOT=\\wsl$\Nobara\home\administrator\voices"

set "REF_ROOT=\\wsl.localhost\Ubuntu\home\administrator\voices\"

if "%REF_ROOT%"=="" (
    echo REF_ROOT is not set. Edit this batch file and set REF_ROOT at the top.
    pause
    goto :eof
)

rem ============================
rem ARG CHECK
rem ============================

if "%~1"=="" (
    echo Drag a .wav file onto this script to add it as a VoxCPM seed.
    pause
    goto :eof
)

set "SRC=%~1"

if /I not "%~x1"==".wav" (
    echo Input file is not a .wav: "%SRC%"
    pause
    goto :eof
)

if not exist "%SRC%" (
    echo File not found: "%SRC%"
    pause
    goto :eof
)

echo Source WAV: "%SRC%"
echo.

rem ============================
rem ASK FOR DLG / CHARACTER
rem ============================

set "DLG="

set /p DLG=Enter DLG name (e.g. DMORTE, DANNAH, DAKKON): 

if "%DLG%"=="" (
    echo No DLG name entered. Aborting.
    pause
    goto :eof
)

rem strip surrounding quotes if any
set "DLG=%DLG:"=%"

rem ============================
rem COMPUTE VOICE PREFIX (matches Python logic)
rem   - If name starts with D + letter, drop leading D
rem     DMORTE  -> MORTE
rem     DANNAH  -> ANNAH
rem   - Else use full name
rem ============================

set "BASE=%DLG%"
if /I "%BASE:~0,1%"=="D" if not "%BASE:~1%"=="" (
    rem Drop the leading D; second char is assumed alphabetic for these DLGs
    set "BASE=%BASE:~1%"
)

set "VOICEPREFIX=%BASE%"
rem refs dir: <voiceprefix>_refs (no lowercase conversion needed on Windows)
set "REFDIR=%REF_ROOT%\d%VOICEPREFIX%_refs"

echo Using voice prefix: "%VOICEPREFIX%"
echo Target refs folder: "%REFDIR%"
echo.

rem ============================
rem ENSURE TARGET FOLDER EXISTS
rem ============================

if not exist "%REFDIR%" (
    echo Creating folder "%REFDIR%" ...
    mkdir "%REFDIR%"
    if errorlevel 1 (
        echo Failed to create "%REFDIR%".
        pause
        goto :eof
    )
) else (
    echo Folder already exists.
)

rem ============================
rem BUILD TARGET FILENAME
rem   - Start from original base name
rem   - If it collides, append _1, _2, ...
rem ============================

set "BASE_NAME=%~n1"
set "TARGET_BASE=%BASE_NAME%"
set "TARGET_WAV=%REFDIR%\%TARGET_BASE%.wav"

set /a N=1
:CHECK_COLLISION
if exist "%TARGET_WAV%" (
    set /a N+=1
    set "TARGET_BASE=%BASE_NAME%_%N%"
    set "TARGET_WAV=%REFDIR%\%TARGET_BASE%.wav"
    goto :CHECK_COLLISION
)

set "TARGET_TXT=%REFDIR%\%TARGET_BASE%.txt"

rem ============================
rem COPY FILE AND CREATE BLANK TXT
rem ============================

echo Copying WAV to:
echo   "%TARGET_WAV%"
copy /Y "%SRC%" "%TARGET_WAV%" >nul
if errorlevel 1 (
    echo Failed to copy WAV.
    pause
    goto :eof
)

if not exist "%TARGET_TXT%" (
    echo Creating blank transcript file:
    echo   "%TARGET_TXT%"
    type nul > "%TARGET_TXT%"
) else (
    echo Transcript file already exists:
    echo   "%TARGET_TXT%"
)

echo.
echo Done.
echo   Character (DLG): %DLG%
echo   Voice prefix   : %VOICEPREFIX%
echo   Seed WAV       : %TARGET_WAV%
echo   Seed TXT       : %TARGET_TXT%
echo.
pause

endlocal
goto :eof
