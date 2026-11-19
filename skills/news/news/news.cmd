@echo off
rem Downloads the news and opens the overview, without Claude.
rem Items Claude has not summarized show the perex of the source.
chcp 65001 >nul
python "%~dp0news.py" fetch && python "%~dp0news.py" render --open
if errorlevel 1 pause
