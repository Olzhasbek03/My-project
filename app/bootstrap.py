"""Инициализация данных при первом запуске (в т.ч. на serverless-хостинге).

На Vercel файловая система read-only, поэтому SQLite живёт в /tmp
(см. app/config.py) и наполняется из data/wells_export.csv при холодном
старте. Функция идемпотентна.
"""
import logging
import os
import shutil

from . import config, models
from .database import Base, SessionLocal, engine

log = logging.getLogger("wellapp.bootstrap")


def ensure_data() -> None:
    # Serverless (Vercel): мгновенный холодный старт — копируем готовую demo.db
    # в эфемерную /tmp вместо ~5-секундного импорта CSV на каждом инстансе.
    if config.IS_SERVERLESS:
        tmp_db = "/tmp/app.db"
        prebuilt = os.path.join(os.path.dirname(__file__), "..", "data", "demo.db")
        if not os.path.exists(tmp_db) and os.path.exists(prebuilt):
            shutil.copy(prebuilt, tmp_db)
            log.info("Скопирована готовая demo.db → %s", tmp_db)

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        if db.query(models.Well).count() > 0:
            return
    csv_path = os.path.join(os.path.dirname(__file__), "..", "data",
                            "wells_export.csv")
    if os.path.exists(csv_path):
        from . import import_csv
        log.info("Пустая база — импорт %s", csv_path)
        import_csv.run(csv_path)
