@echo off
cd /d "%~dp0"

if /i "%~1"=="serviciu" goto serviciu

set "CHEIE=test-cheie"

set "ANALIZA_API_KEY=%CHEIE%"
set "ANALIZA_HOST=127.0.0.1"
set "ANALIZA_PORT=8077"
set "ANALIZA_LUCRATORI=2"
set "ANALIZA_REPORNIRE=1"
set "OLLAMA_MODEL=qwen3.5:9b"

start "Zivro - produse si servicii (8077)" cmd /k call "%~f0" serviciu "produse si servicii (8077)" "PASUL-II-filtrare.py"

set "CONTACT_API_KEY=%CHEIE%"
set "CONTACT_HOST=127.0.0.1"
set "CONTACT_PORT=8078"
set "CONTACT_LUCRATORI=1"
set "CONTACT_REPORNIRE=1"
set "CHROME_INVIZIBIL=1"

start "Zivro - date de contact (8078)" cmd /k call "%~f0" serviciu "date de contact (8078)" "Detectare_adresa_server_rapida.py"

goto :eof

:serviciu
set "ETICHETA=%~2"
set "SCRIPT=%~3"

title Zivro - %ETICHETA%

:bucla
echo.
echo ============================================================
echo  %ETICHETA% - pornesc %SCRIPT%   (%date% %time%)
echo ============================================================
echo.

python "%SCRIPT%" --server
set "COD=%ERRORLEVEL%"

if "%COD%"=="7" goto repornire

echo.
echo ------------------------------------------------------------
echo  %ETICHETA% - serviciul s-a oprit (cod %COD%), nu a fost o analiza.
echo  Nu repornesc automat. Ruleaza din nou porneste-servicii.bat.
echo ------------------------------------------------------------

goto :eof

:repornire
echo.
echo ------------------------------------------------------------
echo  %ETICHETA% - analiza s-a terminat. Repornesc in 2 secunde
echo  si astept urmatoarea cerere.
echo ------------------------------------------------------------

"%SystemRoot%\System32\timeout.exe" /t 2 /nobreak >nul 2>&1
if errorlevel 1 "%SystemRoot%\System32\ping.exe" -n 3 127.0.0.1 >nul 2>&1

goto bucla
