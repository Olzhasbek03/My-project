"""Импорт реального фонда скважин из экспорта Optiwell Cloud (CSV).

Запуск:  python -m app.import_csv [путь_к_csv]
По умолчанию читает data/Optiwell_Cloud.csv.

Формат экспорта: Название Скважины, ГЗУ, Связь (unix-время последнего выхода
на связь), Статус работы (Running/Stop/пусто), Тип Скважины (Srp=ШГН /
Pcp=ВН), Qж (факт) т/сут, Qж (4 часа) т/сут, Рлин атм, Тлин °C, Работа ч,
Комментарии.
"""
import csv
import datetime as dt
import hashlib
import math
import sys

from . import models
from .database import Base, SessionLocal, engine

WELL_TYPE_MAP = {"Srp": "ШГН", "Pcp": "ВН"}

# Координаты района месторождения Каражанбас — используются для построения
# схематичной карты: каждой ГЗУ назначается детерминированный кластер,
# скважины распределяются вокруг него (до загрузки фактических координат).
BASE_LAT, BASE_LON = 45.38, 51.75


def _f(value: str) -> float | None:
    value = (value or "").strip()
    if not value or value in ("-", "null"):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _cluster(name: str, spread: float) -> tuple[float, float]:
    """Детерминированная точка по хэшу имени (стабильна между запусками)."""
    digest = hashlib.sha256(name.encode()).digest()
    dx = (int.from_bytes(digest[0:4], "big") / 2**32 - 0.5) * spread
    dy = (int.from_bytes(digest[4:8], "big") / 2**32 - 0.5) * spread
    return dy, dx


def run(path: str = "data/Optiwell_Cloud.csv") -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    cdn_cache: dict[str, models.Cdn] = {}
    gzu_cache: dict[str, models.Gzu] = {}
    imported = updated = 0

    for row in rows:
        number = row["Название Скважины"].strip()
        if not number:
            continue
        gzu_name = row["ГЗУ"].strip() or "—"

        # Цех (ЦДН) определяется по суффиксу площадки в имени ГЗУ:
        # «ГЗУ-12 птв» → цех ПТВ, «ГЗУ-27 п» → цех П, «ГЗУ-1 ввг» → цех ВВГ
        parts = gzu_name.split()
        area = parts[-1].upper() if len(parts) > 1 else "—"
        cdn_name = f"Цех {area}"
        cdn = cdn_cache.get(cdn_name)
        if cdn is None:
            cdn = (db.query(models.Cdn).filter(models.Cdn.name == cdn_name).first()
                   or models.Cdn(name=cdn_name))
            db.add(cdn)
            db.flush()
            cdn_cache[cdn_name] = cdn
        gzu = gzu_cache.get(gzu_name)
        if gzu is None:
            gzu = (db.query(models.Gzu).filter(models.Gzu.name == gzu_name).first()
                   or models.Gzu(name=gzu_name, cdn_id=cdn.id))
            db.add(gzu)
            db.flush()
            gzu_cache[gzu_name] = gzu

        glat, glon = _cluster(gzu_name, 0.28)
        wlat, wlon = _cluster(number, 0.035)

        well = db.query(models.Well).filter(models.Well.number == number).first()
        if well is None:
            well = models.Well(number=number)
            db.add(well)
            imported += 1
        else:
            updated += 1
        well.gzu_id = gzu.id
        well.well_type = WELL_TYPE_MAP.get(row["Тип Скважины"].strip(),
                                           row["Тип Скважины"].strip() or "ШГН")
        well.work_status = row["Статус работы"].strip()
        well.work_hours = row["Работа, ч"].strip() or "-"
        well.latitude = BASE_LAT + glat + wlat
        well.longitude = BASE_LON + glon + wlon
        db.flush()

        link_ts = _f(row["Связь"])
        last_seen = dt.datetime.utcfromtimestamp(link_ts) if link_ts else None
        dev = (db.query(models.Device)
               .filter(models.Device.well_id == well.id).first())
        if dev is None:
            dev = models.Device(dev_eui=f"70b3d5{well.id:010x}", well_id=well.id)
            db.add(dev)
        dev.last_seen_at = last_seen

        q_fact, q_4h = _f(row["Qж (факт), т/сут"]), _f(row["Qж (4 часа), т/сут"])
        rlin, tlin = _f(row["Рлин, атм"]), _f(row["Тлин, °C"])
        if last_seen is not None and q_fact is not None:
            # два замера: текущий и четырёхчасовой давности — базис для
            # расчёта дебита по интервалам
            if q_4h is not None:
                db.add(models.Measurement(
                    well_id=well.id, measured_at=last_seen - dt.timedelta(hours=4),
                    received_at=last_seen - dt.timedelta(hours=4),
                    flow_rate=q_4h, cumulative_total=0.0))
            db.add(models.Measurement(
                well_id=well.id, measured_at=last_seen, received_at=last_seen,
                flow_rate=q_fact, cumulative_total=0.0,
                pressure=rlin, temperature=tlin,
                alarm=row["Статус работы"].strip() == "Stop"))

        comment = row["Комментарии"].strip()
        if comment and comment not in ("-", "."):
            exists = (db.query(models.WellComment)
                      .filter(models.WellComment.well_id == well.id,
                              models.WellComment.text == comment).first())
            if not exists:
                db.add(models.WellComment(well_id=well.id, author="Импорт Optiwell Cloud",
                                          text=comment))

    # 14 базовых станций по ТЗ (п. 2.1.2), если ещё не созданы
    if db.query(models.Gateway).count() == 0:
        for i in range(14):
            glat, glon = _cluster(f"БС-{i + 1:02d}", 0.5)
            db.add(models.Gateway(gateway_id=f"gw{i:02d}aabbccddeeff",
                                  name=f"БС-{i + 1:02d}",
                                  latitude=BASE_LAT + glat, longitude=BASE_LON + glon))

    # администратор по умолчанию
    if db.query(models.User).count() == 0:
        from .auth import hash_password
        db.add(models.User(username="admin", full_name="Администратор платформы",
                           email="optiwell@kbm.kz",
                           password_hash=hash_password("admin"),
                           role=models.ROLE_ADMIN, receive_reports=True))

    db.commit()
    print(f"Импортировано скважин: {imported}, обновлено: {updated}; "
          f"ГЗУ: {db.query(models.Gzu).count()}, цехов: {db.query(models.Cdn).count()}")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "data/Optiwell_Cloud.csv")
