#!/usr/bin/env bash
# Запуск портала одной командой: ./run.sh — откроется http://localhost:8000
# (для macOS / Linux; на Windows используйте run.bat)
set -e
cd "$(dirname "$0")"

echo "Установка зависимостей (один раз)…"
python3 -m pip install -q -r requirements.txt

if [ ! -f app.db ]; then
  echo "Загрузка фонда скважин (2420 скважин)…"
  python3 -m app.import_csv
fi

echo
echo "============================================"
echo "  Портал открывается: http://localhost:8000"
echo "  Чтобы остановить — нажмите Ctrl+C."
echo "============================================"
echo

# Открыть браузер через ~4 секунды, когда сервер поднимется
( sleep 4; python3 -c "import webbrowser; webbrowser.open('http://localhost:8000')" ) &

python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
