@echo off
REM Двойной клик — портал запускается и открывается в браузере.
REM Требуется установленный Python 3 (python.org, галочка "Add to PATH").
cd /d "%~dp0"

echo Установка зависимостей (один раз)...
pip install -q -r requirements.txt

if not exist app.db (
  echo Загрузка фонда скважин (2420 скважин)...
  python -m app.import_csv
)

echo.
echo ============================================
echo   Портал открывается: http://localhost:8000
echo   Чтобы остановить — закройте это окно.
echo ============================================
echo.

REM Открыть браузер через ~5 секунд, когда сервер поднимется
start "" /b cmd /c "ping 127.0.0.1 -n 6 >nul & start http://localhost:8000"

python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
