"""Инициализация данных при первом запуске (в т.ч. на serverless-хостинге).

На Vercel файловая система read-only, поэтому SQLite живёт в /tmp
(см. app/config.py) и наполняется из data/wells_export.csv при холодном
старте. Функция идемпотентна.
"""
import datetime as dt
import logging
import os
import shutil

from sqlalchemy import func, text

from . import config, models
from .database import Base, SessionLocal, engine

log = logging.getLogger("wellapp.bootstrap")

# Витрина считается «устаревшей», если самые свежие данные старше этого
# порога — тогда все временные метки сдвигаются вперёд одним смещением.
_STALE_AFTER = dt.timedelta(hours=6)


def _refresh_stale_timestamps() -> None:
    """Демо-режим (SQLite): поддерживать витрину «живой».

    Модуль радиоанализа и 4-часовые интервалы смотрят на окно последних
    24 часов; предсобранная demo.db со временем «стареет» и страницы
    пустеют. Если новейшая запись старше порога — сдвигаем все временные
    метки вперёд на одно общее смещение, сохраняя относительные интервалы.
    Продуктив (PostgreSQL, живые данные ChirpStack) не затрагивается.
    """
    if engine.dialect.name != "sqlite":
        return
    with SessionLocal() as db:
        candidates = [
            db.query(func.max(models.UplinkFrame.received_at)).scalar(),
            db.query(func.max(models.Measurement.received_at)).scalar(),
        ]
        newest = max((d for d in candidates if d is not None), default=None)
        if newest is None:
            return
        age = dt.datetime.utcnow() - newest
        if age < _STALE_AFTER:
            return
        shift = int(age.total_seconds()) - 300  # новейшая точка = «5 минут назад»
        stmts = [
            "UPDATE measurement SET measured_at = datetime(measured_at, '+' || :s || ' seconds'), "
            "received_at = datetime(received_at, '+' || :s || ' seconds')",
            "UPDATE uplink_frame SET received_at = datetime(received_at, '+' || :s || ' seconds')",
            "UPDATE device SET last_seen_at = datetime(last_seen_at, '+' || :s || ' seconds') "
            "WHERE last_seen_at IS NOT NULL",
            "UPDATE gateway SET last_seen_at = datetime(last_seen_at, '+' || :s || ' seconds') "
            "WHERE last_seen_at IS NOT NULL",
        ]
        for sql in stmts:
            db.execute(text(sql), {"s": shift})
        db.commit()
        log.info("Витрина освежена: метки сдвинуты на %d с вперёд", shift)


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
        has_wells = db.query(models.Well).count() > 0
    if not has_wells:
        csv_path = os.path.join(os.path.dirname(__file__), "..", "data",
                                "wells_export.csv")
        if os.path.exists(csv_path):
            from . import import_csv
            log.info("Пустая база — импорт %s", csv_path)
            import_csv.run(csv_path)

    _refresh_stale_timestamps()
