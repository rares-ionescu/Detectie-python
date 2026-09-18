@echo off
cd /d "%~dp0"

set "CHEIE=test-cheie"

set "ANALIZA_API_KEY=%CHEIE%"
set "ANALIZA_HOST=127.0.0.1"
set "ANALIZA_PORT=8077"
set "ANALIZA_LUCRATORI=2"
set "ANALIZA_REPORNIRE=1"
set "OLLAMA_MODEL=qwen3.5:9b"

start "Zivro - produse si servicii (8077)" cmd /k call "%~dp0ruleaza-serviciu.bat" "produse si servicii (8077)" "PASUL-II-filtrare.py"

set "CONTACT_API_KEY=%CHEIE%"
set "CONTACT_HOST=127.0.0.1"
set "CONTACT_PORT=8078"
set "CONTACT_LUCRATORI=1"
set "CONTACT_REPORNIRE=1"
set "CHROME_INVIZIBIL=1"

start "Zivro - date de contact (8078)" cmd /k call "%~dp0ruleaza-serviciu.bat" "date de contact (8078)" "Detectare_adresa_server_rapida.py"
