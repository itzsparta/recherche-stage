@echo off
rem Double-clic : ouvre l'interface de recherche de stage dans le navigateur.
cd /d "%~dp0"
if exist ".venv\Scripts\rs.exe" (".venv\Scripts\rs.exe" interface) else (rs interface)
pause
