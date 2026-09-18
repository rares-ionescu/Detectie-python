@echo off
cd /d "%~dp0"

set "ETICHETA=%~1"
set "SCRIPT=%~2"

title Zivro - %ETICHETA%

:bucla
echo.
echo ============================================================
echo  %ETICHETA% - pornesc %SCRIPT%   (%date% %time%)
echo ============================================================
echo.

python "%SCRIPT%" --server

echo.
echo ------------------------------------------------------------
echo  %ETICHETA% - serviciul s-a oprit. Repornesc in 2 secunde...
echo  (inchide fereastra sau Ctrl+C de doua ori ca sa opresti)
echo ------------------------------------------------------------

"%SystemRoot%\System32\timeout.exe" /t 2 /nobreak >nul 2>&1
if errorlevel 1 "%SystemRoot%\System32\ping.exe" -n 3 127.0.0.1 >nul 2>&1
goto bucla
