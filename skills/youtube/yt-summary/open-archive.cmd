@echo off
rem Starts the archive viewer server if it is not running yet and opens the archive in the browser.
python "%~dp0summary.py" start --open
if errorlevel 1 pause
