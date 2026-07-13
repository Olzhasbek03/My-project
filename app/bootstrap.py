"""Инициализация данных при первом запуске (в т.ч. на serverless-хостинге).

На Vercel файловая система read-only, поэтому SQLite живёт в /tmp
(см. app/config.py) и наполняется из data/Optiwell_Cloud.csv при холодном
старте. Функция идемпотентна.
"""
import logging
import os

from . import models
from .database import Base, SessionLocal, engine

log = logging.getLogger("optiwell.bootstrap")


def ensure_data() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        if db.query(models.Well).count() > 0:
            return
    csv_path = os.path.join(os.path.dirname(__file__), "..", "data",
                            "Optiwell_Cloud.csv")
    if os.path.exists(csv_path):
        from . import import_csv
        log.info("Пустая база — импорт %s", csv_path)
        import_csv.run(csv_path)
