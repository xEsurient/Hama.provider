@echo off
rem Start the HAMA Plex metadata provider (needs Python 3.8+ from python.org or the Microsoft Store).
cd /d "%~dp0..\.."
where py >nul 2>nul && (py -3 run.py %*) || (python run.py %*)
