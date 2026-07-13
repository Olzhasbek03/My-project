"""Пользовательский веб-интерфейс (п. 2.1.3.3 ТЗ)."""
import datetime as dt

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from .. import auth, config, models, radio, reports, services
from ..auth import get_current_user, visible_wells_query
from ..database import get_db
from ..templating import render

router = APIRouter()


# ---------- вход / выход ----------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", {"error": False})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_db)):
    user = (db.query(models.User)
            .filter(models.User.username == username,
                    models.User.is_active.is_(True))
            .first())
    if user is None or not auth.verify_password(password, user.password_hash):
        return render(request, "login.html", {"error": True}, status_code=401)
    resp = RedirectResponse("/home", status_code=303)
    resp.set_cookie(auth.SESSION_COOKIE, auth.make_session_token(user.id),
                    httponly=True, samesite="lax")
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


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
def dashboard(request: Request, db: Session = Depends(get_db),
              user: models.User = Depends(get_current_user)):
    wells = visible_wells_query(db, user).all()
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
                  {"user": user, "kpi": kpi, "drops": drops[:12]})


# ---------- фонд скважин ----------

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
               db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    anchor = services.link_anchor(db)
    wells_q = visible_wells_query(db, user)
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
    return render(request, "wells.html", {
        "user": user,
        "rows": rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE],
        "total": total, "online_count": online_count,
        "page": page, "pages": pages, "q": q, "sort": sort, "dir": dir,
        "gzu": gzu, "cdn": cdn, "wtype": wtype, "link": link, "wstatus": wstatus,
        "filters_qs": filters_qs,
        "gzus": db.query(models.Gzu).order_by(models.Gzu.name).all(),
        "cdns": db.query(models.Cdn).order_by(models.Cdn.name).all(),
    })


@router.get("/wells/export.xlsx")
def wells_export(db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    data = reports.wells_excel_export(db, user)
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=wells.xlsx"})


@router.get("/wells/{well_id}", response_class=HTMLResponse)
def well_detail(well_id: int, request: Request, db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    well = visible_wells_query(db, user).filter(models.Well.id == well_id).first()
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
        "user": user, "well": well, "last": m,
        "status": services.well_status(db, well),
        "prev_day": services.volume_for_period(
            db, well.id, ref - dt.timedelta(hours=24), ref + dt.timedelta(minutes=1)),
        "four_hours": four_hours, "history": history, "comments": comments,
    })


@router.post("/wells/{well_id}/comments")
def add_comment(well_id: int, text: str = Form(...), db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    well = visible_wells_query(db, user).filter(models.Well.id == well_id).first()
    if well is not None and text.strip():
        db.add(models.WellComment(well_id=well.id, author=user.full_name or user.username,
                                  text=text.strip()))
        db.commit()
    return RedirectResponse(f"/wells/{well_id}", status_code=303)


# ---------- карта ----------

@router.get("/map", response_class=HTMLResponse)
def wells_map(request: Request, db: Session = Depends(get_db),
              user: models.User = Depends(get_current_user)):
    wells = visible_wells_query(db, user).all()
    statuses = services.bulk_statuses(db, wells)
    points = [{"number": w.number, "id": w.id,
               "lat": w.latitude, "lon": w.longitude,
               "status": statuses[w.id]} for w in wells
              if w.latitude and w.longitude]
    gateways = [{"name": g.name, "lat": g.latitude, "lon": g.longitude,
                 "online": g.is_online}
                for g in db.query(models.Gateway).all()]
    return render(request, "map.html",
                  {"user": user, "points": points, "gateways": gateways})


# ---------- радиоанализ (отдельный интерфейс, kk/en/ru — п. 2.1.3.4) ----------

@router.get("/radio", response_class=HTMLResponse)
def radio_analysis(request: Request, db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    return render(request, "radio.html", {
        "user": user,
        "gw_stats": radio.gateway_signal_stats(db),
        "worst": radio.worst_gateways(db),
        "wrong_gw": radio.terminals_on_wrong_gateway(db),
        "weak_zones": radio.weak_signal_zones(db),
        "empty": radio.empty_packets(db),
        "distances": radio.terminal_distances(db),
    }, radio_mode=True)


# ---------- отчёты ----------

@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, user: models.User = Depends(get_current_user)):
    return render(request, "reports.html", {"user": user})


@router.get("/reports/4h.xlsx")
def report_4h(db: Session = Depends(get_db),
              user: models.User = Depends(get_current_user)):
    stamp = dt.datetime.utcnow().strftime("%Y-%m-%d_%H%M")
    return Response(
        reports.wells_report(db, user, 4),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=report_4h_{stamp}.xlsx"})


@router.get("/reports/daily.xlsx")
def report_daily(db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    stamp = dt.datetime.utcnow().strftime("%Y-%m-%d")
    return Response(
        reports.wells_report(db, user, 24),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=report_daily_{stamp}.xlsx"})


@router.get("/reports/validation.xlsx")
def report_validation(db: Session = Depends(get_db),
                      user: models.User = Depends(auth.require_admin)):
    stamp = dt.datetime.utcnow().strftime("%Y-%m-%d")
    return Response(
        reports.validation_report(db),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f"attachment; filename=report_validation_{stamp}.xlsx"})
