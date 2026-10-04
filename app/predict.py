"""Предиктивная аналитика: динамика добычи и прогноз (страница /predict).

Три уровня детализации:
  • флот → изменение дебита каждого ГЗУ за окно (4 ч или 24 ч);
  • ГЗУ → изменение по каждой скважине группы;
  • скважина → ряд замеров, линейный тренд и прогноз на следующее окно.

Сравнение честное по данным: «сейчас» — последний замер, «было» — замер,
ближайший к (сейчас − окно) в пределах допуска PAIR_TOLERANCE. Скважины без
такой пары или без свежего замера помечаются «нет данных», а не нулём.

Время отсчитывается от самого свежего замера по фонду (как и статус связи в
services.link_anchor), а не от часов сервера — иначе статичная витрина
со временем «протухает».
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models

# насколько глубоко смотрим назад: 2 × макс. окно (для тренда 24 ч) + запас
LOOKBACK_HOURS = 72
# «было» должно отстоять от «сейчас» на окно ± эта доля окна
PAIR_TOLERANCE = 0.25
# по скольким окнам назад строится тренд для прогноза (24 ч × 3 = LOOKBACK)
FIT_WINDOWS = 3
# критические значения t-Стьюдента (95%, двусторонний) по числу степеней
# свободы n−2: тренд значим, если |наклон| > t × его ст. ошибка
_T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36,
        8: 2.31, 9: 2.26, 10: 2.23}


def _t95(dof: int) -> float:
    return _T95.get(dof, 2.0)


def data_anchor(db: Session) -> dt.datetime:
    """Опорное «сейчас» — самый свежий замер по фонду."""
    latest = db.query(func.max(models.Measurement.measured_at)).scalar()
    return latest or dt.datetime.utcnow()


def _measurements_by_well(db: Session, well_ids: list[int],
                          anchor: dt.datetime) -> dict[int, list]:
    """Валидные замеры скважин за LOOKBACK_HOURS до anchor, свежие первыми."""
    if not well_ids:
        return {}
    since = anchor - dt.timedelta(hours=LOOKBACK_HOURS)
    rows = (db.query(models.Measurement.well_id,
                     models.Measurement.measured_at,
                     models.Measurement.flow_rate)
            .filter(models.Measurement.well_id.in_(well_ids),
                    models.Measurement.measured_at >= since,
                    models.Measurement.is_valid.isnot(False))
            .order_by(models.Measurement.measured_at.desc())
            .all())
    out: dict[int, list] = defaultdict(list)
    for wid, t, q in rows:
        out[wid].append((t, q))
    return out


def _pair(series: list, window_h: int, anchor: dt.datetime):
    """(q_now, q_prev, stale) по ряду замеров (свежие первыми).

    q_now — последний замер; stale=True, если он старше anchor − окно (тогда
    «сейчас» неизвестно и изменение не считается). q_prev — замер, ближайший к
    (t_now − окно), но не дальше допуска; иначе None.
    """
    if not series:
        return None, None, False
    t1, q1 = series[0]
    win = dt.timedelta(hours=window_h)
    if t1 < anchor - win:
        return q1, None, True
    target, tol = t1 - win, win * PAIR_TOLERANCE
    best = None
    for t, q in series[1:]:
        gap = abs(t - target)
        if gap <= tol and (best is None or gap < best[0]):
            best = (gap, q)
    return q1, (best[1] if best else None), False


def well_changes(db: Session, wells: list[models.Well],
                 window_h: int) -> list[dict]:
    """Изменение дебита каждой скважины за окно + статус связи."""
    from . import services
    anchor = services.link_anchor(db)
    t_anchor = data_anchor(db)
    series = _measurements_by_well(db, [w.id for w in wells], t_anchor)
    rows = []
    for w in wells:
        q_now, q_prev, stale = _pair(series.get(w.id, []), window_h, t_anchor)
        delta = pct = None
        if q_now is not None and q_prev is not None:
            delta = round(q_now - q_prev, 1)
            pct = round(delta / q_prev * 100, 1) if q_prev > 0 else None
        rows.append({
            "id": w.id, "number": w.number, "gzu": w.gzu.name,
            "well_type": w.well_type, "meter_type": w.meter_type,
            "work_status": w.work_status,
            "online": services.link_online(w, anchor),
            "q_now": round(q_now, 1) if q_now is not None else None,
            "q_prev": round(q_prev, 1) if q_prev is not None else None,
            "delta": delta, "pct": pct, "stale": stale,
        })
    return rows


def gzu_changes(db: Session, window_h: int) -> list[dict]:
    """Изменение суммарного дебита каждого ГЗУ за окно.

    Суммируются только скважины, у которых есть ПАРА замеров (сейчас/было) —
    иначе выбытие данных выглядело бы как падение добычи.
    """
    wells = db.query(models.Well).all()
    rows = well_changes(db, wells, window_h)
    gzus = {g.id: g for g in db.query(models.Gzu).all()}
    by_gzu: dict[str, dict] = {}
    gzu_of = {w.id: w.gzu_id for w in wells}
    for r in rows:
        gid = gzu_of[r["id"]]
        g = by_gzu.setdefault(gid, {
            "id": gid, "name": gzus[gid].name, "cdn": gzus[gid].cdn.name,
            "wells": 0, "paired": 0, "offline": 0, "stopped": 0,
            "q_now": 0.0, "q_prev": 0.0, "falling": 0,
        })
        g["wells"] += 1
        if not r["online"]:
            g["offline"] += 1
        if r["work_status"] == "Stop":
            g["stopped"] += 1
        if r["delta"] is not None:
            g["paired"] += 1
            g["q_now"] += r["q_now"]
            g["q_prev"] += r["q_prev"]
            if r["delta"] < 0:
                g["falling"] += 1
    out = []
    for g in by_gzu.values():
        if g["paired"]:
            g["delta"] = round(g["q_now"] - g["q_prev"], 1)
            g["pct"] = (round(g["delta"] / g["q_prev"] * 100, 1)
                        if g["q_prev"] > 0 else None)
        else:
            g["delta"] = g["pct"] = None
        g["q_now"] = round(g["q_now"], 1)
        g["q_prev"] = round(g["q_prev"], 1)
        out.append(g)
    # худшие сверху: сначала падение, затем без данных, затем рост
    out.sort(key=lambda g: (0, g["delta"]) if g["delta"] is not None and g["delta"] < 0
             else (1, 0) if g["delta"] is None else (2, g["delta"]))
    return out


def well_forecast(db: Session, well: models.Well, window_h: int) -> dict:
    """Ряд замеров скважины + линейный тренд и прогноз на следующее окно.

    Тренд — МНК по замерам за последние FIT_WINDOWS окон (не меньше 3 точек).
    Прогноз берётся с линии тренда, а не от последней точки, чтобы шум одного
    замера не переносился вперёд. Если наклон статистически не отличим от
    нуля (|b| ≤ t₀.₉₅·SE), тренд помечается незначимым и прогноз
    равен текущему уровню линии.
    """
    t_anchor = data_anchor(db)
    series = list(reversed(
        _measurements_by_well(db, [well.id], t_anchor).get(well.id, [])))
    points = [{"t": t.strftime("%d.%m %H:%M"), "ts": t.isoformat(), "q": round(q, 2)}
              for t, q in series]

    out = {"points": points, "fit_points": 0, "slope_per_h": None,
           "level": None, "projected": None, "significant": False,
           "window_h": window_h}
    if not series:
        return out
    t_last = series[-1][0]
    fit = [(t, q) for t, q in series
           if t >= t_last - dt.timedelta(hours=window_h * FIT_WINDOWS)]
    out["fit_points"] = len(fit)
    if len(fit) < 3:
        return out

    xs = [(t - t_last).total_seconds() / 3600 for t, _ in fit]  # ≤ 0
    ys = [q for _, q in fit]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return out
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx                      # уровень линии в момент t_last
    resid = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    se = (resid / (n - 2) / sxx) ** 0.5
    significant = b != 0 and abs(b) > _t95(n - 2) * se
    level = max(0.0, a)
    projected = max(0.0, a + b * window_h) if significant else level

    out.update({
        "slope_per_h": round(b, 3),
        "level": round(level, 1),
        "projected": round(projected, 1),
        "significant": significant,
    })
    return out
