@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  Сбор Google News
echo  Не закрывайте это окно, пока нужна панель в браузере.
echo  Отчёты сохраняются в папку otchet
echo.
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY where python3 >nul 2>nul && set PY=python3
if not defined PY (
  echo Не найден Python. Установите Python 3 и повторите запуск.
  pause
  exit /b 1
)
%PY% server.py
if errorlevel 1 pause
