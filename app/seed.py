"""Наполнение базы демонстрационными данными месторождения Каражанбас.

Запуск:  python -m app.seed
Создаёт иерархию ЦДН → ГЗУ → скважины, терминалы, 14 базовых станций,
двое суток измерений и uplink-кадры для радиоанализа, а также пользователей
всех трёх уровней доступа (admin / ЦДН / ГЗУ).
"""
import datetime as dt
import random

from . import models
from .auth import hash_password
from .database import Base, SessionLocal, engine

# Координаты района месторождения Каражанбас (Мангистауская область)
BASE_LAT, BASE_LON = 45.38, 51.75

random.seed(42)


def run(wells_per_gzu: int = 6) -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    if db.query(models.Well).count():
        print("База уже содержит данные — пропуск")
        return

    now = dt.datetime.utcnow()

    gateways = []
    for i in range(14):
        gw = models.Gateway(
            gateway_id=f"gw{i:02d}aabbccddeeff",
            name=f"БС-{i + 1:02d}",
            latitude=BASE_LAT + random.uniform(-0.12, 0.12),
            longitude=BASE_LON + random.uniform(-0.18, 0.18),
            is_online=i != 13,  # одна БС оффлайн для демонстрации
            last_seen_at=now,
        )
        gateways.append(gw)
        db.add(gw)

    cdns, gzus, wells = [], [], []
    devices_by_well: dict[int, models.Device] = {}
    for c in range(1, 4):
        cdn = models.Cdn(name=f"ЦДН-{c}")
        db.add(cdn)
        db.flush()
        cdns.append(cdn)
        for g in range(1, 4):
            gzu = models.Gzu(name=f"ГЗУ-{c}{g}", cdn_id=cdn.id)
            db.add(gzu)
            db.flush()
            gzus.append(gzu)
            for w in range(wells_per_gzu):
                num = f"{c}{g}{w + 1:02d}"
                fund = (models.FUND_INJECTION if w == wells_per_gzu - 1
                        else models.FUND_PRODUCING)
                well = models.Well(
                    number=num, gzu_id=gzu.id, fund=fund,
                    meter_type=random.choice(models.METER_TYPES),
                    latitude=BASE_LAT + random.uniform(-0.1, 0.1),
                    longitude=BASE_LON + random.uniform(-0.15, 0.15),
                    water_cut_pct=round(random.uniform(30, 90), 1),
                )
                db.add(well)
                db.flush()
                wells.append(well)
                dev = models.Device(
                    dev_eui=f"70b3d5{well.id:010x}",
                    serial_number=f"SN-{well.id:05d}",
                    well_id=well.id,
                    report_interval_sec=3600,
                    battery_v=round(random.uniform(3.2, 3.65), 2),
                    last_seen_at=now,
                )
                db.add(dev)
                devices_by_well[well.id] = dev

    # Двое суток измерений с шагом 1 час + uplink-кадры
    for well in wells:
        dev = devices_by_well[well.id]
        base_rate = random.uniform(5, 60) if well.fund == models.FUND_PRODUCING else random.uniform(40, 120)
        stopped = random.random() < 0.07
        # каждый ~18-й передатчик молчит 18 часов → статус «оффлайн»
        dead = well.id % 18 == 0
        total = random.uniform(1000, 90000)
        # ближайшая БС и «фактическая» БС (иногда не ближайшая — для п. c радиоанализа)
        from .radio import haversine_km
        nearest = min(gateways, key=lambda g: haversine_km(well.latitude, well.longitude,
                                                           g.latitude, g.longitude))
        used_gw = nearest if random.random() > 0.1 else random.choice(gateways)
        distance = haversine_km(well.latitude, well.longitude,
                                used_gw.latitude, used_gw.longitude)
        declining = not stopped and not dead and random.random() < 0.06
        last_hour = 18 if dead else 1  # «мёртвые» замолкают за 18 ч до now
        for h in range(48, last_hour - 1, -1):
            ts = now - dt.timedelta(hours=h)
            drift = random.uniform(0.85, 1.15)
            if declining and h <= 4:
                drift *= 0.4  # значительное снижение дебита за последние 4 часа
            rate = 0.0 if stopped else round(base_rate * drift, 2)
            total += rate / 24
            db.add(models.Measurement(
                well_id=well.id, measured_at=ts,
                received_at=ts + dt.timedelta(seconds=random.randint(3, 40)),
                flow_rate=rate, cumulative_total=round(total, 2),
                pressure=round(random.uniform(8, 25), 1),
                temperature=round(random.uniform(15, 45), 1),
                alarm=stopped,
            ))
            rssi = random.uniform(-135, -70) - distance * 1.5
            db.add(models.UplinkFrame(
                dev_eui=dev.dev_eui, gateway_id=used_gw.gateway_id,
                received_at=ts, rssi=round(rssi, 1),
                snr=round(random.uniform(-12, 8), 1),
                payload_size=0 if random.random() < 0.02 else 19,
                distance_km=distance,
            ))
        if dead:
            dev.last_seen_at = now - dt.timedelta(hours=18)

    # Пользователи трёх уровней доступа
    admin = models.User(username="admin", full_name="Администратор платформы",
                        email="optiwell@kbm.kz",
                        password_hash=hash_password("admin"),
                        role=models.ROLE_ADMIN, receive_reports=True)
    db.add(admin)
    cdn_user = models.User(username="cdn1", full_name="Начальник ЦДН-1",
                           email="cdn1@example.kz",
                           password_hash=hash_password("cdn1"),
                           role=models.ROLE_CDN, receive_reports=True)
    db.add(cdn_user)
    db.flush()
    db.add(models.UserScope(user_id=cdn_user.id, level="cdn", object_id=cdns[0].id))
    gzu_user = models.User(username="gzu11", full_name="Оператор ГЗУ-11",
                           email="gzu11@example.kz",
                           password_hash=hash_password("gzu11"),
                           role=models.ROLE_GZU, receive_reports=True)
    db.add(gzu_user)
    db.flush()
    db.add(models.UserScope(user_id=gzu_user.id, level="gzu", object_id=gzus[0].id))

    db.add(models.WellComment(well_id=wells[0].id, author="Оператор ГЗУ-11",
                              text="Заменён электроконтактный манометр, замерное хозяйство исправно"))
    db.commit()
    print(f"Создано: {len(cdns)} ЦДН, {len(gzus)} ГЗУ, {len(wells)} скважин, "
          f"{len(gateways)} БС. Пользователи: admin/admin, cdn1/cdn1, gzu11/gzu11")


if __name__ == "__main__":
    run()
