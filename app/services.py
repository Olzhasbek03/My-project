"""Сервисные функции: статусы скважин, дебиты, 4-часовые интервалы, валидация."""
import datetime as dt

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import config, models


def utcnow() -> dt.datetime:
    return dt.datetime.utcnow()


def last_measurement(db: Session, well_id: int) -> models.Measurement | None:
    return (db.query(models.Measurement)
            .filter(models.Measurement.well_id == well_id)
            .order_by(models.Measurement.measured_at.desc())
            .first())


def link_anchor(db: Session) -> dt.datetime:
    """Опорное время для определения статуса связи — самый свежий выход на
    связь по всему фонду. При живом потоке данных совпадает с текущим
    временем; для статического среза не даёт всему фонду «протухнуть»."""
    latest = db.query(func.max(models.Device.last_seen_at)).scalar()
    return latest or utcnow()


def link_online(well: models.Well, anchor: dt.datetime) -> bool:
    """Статус связи («Связь»): выходил ли терминал на связь за последние
    OFFLINE_THRESHOLD_MINUTES относительно опорного времени."""
    dev = well.device
    if dev is None or dev.last_seen_at is None:
        return False
    return (anchor - dev.last_seen_at) <= dt.timedelta(
        minutes=config.OFFLINE_THRESHOLD_MINUTES)


def well_status(db: Session, well: models.Well,
                anchor: dt.datetime | None = None) -> str:
    """Сводный статус: offline (нет связи) / stopped (Stop, авария или нулевой
    дебит) / in_operation."""
    anchor = anchor or link_anchor(db)
    if not link_online(well, anchor):
        return "offline"
    if well.work_status == "Stop":
        return "stopped"
    m = last_measurement(db, well.id)
    if m is not None and (m.alarm or m.flow_rate <= 0):
        return "stopped"
    return "in_operation"


def latest_measurements(db: Session, well_ids: list[int] | None = None
                        ) -> dict[int, models.Measurement]:
    """Последнее измерение по каждой скважине одним запросом (для списков)."""
    sub = (db.query(models.Measurement.well_id,
                    func.max(models.Measurement.measured_at).label("mx"))
           .group_by(models.Measurement.well_id))
    if well_ids is not None:
        sub = sub.filter(models.Measurement.well_id.in_(well_ids))
    sub = sub.subquery()
    q = (db.query(models.Measurement)
         .join(sub, (models.Measurement.well_id == sub.c.well_id) &
                    (models.Measurement.measured_at == sub.c.mx)))
    return {m.well_id: m for m in q.all()}


def previous_flow(db: Session, well_ids: list[int] | None = None
                  ) -> dict[int, float]:
    """Предпоследний замер дебита по каждой скважине («Qж за 4 часа») —
    одним запросом для списков."""
    q = (db.query(models.Measurement.well_id, models.Measurement.flow_rate)
         .order_by(models.Measurement.well_id,
                   models.Measurement.measured_at.desc()))
    if well_ids is not None:
        q = q.filter(models.Measurement.well_id.in_(well_ids))
    result: dict[int, float] = {}
    seen: dict[int, int] = {}
    for wid, flow in q.all():
        n = seen.get(wid, 0)
        if n == 1:
            result[wid] = flow
        seen[wid] = n + 1
    return result


def bulk_statuses(db: Session, wells: list[models.Well]) -> dict[int, str]:
    """Сводные статусы для списков/карты без N+1 запросов."""
    anchor = link_anchor(db)
    last = latest_measurements(db, [w.id for w in wells])
    out: dict[int, str] = {}
    for w in wells:
        if not link_online(w, anchor):
            out[w.id] = "offline"
            continue
        m = last.get(w.id)
        if w.work_status == "Stop" or (m is not None and
                                       (m.alarm or m.flow_rate <= 0)):
            out[w.id] = "stopped"
        else:
            out[w.id] = "in_operation"
    return out


def rate_for_period(db: Session, well_id: int,
                    start: dt.datetime, end: dt.datetime) -> float:
    """Средний дебит (м³/сут) за период по измерениям."""
    avg = (db.query(func.avg(models.Measurement.flow_rate))
           .filter(models.Measurement.well_id == well_id,
                   models.Measurement.measured_at >= start,
                   models.Measurement.measured_at < end)
           .scalar())
    return round(avg or 0.0, 2)


def volume_for_period(db: Session, well_id: int,
                      start: dt.datetime, end: dt.datetime) -> float:
    """Объём за период (м³): средний дебит × доля суток."""
    hours = (end - start).total_seconds() / 3600
    return round(rate_for_period(db, well_id, start, end) * hours / 24, 2)


def previous_day_bounds(now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    now = now or utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start - dt.timedelta(days=1), day_start


def four_hour_intervals(day_start: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
    """Разбивка суток на шесть 4-часовых интервалов (п. 2.1.3.3 ТЗ)."""
    return [(day_start + dt.timedelta(hours=h), day_start + dt.timedelta(hours=h + 4))
            for h in range(0, 24, 4)]


def four_hour_rates(db: Session, well_id: int, day_start: dt.datetime) -> list[float]:
    return [volume_for_period(db, well_id, s, e)
            for s, e in four_hour_intervals(day_start)]


def rate_drop_pct(db: Session, well_id: int, now: dt.datetime | None = None) -> float:
    """Снижение дебита за последние 4 часа относительно предыдущих 4 часов, %."""
    now = now or utcnow()
    recent = rate_for_period(db, well_id, now - dt.timedelta(hours=4), now)
    prior = rate_for_period(db, well_id, now - dt.timedelta(hours=8),
                            now - dt.timedelta(hours=4))
    if prior <= 0:
        return 0.0
    return round((prior - recent) / prior * 100, 1)


def offline_devices(db: Session) -> list[models.Device]:
    threshold = utcnow() - dt.timedelta(minutes=config.OFFLINE_THRESHOLD_MINUTES)
    return (db.query(models.Device)
            .filter((models.Device.last_seen_at.is_(None)) |
                    (models.Device.last_seen_at < threshold))
            .all())


def validate_measurement(m: models.Measurement, prev: models.Measurement | None) -> bool:
    """Валидация данных: накопленный расход не убывает, дебит неотрицателен,
    время опроса не из будущего."""
    if m.flow_rate < 0:
        return False
    if m.measured_at > utcnow() + dt.timedelta(minutes=5):
        return False
    if prev is not None and m.cumulative_total < prev.cumulative_total:
        return False
    return True


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(models.Setting, key)
    return row.value if row else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(models.Setting, key)
    if row is None:
        row = models.Setting(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()
