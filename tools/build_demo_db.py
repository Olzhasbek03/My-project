#!/usr/bin/env python3
"""Сборка готовой демо-БД (data/demo.db) для витрины интерфейса.

Импортирует реальный фонд (2420 скважин, измерения, ГЗУ, БС) и дополняет его
синтетическими uplink-кадрами, чтобы наполнить модуль радиоанализа (RSSI/SNR,
худшие БС, расстояния терминал–БС, пустые пакеты). На serverless (Vercel) эта
БД просто копируется в /tmp за миллисекунды — холодный старт мгновенный,
вместо ~5 c импорта CSV на каждом инстансе.

CSV даёт по скважине только два замера (сейчас и 4 ч назад), поэтому для
вкладки «Прогнозы» (окно 24 ч, тренд) ряд дополняется СИНТЕТИЧЕСКОЙ историей:
замеры каждые 4 ч на 48 ч назад вокруг замера «4 ч назад». Два реальных
последних замера не меняются, так что списки и карточки скважин те же;
скважины без замера «4 ч назад» историю не получают.

Запуск:  python -m tools.build_demo_db
"""
import datetime as dt
import os
import random

# Демо-БД строится в фиксированном файле рядом с CSV
os.environ.setdefault("WELLAPP_DATABASE_URL", "sqlite:///./data/demo.db")

from app.database import Base, SessionLocal, engine  # noqa: E402
from app import models, radio  # noqa: E402
from app.bootstrap import ensure_data  # noqa: E402


def build() -> None:
    # чистая сборка
    db_path = "data/demo.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    Base.metadata.create_all(bind=engine)

    # реальный фонд + измерения (идемпотентный импорт из data/wells_export.csv)
    ensure_data()

    db = SessionLocal()
    if db.query(models.UplinkFrame).count() > 0:
        db.close()
        return

    rnd = random.Random(42)  # детерминированно
    _add_history(db, random.Random(7))  # отдельный генератор: радио-данные прежние
    gateways = db.query(models.Gateway).all()
    devices = db.query(models.Device).filter(
        models.Device.well_id.isnot(None)).all()
    frames = []
    for dev in devices:
        well = dev.well
        if not well:
            continue
        # ближайшие БС по расстоянию
        ranked = sorted(
            gateways,
            key=lambda g: radio.haversine_km(well.latitude, well.longitude,
                                             g.latitude, g.longitude))
        near = ranked[:3]
        for i, gw in enumerate(near):
            dist = radio.haversine_km(well.latitude, well.longitude,
                                      gw.latitude, gw.longitude)
            # RSSI падает с расстоянием + шум; SNR аналогично
            rssi = -60 - dist * 1.6 - i * 8 + rnd.uniform(-6, 6)
            snr = 9 - dist * 0.25 - i * 3 + rnd.uniform(-3, 3)
            # часть пакетов «пустые» (payload_size=0) — для п. 2.1.3.4-e
            empty = rnd.random() < 0.05
            frames.append(models.UplinkFrame(
                dev_eui=dev.dev_eui, gateway_id=gw.gateway_id,
                rssi=round(rssi, 1), snr=round(snr, 1),
                frequency=rnd.choice([865.1, 865.3, 865.5, 865.7, 865.9]),
                payload_size=0 if empty else rnd.choice([9, 13]),
                distance_km=round(dist, 3)))
    db.add_all(frames)
    db.commit()
    print(f"uplink-кадров создано: {len(frames)}")
    print(f"скважин: {db.query(models.Well).count()}, "
          f"измерений: {db.query(models.Measurement).count()}, "
          f"БС: {db.query(models.Gateway).count()}")
    db.close()


HISTORY_HOURS = 48
HISTORY_STEP_H = 4


def _add_history(db, rnd: random.Random) -> None:
    """Синтетическая история дебита: 48 ч до замера «4 ч назад».

    Профили: ~80% стабильные (шум ±6%), ~12% с плавным спадом (уровень
    48 ч назад выше на 20–60%), ~8% с ростом. Остановленные/нулевые — нули.
    """
    from collections import defaultdict
    by_well = defaultdict(list)
    for m in (db.query(models.Measurement)
              .order_by(models.Measurement.measured_at.desc())):
        by_well[m.well_id].append(m)
    added = []
    for wid, ms in by_well.items():
        if len(ms) < 2:
            continue  # нет «Qж за 4 часа» в CSV — не выдумываем его
        base = ms[1]
        q0 = base.flow_rate or 0.0
        r = rnd.random()
        k = (rnd.uniform(0.2, 0.6) if r < 0.12
             else -rnd.uniform(0.15, 0.4) if r < 0.20 else 0.0)
        for h in range(HISTORY_STEP_H, HISTORY_HOURS + 1, HISTORY_STEP_H):
            t = base.measured_at - dt.timedelta(hours=h)
            q = 0.0 if q0 <= 0 else max(
                0.0, q0 * (1 + k * h / HISTORY_HOURS) * (1 + rnd.uniform(-0.06, 0.06)))
            added.append(models.Measurement(
                well_id=wid, measured_at=t, received_at=t,
                flow_rate=round(q, 3), cumulative_total=0.0,
                pressure=base.pressure, temperature=base.temperature,
                alarm=base.alarm))
    db.add_all(added)
    db.commit()
    print(f"синтетических замеров истории: {len(added)}")


if __name__ == "__main__":
    build()
