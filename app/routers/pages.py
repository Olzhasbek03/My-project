"""Пользовательский веб-интерфейс (п. 2.1.3.3 ТЗ)."""
import datetime as dt

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from .. import advisor, config, models, predict, radio, reports, services
from ..database import get_db
from ..templating import render

router = APIRouter()


@router.get("/lang/{lang}")
def set_language(lang: str, request: Request):
    resp = RedirectResponse(request.headers.get("referer", "/home"), status_code=303)
    if lang in config.LANGUAGES:
        resp.set_cookie("site_lang", lang, max_age=365 * 24 * 3600)
    return resp


# ---------- дашборд ----------

@router.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/home", status_code=307)


@router.get("/home", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    wells = db.query(models.Well).all()
    anchor = services.link_anchor(db)
    last = services.latest_measurements(db, [w.id for w in wells])

    online = stopped = 0
    q_total = 0.0
    for w in wells:
        if services.link_online(w, anchor):
            online += 1
            m = last.get(w.id)
            if w.work_status == "Stop" or (m is not None and m.flow_rate <= 0):
                stopped += 1
            if m is not None and m.flow_rate > 0:
                q_total += m.flow_rate
    kpi = {
        "total": len(wells),
        "online": online,
        "offline": len(wells) - online,
        "stopped": stopped,
        "shgn": sum(1 for w in wells if w.well_type == "ШГН"),
        "vn": sum(1 for w in wells if w.well_type == "ВН"),
        "q_total": round(q_total),
    }
    # значительное снижение дебита за 4 часа: сравнение Qж(факт) и Qж(4 часа)
    prev_flow = services.previous_flow(db, [w.id for w in wells])
    drops = []
    for w in wells:
        m = last.get(w.id)
        prev = prev_flow.get(w.id)
        if m is None or not prev or prev <= 0:
            continue
        if w.work_status == "Stop":  # уже учтена как остановленная
            continue
        pct = round((prev - m.flow_rate) / prev * 100, 1)
        if pct >= config.RATE_DROP_THRESHOLD_PCT:
            drops.append({"well": w, "drop": pct, "rate": m.flow_rate})
    drops.sort(key=lambda d: -d["drop"])
    return render(request, "dashboard.html",
                  {"kpi": kpi, "drops": drops[:12]})


# ---------- фонд скважин ----------

def _row_status(r: dict) -> str:
    """Статус скважины из уже собранной строки таблицы (без доп. запросов):
    оффлайн → нет связи; остановлена → Stop или нулевой дебит; иначе в работе."""
    if not r["online"]:
        return "offline"
    m = r["last"]
    if r["well"].work_status == "Stop" or (m is not None and m.flow_rate <= 0):
        return "stopped"
    return "in_operation"


def _gzu_dashboard(db: Session, rows: list[dict], gzu_id: int) -> dict:
    """Аналитика по выбранному ГЗУ из строк текущей выборки.

    Строится из тех же строк, что и таблица ниже, поэтому фильтры страницы
    одинаково действуют на дашборды и на таблицу — цифры всегда сходятся.
    """
    gzu_obj = db.get(models.Gzu, gzu_id)
    statuses = [_row_status(r) for r in rows]
    q_total = sum(r["last"].flow_rate for r in rows
                  if r["last"] and r["last"].flow_rate > 0)

    top = sorted((r for r in rows if r["last"] and r["last"].flow_rate > 0),
                 key=lambda r: -r["last"].flow_rate)[:10]

    # Гистограмма распределения дебита по действующим скважинам
    bins = [(0, 5), (5, 10), (10, 20), (20, 40), (40, 80), (80, None)]
    bin_counts = [0] * len(bins)
    for r in rows:
        if r["last"] is None or r["last"].flow_rate <= 0:
            continue
        v = r["last"].flow_rate
        for i, (lo, hi) in enumerate(bins):
            if v >= lo and (hi is None or v < hi):
                bin_counts[i] += 1
                break

    # Суммарный дебит ГЗУ по 4-часовым интервалам суток последнего замера
    well_ids = [r["well"].id for r in rows]
    fourh = []
    last_times = [r["last"].measured_at for r in rows if r["last"]]
    if last_times and well_ids:
        ref = max(last_times)
        start = ref.replace(hour=0, minute=0, second=0, microsecond=0)
        sums = [dict() for _ in range(6)]           # bucket -> {well_id: [flows]}
        ms = (db.query(models.Measurement.well_id,
                       models.Measurement.measured_at,
                       models.Measurement.flow_rate)
              .filter(models.Measurement.well_id.in_(well_ids),
                      models.Measurement.measured_at >= start,
                      models.Measurement.measured_at < start + dt.timedelta(hours=24))
              .all())
        for wid, t, flow in ms:
            b = int((t - start).total_seconds() // 14400)
            if 0 <= b < 6 and flow > 0:
                sums[b].setdefault(wid, []).append(flow)
        for b in range(6):
            total = sum(sum(v) / len(v) for v in sums[b].values())
            t0 = start + dt.timedelta(hours=4 * b)
            fourh.append({"label": f"{t0:%H:%M}", "value": round(total, 1)})

    # Значительное снижение дебита внутри ГЗУ
    drops = []
    for r in rows:
        m, prev = r["last"], r["q_4h"]
        if m is None or not prev or prev <= 0 or r["well"].work_status == "Stop":
            continue
        pct = round((prev - m.flow_rate) / prev * 100, 1)
        if pct >= config.RATE_DROP_THRESHOLD_PCT:
            drops.append({"number": r["well"].number, "id": r["well"].id,
                          "rate": round(m.flow_rate, 1), "drop": pct})
    drops.sort(key=lambda d: -d["drop"])

    return {
        "name": gzu_obj.name if gzu_obj else "",
        "cdn": gzu_obj.cdn.name if gzu_obj else "",
        "kpi": {
            "total": len(rows),
            "online": statuses.count("in_operation") + statuses.count("stopped"),
            "in_operation": statuses.count("in_operation"),
            "stopped": statuses.count("stopped"),
            "offline": statuses.count("offline"),
            "q_total": round(q_total),
            "shgn": sum(1 for r in rows if r["well"].well_type == "ШГН"),
            "vn": sum(1 for r in rows if r["well"].well_type == "ВН"),
        },
        "top": [{"number": r["well"].number, "id": r["well"].id,
                 "q": round(r["last"].flow_rate, 1)} for r in top],
        "hist": [{"label": f"{lo}–{hi}" if hi else f"{lo}+", "value": n}
                 for (lo, hi), n in zip(bins, bin_counts)],
        "fourh": fourh,
        "drops": drops[:5],
    }


SORT_COLUMNS = {
    "number": lambda r: r["well"].number,
    "gzu": lambda r: r["well"].gzu.name,
    "link": lambda r: r["online"],
    "work_status": lambda r: r["well"].work_status,
    "well_type": lambda r: r["well"].well_type,
    "q_fact": lambda r: r["last"].flow_rate if r["last"] else -1,
    "q_4h": lambda r: r["q_4h"] if r["q_4h"] is not None else -1,
    "rlin": lambda r: (r["last"].pressure if r["last"] and r["last"].pressure is not None else -1),
    "tlin": lambda r: (r["last"].temperature if r["last"] and r["last"].temperature is not None else -1),
}

PAGE_SIZE = 100


@router.get("/wells", response_class=HTMLResponse)
def wells_list(request: Request, q: str = "", sort: str = "number",
               dir: str = "asc", page: int = 1,
               gzu: int = 0, cdn: int = 0, wtype: str = "",
               link: str = "", wstatus: str = "",
               db: Session = Depends(get_db)):
    anchor = services.link_anchor(db)
    wells_q = db.query(models.Well).join(models.Gzu)
    if q.strip():
        needle = f"%{q.strip()}%"
        wells_q = wells_q.filter(models.Well.number.ilike(needle) |
                                 models.Gzu.name.ilike(needle))
    if gzu:
        wells_q = wells_q.filter(models.Well.gzu_id == gzu)
    if cdn:
        wells_q = wells_q.filter(models.Gzu.cdn_id == cdn)
    if wtype in models.WELL_TYPES:
        wells_q = wells_q.filter(models.Well.well_type == wtype)
    if wstatus == "running":
        wells_q = wells_q.filter(models.Well.work_status == "Running")
    elif wstatus == "stop":
        wells_q = wells_q.filter(models.Well.work_status == "Stop")
    elif wstatus == "none":
        wells_q = wells_q.filter(models.Well.work_status == "")
    wells = wells_q.all()
    if link == "on":
        wells = [w for w in wells if services.link_online(w, anchor)]
    elif link == "off":
        wells = [w for w in wells if not services.link_online(w, anchor)]
    last = services.latest_measurements(db, [w.id for w in wells])
    comments = dict(
        db.query(models.WellComment.well_id, models.WellComment.text)
        .filter(models.WellComment.well_id.in_([w.id for w in wells] or [-1]))
        .order_by(models.WellComment.created_at).all())

    prev_flow = services.previous_flow(db, [w.id for w in wells])
    rows = []
    for w in wells:
        m = last.get(w.id)
        q4 = prev_flow.get(w.id) if m is not None else None
        rows.append({
            "well": w, "last": m, "q_4h": q4,
            "online": services.link_online(w, anchor),
            "comment": comments.get(w.id, ""),
        })
    key = SORT_COLUMNS.get(sort, SORT_COLUMNS["number"])
    rows.sort(key=key, reverse=(dir == "desc"))

    total = len(rows)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    online_count = sum(1 for r in rows if r["online"])
    from urllib.parse import urlencode
    filters_qs = urlencode({"q": q, "gzu": gzu, "cdn": cdn, "wtype": wtype,
                            "link": link, "wstatus": wstatus})
    # без выбранного ГЗУ строка запроса для карточек-ссылок (gzu подставляется)
    picker_qs = urlencode({"q": q, "cdn": cdn, "wtype": wtype,
                           "link": link, "wstatus": wstatus})

    # Аналитика: дашборды выбранного ГЗУ либо карточки выбора ГЗУ
    dash = _gzu_dashboard(db, rows, gzu) if gzu else None
    gzu_cards = []
    if not gzu:
        groups: dict[int, dict] = {}
        for r in rows:
            g = r["well"].gzu
            card = groups.setdefault(g.id, {"id": g.id, "name": g.name,
                                            "total": 0, "online": 0, "q": 0.0})
            card["total"] += 1
            card["online"] += 1 if r["online"] else 0
            if r["last"] and r["last"].flow_rate > 0:
                card["q"] += r["last"].flow_rate
        gzu_cards = sorted(groups.values(), key=lambda c: c["name"])
        for c in gzu_cards:
            c["q"] = round(c["q"])

    return render(request, "wells.html", {
        "rows": rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE],
        "total": total, "online_count": online_count,
        "page": page, "pages": pages, "q": q, "sort": sort, "dir": dir,
        "gzu": gzu, "cdn": cdn, "wtype": wtype, "link": link, "wstatus": wstatus,
        "filters_qs": filters_qs, "picker_qs": picker_qs,
        "dash": dash, "gzu_cards": gzu_cards,
        "gzus": db.query(models.Gzu).order_by(models.Gzu.name).all(),
        "cdns": db.query(models.Cdn).order_by(models.Cdn.name).all(),
    })


@router.get("/wells/export.xlsx")
def wells_export(db: Session = Depends(get_db)):
    data = reports.wells_excel_export(db)
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=wells.xlsx"})


@router.get("/wells/{well_id}", response_class=HTMLResponse)
def well_detail(well_id: int, request: Request, db: Session = Depends(get_db)):
    well = db.query(models.Well).filter(models.Well.id == well_id).first()
    if well is None:
        return RedirectResponse("/wells", status_code=303)
    m = services.last_measurement(db, well.id)
    # опорная точка — время последнего замера, чтобы интервалы и суточный
    # дебит отражали фактический период данных, а не пустое «сегодня»
    ref = m.measured_at if m else services.utcnow()
    prev_start = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    prev_end = ref
    intervals = services.four_hour_intervals(prev_start)
    four_hours = list(zip(intervals, services.four_hour_rates(db, well.id, prev_start)))
    history = (db.query(models.Measurement)
               .filter(models.Measurement.well_id == well.id)
               .order_by(models.Measurement.measured_at.desc())
               .limit(200).all())
    comments = (db.query(models.WellComment)
                .filter(models.WellComment.well_id == well.id)
                .order_by(models.WellComment.created_at.desc()).all())
    return render(request, "well_detail.html", {
        "well": well, "last": m,
        "status": services.well_status(db, well),
        "prev_day": services.volume_for_period(
            db, well.id, ref - dt.timedelta(hours=24), ref + dt.timedelta(minutes=1)),
        "four_hours": four_hours, "history": history, "comments": comments,
    })


@router.post("/wells/{well_id}/comments")
def add_comment(well_id: int, text: str = Form(...), db: Session = Depends(get_db)):
    well = db.query(models.Well).filter(models.Well.id == well_id).first()
    if well is not None and text.strip():
        db.add(models.WellComment(well_id=well.id, author="Anonymous",
                                  text=text.strip()))
        db.commit()
    return RedirectResponse(f"/wells/{well_id}", status_code=303)


# ---------- предиктивный анализ ----------

@router.get("/predict", response_class=HTMLResponse)
def predict_page(request: Request, window: int = 4, gzu: int = 0, well: int = 0,
                 db: Session = Depends(get_db)):
    """Динамика добычи за 4/24 ч + ИИ-советник.

    Уровни: флот (все ГЗУ) → ГЗУ (его скважины) → скважина (тренд и прогноз).
    """
    window = 24 if window == 24 else 4
    ctx: dict = {"window": window, "ai_enabled": advisor.ai_enabled(),
                 "level": "fleet", "gzu_id": gzu, "well_id": well}

    if well:
        w = db.get(models.Well, well)
        if w is None:
            return RedirectResponse(f"/predict?window={window}", status_code=303)
        row = predict.well_changes(db, [w], window)[0]
        forecast = predict.well_forecast(db, w, window)
        recs = advisor.advise_well(row, forecast, window)
        ctx.update({
            "level": "well", "well": w, "row": row, "forecast": forecast,
            "recs": recs, "gzu_id": w.gzu_id, "gzu_name": w.gzu.name,
            "ai_text": advisor.ai_narrative(
                f"well:{w.id}:{window}",
                {"скважина": row, "прогноз": forecast, "правила": recs}),
        })
    elif gzu:
        g = db.get(models.Gzu, gzu)
        if g is None:
            return RedirectResponse(f"/predict?window={window}", status_code=303)
        rows = predict.well_changes(db, list(g.wells), window)
        rows.sort(key=lambda r: (r["delta"] is None, r["delta"] or 0))
        recs = advisor.advise_gzu(g.name, rows, window)
        ctx.update({
            "level": "gzu", "gzu_name": g.name, "cdn_name": g.cdn.name,
            "rows": rows, "recs": recs,
            "max_abs": max((abs(r["delta"]) for r in rows
                            if r["delta"] is not None), default=1) or 1,
            "ai_text": advisor.ai_narrative(
                f"gzu:{g.id}:{window}",
                {"ГЗУ": g.name, "скважины": rows[:40], "правила": recs}),
        })
    else:
        rows = predict.gzu_changes(db, window)
        recs = advisor.advise_fleet(rows, window)
        ctx.update({
            "rows": rows, "recs": recs,
            "max_abs": max((abs(r["delta"]) for r in rows
                            if r["delta"] is not None), default=1) or 1,
            "ai_text": advisor.ai_narrative(
                f"fleet:{window}", {"ГЗУ": rows, "правила": recs}),
        })
    return render(request, "predict.html", ctx)


# ---------- карта ----------

@router.get("/map", response_class=HTMLResponse)
def wells_map(request: Request, db: Session = Depends(get_db)):
    wells = db.query(models.Well).all()
    statuses = services.bulk_statuses(db, wells)
    points = [{"number": w.number, "id": w.id,
               "lat": w.latitude, "lon": w.longitude,
               "status": statuses[w.id]} for w in wells
              if w.latitude and w.longitude]
    gateways = [{"name": g.name, "lat": g.latitude, "lon": g.longitude,
                 "online": g.is_online}
                for g in db.query(models.Gateway).all()]
    return render(request, "map.html",
                  {"points": points, "gateways": gateways})


# ---------- радиоанализ (отдельный интерфейс, kk/en/ru — п. 2.1.3.4) ----------

@router.get("/radio", response_class=HTMLResponse)
def radio_analysis(request: Request, db: Session = Depends(get_db)):
    return render(request, "radio.html", {
        "gw_stats": radio.gateway_signal_stats(db),
        "worst": radio.worst_gateways(db),
        "wrong_gw": radio.terminals_on_wrong_gateway(db),
        "weak_zones": radio.weak_signal_zones(db),
        "empty": radio.empty_packets(db),
        "distances": radio.terminal_distances(db),
    }, radio_mode=True)


# ---------- отчёты ----------

@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    return render(request, "reports.html", {})


@router.get("/reports/4h.xlsx")
def report_4h(db: Session = Depends(get_db)):
    stamp = dt.datetime.utcnow().strftime("%Y-%m-%d_%H%M")
    return Response(
        reports.wells_report(db, None, 4),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=report_4h_{stamp}.xlsx"})


@router.get("/reports/daily.xlsx")
def report_daily(db: Session = Depends(get_db)):
    stamp = dt.datetime.utcnow().strftime("%Y-%m-%d")
    return Response(
        reports.wells_report(db, None, 24),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=report_daily_{stamp}.xlsx"})


@router.get("/reports/validation.xlsx")
def report_validation(db: Session = Depends(get_db)):
    stamp = dt.datetime.utcnow().strftime("%Y-%m-%d")
    return Response(
        reports.validation_report(db),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f"attachment; filename=report_validation_{stamp}.xlsx"})
