#!/usr/bin/env bash
# Точка входа контейнера: дождаться СУБД, наполнить фонд скважин при первом
# запуске (идемпотентно), затем передать управление CMD (ASGI-серверу).
set -e

# Ожидание PostgreSQL, если строка подключения указывает на postgresql://
if [[ "${WELLAPP_DATABASE_URL:-}" == postgresql* ]]; then
  echo "Ожидание PostgreSQL…"
  python - <<'PY'
import os, time, sqlalchemy
url = os.environ["WELLAPP_DATABASE_URL"]
for attempt in range(60):
    try:
        sqlalchemy.create_engine(url).connect().close()
        print("PostgreSQL доступен")
        break
    except Exception as exc:
        print(f"  …ещё не готов ({attempt+1}/60): {exc.__class__.__name__}")
        time.sleep(2)
else:
    raise SystemExit("PostgreSQL недоступен — прекращаю запуск")
PY
fi

# Наполнение фонда скважин из data/wells_export.csv при пустой базе
python -c "from app.bootstrap import ensure_data; ensure_data()"

exec "$@"
