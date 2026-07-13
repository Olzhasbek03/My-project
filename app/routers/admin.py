"""Административные вкладки платформы (п. 2.1.3.3 ТЗ) — все операции
выполняются через веб-интерфейс без прямого доступа к СУБД:

- управление пользователями и разграничение прав доступа (видимость
  скважин/ГЗУ/ЦДН);
- управление фондом скважин и оборудованием: добавление скважин, перевод
  между фондами (добывающий/ППД/бездействие), список устройств, изменение
  типа расходомера;
- настройка сбора и обработки данных (период передачи, регистры терминалов);
- настройка расписания отправки суточных отчётов.
"""
import os
import tempfile

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import auth, models, services
from ..database import get_db
from ..templating import render

router = APIRouter(prefix="/admin", dependencies=[Depends(auth.require_admin)])


ADMIN_LIST_LIMIT = 50


@router.get("", response_class=HTMLResponse)
def admin_home(request: Request, wq: str = "", db: Session = Depends(get_db),
               user: models.User = Depends(auth.require_admin)):
    wells_q = db.query(models.Well).order_by(models.Well.number)
    devices_q = (db.query(models.Device).join(models.Well)
                 .order_by(models.Well.number))
    if wq.strip():
        needle = f"%{wq.strip()}%"
        wells_q = wells_q.filter(models.Well.number.ilike(needle))
        devices_q = devices_q.filter(models.Well.number.ilike(needle))
    return render(request, "admin.html", {
        "user": user, "wq": wq, "limit": ADMIN_LIST_LIMIT,
        "users": db.query(models.User).order_by(models.User.username).all(),
        "wells": wells_q.limit(ADMIN_LIST_LIMIT).all(),
        "devices": devices_q.limit(ADMIN_LIST_LIMIT).all(),
        "gzus": db.query(models.Gzu).order_by(models.Gzu.name).all(),
        "cdns": db.query(models.Cdn).order_by(models.Cdn.name).all(),
        "meter_types": models.METER_TYPES,
        "daily_report_time": services.get_setting(db, "daily_report_time", "06:00"),
        "validation_report_time": services.get_setting(db, "validation_report_time", "07:00"),
        "scada_modbus": services.get_setting(db, "scada_modbus_endpoint", ""),
        "scada_opcua": services.get_setting(db, "scada_opcua_endpoint", ""),
    })


# ---------- пользователи и доступ ----------

@router.post("/users")
def create_user(username: str = Form(...), full_name: str = Form(""),
                email: str = Form(""), password: str = Form(...),
                role: str = Form(models.ROLE_GZU),
                scope_gzu: list[int] = Form([]), scope_cdn: list[int] = Form([]),
                receive_reports: bool = Form(False),
                db: Session = Depends(get_db)):
    if db.query(models.User).filter(models.User.username == username).first():
        return RedirectResponse("/admin?error=user_exists", status_code=303)
    user = models.User(username=username, full_name=full_name, email=email,
                       password_hash=auth.hash_password(password), role=role,
                       receive_reports=receive_reports)
    db.add(user)
    db.flush()
    for gid in scope_gzu:
        db.add(models.UserScope(user_id=user.id, level="gzu", object_id=gid))
    for cid in scope_cdn:
        db.add(models.UserScope(user_id=user.id, level="cdn", object_id=cid))
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/users/{user_id}/toggle")
def toggle_user(user_id: int, db: Session = Depends(get_db)):
    user = db.get(models.User, user_id)
    if user:
        user.is_active = not user.is_active
        db.commit()
    return RedirectResponse("/admin", status_code=303)


# ---------- фонд скважин и оборудование ----------

@router.post("/wells")
def create_well(number: str = Form(...), gzu_id: int = Form(...),
                fund: str = Form(models.FUND_PRODUCING),
                meter_type: str = Form(models.METER_TYPES[0]),
                latitude: float = Form(0.0), longitude: float = Form(0.0),
                dev_eui: str = Form(""),
                db: Session = Depends(get_db)):
    if db.query(models.Well).filter(models.Well.number == number).first():
        return RedirectResponse("/admin?error=well_exists", status_code=303)
    well = models.Well(number=number, gzu_id=gzu_id, fund=fund,
                       meter_type=meter_type, latitude=latitude, longitude=longitude)
    db.add(well)
    db.flush()
    if dev_eui:
        db.add(models.Device(dev_eui=dev_eui, well_id=well.id))
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/wells/{well_id}")
def update_well(well_id: int, fund: str = Form(...), meter_type: str = Form(...),
                well_type: str = Form("ШГН"),
                water_cut_pct: float = Form(0.0), is_active: bool = Form(False),
                db: Session = Depends(get_db)):
    """Перевод скважины между фондами (добывающий → ППД → БД), изменение типа
    расходомера, обводнённости и активация опроса (пп. 2.1.3.3, 2.1.3.7 ТЗ)."""
    well = db.get(models.Well, well_id)
    if well:
        well.fund = fund
        well.meter_type = meter_type
        well.well_type = well_type
        well.water_cut_pct = water_cut_pct
        well.is_active = is_active
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/devices/{device_id}")
def update_device(device_id: int, report_interval_sec: int = Form(...),
                  modbus_registers: str = Form(""),
                  db: Session = Depends(get_db)):
    """Удалённая настройка терминала без выезда на скважину (п. 2.1.1 ТЗ):
    периодичность передачи (1 с … 10 ч) и считываемые регистры. Команда
    ставится в очередь downlink LoRaWAN-сервера."""
    dev = db.get(models.Device, device_id)
    if dev:
        dev.report_interval_sec = max(1, min(report_interval_sec, 36000))
        dev.modbus_registers = modbus_registers
        db.commit()
    return RedirectResponse("/admin", status_code=303)


# ---------- загрузка CSV-экспорта фонда скважин ----------

@router.post("/import")
async def import_export(file: UploadFile = File(...)):
    """Обновление фонда скважин свежим CSV-экспортом фонда скважин без
    доступа к серверу: существующие скважины обновляются, новые добавляются,
    история измерений накапливается."""
    from .. import import_csv
    data = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        import_csv.run(tmp_path)
    finally:
        os.unlink(tmp_path)
    return RedirectResponse("/admin", status_code=303)


# ---------- настройки платформы ----------

@router.post("/settings")
def update_settings(daily_report_time: str = Form("06:00"),
                    validation_report_time: str = Form("07:00"),
                    scada_modbus_endpoint: str = Form(""),
                    scada_opcua_endpoint: str = Form(""),
                    db: Session = Depends(get_db)):
    services.set_setting(db, "daily_report_time", daily_report_time)
    services.set_setting(db, "validation_report_time", validation_report_time)
    services.set_setting(db, "scada_modbus_endpoint", scada_modbus_endpoint)
    services.set_setting(db, "scada_opcua_endpoint", scada_opcua_endpoint)
    return RedirectResponse("/admin", status_code=303)
