"""Модуль автоматизированного радиоанализа сети LoRaWAN (п. 2.1.3.4 ТЗ).

Реализован как самостоятельный программный модуль поверх собственной таблицы
uplink-кадров и НЕ использует средства ChirpStack (в т.ч. v3.16.106):

a) сила радиосигнала и уровень помех по всем БС/шлюзам сети;
b) ранжирование наименее производительных базовых станций;
c) терминалы, подключённые не к ближайшей базовой станции;
d) зоны со слабым качеством сигнала;
e) пустые пакеты данных;
f) расстояние от оконечного терминала до базовой станции.
"""
import datetime as dt
import math

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models

WEAK_RSSI_DBM = -120  # порог слабого сигнала
ANALYSIS_WINDOW_HOURS = 24


def _window_start() -> dt.datetime:
    return dt.datetime.utcnow() - dt.timedelta(hours=ANALYSIS_WINDOW_HOURS)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 2)


def gateway_signal_stats(db: Session) -> list[dict]:
    """(a) Средний RSSI/SNR и число кадров по каждой БС; SNR ниже нуля
    указывает на уровень радиопомех."""
    rows = (db.query(models.UplinkFrame.gateway_id,
                     func.avg(models.UplinkFrame.rssi),
                     func.avg(models.UplinkFrame.snr),
                     func.count(models.UplinkFrame.id))
            .filter(models.UplinkFrame.received_at >= _window_start())
            .group_by(models.UplinkFrame.gateway_id)
            .all())
    gateways = {g.gateway_id: g for g in db.query(models.Gateway).all()}
    stats = []
    for gw_id, rssi, snr, frames in rows:
        gw = gateways.get(gw_id)
        stats.append({
            "gateway_id": gw_id,
            "name": gw.name if gw else gw_id,
            "avg_rssi": round(rssi or 0, 1),
            "avg_snr": round(snr or 0, 1),
            "frames": frames,
            "is_online": gw.is_online if gw else False,
        })
    # БС без единого кадра за окно анализа тоже показываем
    for gw_id, gw in gateways.items():
        if not any(s["gateway_id"] == gw_id for s in stats):
            stats.append({"gateway_id": gw_id, "name": gw.name, "avg_rssi": 0,
                          "avg_snr": 0, "frames": 0, "is_online": gw.is_online})
    return sorted(stats, key=lambda s: s["avg_rssi"])


def worst_gateways(db: Session, top_n: int = 5) -> list[dict]:
    """(b) Ранжирование наименее производительных БС: мало кадров и слабый
    средний сигнал."""
    stats = gateway_signal_stats(db)
    ranked = sorted(stats, key=lambda s: (s["frames"], s["avg_rssi"]))
    return ranked[:top_n]


def terminals_on_wrong_gateway(db: Session) -> list[dict]:
    """(c) Терминалы, отправляющие данные не через ближайшую БС."""
    gateways = db.query(models.Gateway).all()
    devices = (db.query(models.Device)
               .filter(models.Device.well_id.isnot(None)).all())
    result = []
    for dev in devices:
        frame = (db.query(models.UplinkFrame)
                 .filter(models.UplinkFrame.dev_eui == dev.dev_eui)
                 .order_by(models.UplinkFrame.received_at.desc())
                 .first())
        if frame is None or dev.well is None:
            continue
        distances = {g.gateway_id: haversine_km(dev.well.latitude, dev.well.longitude,
                                                g.latitude, g.longitude)
                     for g in gateways}
        if not distances:
            continue
        nearest_id = min(distances, key=distances.get)
        if nearest_id != frame.gateway_id:
            result.append({
                "dev_eui": dev.dev_eui,
                "well": dev.well.number,
                "used_gateway": frame.gateway_id,
                "used_distance_km": distances.get(frame.gateway_id, 0.0),
                "nearest_gateway": nearest_id,
                "nearest_distance_km": distances[nearest_id],
            })
    return result


def weak_signal_zones(db: Session) -> list[dict]:
    """(d) Скважины в зоне слабого сигнала (средний RSSI ниже порога)."""
    rows = (db.query(models.UplinkFrame.dev_eui,
                     func.avg(models.UplinkFrame.rssi))
            .filter(models.UplinkFrame.received_at >= _window_start())
            .group_by(models.UplinkFrame.dev_eui)
            .having(func.avg(models.UplinkFrame.rssi) < WEAK_RSSI_DBM)
            .all())
    devices = {d.dev_eui: d for d in db.query(models.Device).all()}
    zones = []
    for dev_eui, rssi in rows:
        dev = devices.get(dev_eui)
        well = dev.well if dev else None
        zones.append({
            "dev_eui": dev_eui,
            "well": well.number if well else "—",
            "latitude": well.latitude if well else None,
            "longitude": well.longitude if well else None,
            "avg_rssi": round(rssi, 1),
        })
    return zones


def empty_packets(db: Session) -> list[dict]:
    """(e) Пустые пакеты данных за окно анализа."""
    rows = (db.query(models.UplinkFrame.dev_eui, func.count(models.UplinkFrame.id))
            .filter(models.UplinkFrame.received_at >= _window_start(),
                    models.UplinkFrame.payload_size == 0)
            .group_by(models.UplinkFrame.dev_eui)
            .all())
    devices = {d.dev_eui: d for d in db.query(models.Device).all()}
    return [{"dev_eui": dev_eui,
             "well": devices[dev_eui].well.number
             if dev_eui in devices and devices[dev_eui].well else "—",
             "count": count}
            for dev_eui, count in rows]


def terminal_distances(db: Session) -> list[dict]:
    """(f) Расстояние от каждого терминала до БС, через которую идут данные."""
    result = []
    for dev in db.query(models.Device).filter(models.Device.well_id.isnot(None)).all():
        frame = (db.query(models.UplinkFrame)
                 .filter(models.UplinkFrame.dev_eui == dev.dev_eui)
                 .order_by(models.UplinkFrame.received_at.desc())
                 .first())
        if frame is None:
            continue
        result.append({
            "dev_eui": dev.dev_eui,
            "well": dev.well.number if dev.well else "—",
            "gateway_id": frame.gateway_id,
            "distance_km": frame.distance_km,
            "rssi": frame.rssi,
        })
    return sorted(result, key=lambda r: -r["distance_km"])
