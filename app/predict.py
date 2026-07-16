"""Предиктивная аналитика: динамика добычи и прогноз (страница /predict).

Три уровня детализации:
  • флот → изменение дебита каждого ГЗУ за окно (4 ч или 24 ч);
  • ГЗУ → изменение по каждой скважине группы;
  • скважина → ряд замеров, линейный тренд и прогноз на следующее окно.

Сравнение честное по данным: «сейчас» — последний замер, «было» — последний
замер не позднее (сейчас − окно). Скважины без пары замеров помечаются
«нет данных», а не нулём.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from sqlalchemy.orm import Session

from . import models

# насколько глубоко смотрим назад за парным замером (макс. окно × запас)
LOOKBACK_HOURS = 72


def _measurements_by_well(db: Session, well_ids: list[int]) -> dict[int, list]:
    """Все замеры скважин за LOOKBACK_HOURS, по скважине, свежие первыми."""
    if not well_ids:
        return {}
    since = dt.datetime.utcnow() - dt.timedelta(hours=LOOKBACK_HOURS)
    rows = (db.query(models.Measurement.well_id,
                     models.Measurement.measured_at,
                     models.Measurement.flow_rate)
            .filter(models.Measurement.well_id.in_(well_ids),
                    models.Measurement.measured_at >= since)
            .order_by(models.Measurement.measured_at.desc())
            .all())
    out: dict[int, list] = defaultdict(list)
    for wid, t, q in rows:
        out[wid].append((t, q))
    return out


def _pair(series: list, window_h: int):
    """(q_now, q_prev) по ряду замеров: последний и последний до t-окно."""
    if not series:
        return None, None
    t1, q1 = series[0]
    cutoff = t1 - dt.timedelta(hours=window_h)
    for t, q in series[1:]:
        if t <= cutoff:
            return q1, q
    return q1, None


def well_changes(db: Session, wells: list[models.Well],
                 window_h: int) -> list[dict]:
    """Изменение дебита каждой скважины за окно + статус связи."""
    from . import services
    anchor = services.link_anchor(db)
    series = _measurements_by_well(db, [w.id for w in wells])
    rows = []
    for w in wells:
        q_now, q_prev = _pair(series.get(w.id, []), window_h)
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
            "delta": delta, "pct": pct,
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
    out.sort(key=lambda g: g["delta"] if g["delta"] is not None else 0)
    return out


def well_forecast(db: Session, well: models.Well, window_h: int) -> dict:
    """Ряд замеров скважины + линейный тренд и прогноз на следующее окно."""
    series = list(reversed(_measurements_by_well(db, [well.id]).get(well.id, [])))
    points = [{"t": t.strftime("%d.%m %H:%M"), "ts": t.isoformat(), "q": round(q, 2)}
              for t, q in series]

    slope_per_h = None
    projected = None
    if len(series) >= 2:
        # линейная регрессия q(t) по часам от первого замера
        t0 = series[0][0]
        xs = [(t - t0).total_seconds() / 3600 for t, _ in series]
        ys = [q for _, q in series]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        if den > 0:
            slope_per_h = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
            last_x, last_q = xs[-1], ys[-1]
            projected = max(0.0, last_q + slope_per_h * window_h)

    return {
        "points": points,
        "slope_per_h": round(slope_per_h, 3) if slope_per_h is not None else None,
        "projected": round(projected, 1) if projected is not None else None,
        "window_h": window_h,
    }
