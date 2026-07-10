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
        resp.set_cookie("optiwell_lang", lang, max_age=365 * 24 * 3600)
    return resp


# ---------- дашборд ----------

@router.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/home", status_code=307)


@router.get("/home", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db),
              user: models.User = Depends(get_current_user)):
    wells = visible_wells_query(db, user).all()
    statuses = {w.id: services.well_status(db, w) for w in wells}
    prev_start, prev_end = services.previous_day_bounds()
    total_prev_day = sum(
        services.volume_for_period(db, w.id, prev_start, prev_end)
        for w in wells if w.fund == models.FUND_PRODUCING)
    kpi = {
        "total": len(wells),
        "producing": sum(1 for w in wells if w.fund == models.FUND_PRODUCING),
        "injection": sum(1 for w in wells if w.fund == models.FUND_INJECTION),
        "in_operation": sum(1 for s in statuses.values() if s == "in_operation"),
        "stopped": sum(1 for s in statuses.values() if s == "stopped"),
        "offline": sum(1 for s in statuses.values() if s == "offline"),
        "prev_day_total": round(total_prev_day, 1),
    }
    drops = []
    for w in wells:
        pct = services.rate_drop_pct(db, w.id)
        if pct >= config.RATE_DROP_THRESHOLD_PCT:
            drops.append({"well": w, "drop": pct})
    drops.sort(key=lambda d: -d["drop"])
    return render(request, "dashboard.html",
                  {"user": user, "kpi": kpi, "drops": drops[:15]})


# ---------- фонд скважин ----------

@router.get("/wells", response_class=HTMLResponse)
def wells_list(request: Request, db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    prev_start, prev_end = services.previous_day_bounds()
    rows = []
    for well in visible_wells_query(db, user).order_by(models.Well.number).all():
        m = services.last_measurement(db, well.id)
        rows.append({
            "well": well,
            "status": services.well_status(db, well),
            "last": m,
            "prev_day": services.volume_for_period(db, well.id, prev_start, prev_end),
        })
    return render(request, "wells.html", {"user": user, "rows": rows})


@router.get("/wells/export.xlsx")
def wells_export(db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    data = reports.wells_excel_export(db, user)
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=optiwell_wells.xlsx"})


@router.get("/wells/{well_id}", response_class=HTMLResponse)
def well_detail(well_id: int, request: Request, db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    well = visible_wells_query(db, user).filter(models.Well.id == well_id).first()
    if well is None:
        return RedirectResponse("/wells", status_code=303)
    m = services.last_measurement(db, well.id)
    prev_start, prev_end = services.previous_day_bounds()
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
        "prev_day": services.volume_for_period(db, well.id, prev_start, prev_end),
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
    points = [{"number": w.number, "id": w.id,
               "lat": w.latitude, "lon": w.longitude,
               "status": services.well_status(db, w)} for w in wells]
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
        headers={"Content-Disposition": f"attachment; filename=optiwell_4h_{stamp}.xlsx"})


@router.get("/reports/daily.xlsx")
def report_daily(db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    stamp = dt.datetime.utcnow().strftime("%Y-%m-%d")
    return Response(
        reports.wells_report(db, user, 24),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=optiwell_daily_{stamp}.xlsx"})


@router.get("/reports/validation.xlsx")
def report_validation(db: Session = Depends(get_db),
                      user: models.User = Depends(auth.require_admin)):
    stamp = dt.datetime.utcnow().strftime("%Y-%m-%d")
    return Response(
        reports.validation_report(db),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f"attachment; filename=optiwell_validation_{stamp}.xlsx"})
