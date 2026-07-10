"""Формирование отчётов в формате .xls(x) на китайском, английском и русском
языках (п. 2.1.3.5 ТЗ):

- четырёхчасовой и суточный отчёты: дебит по каждой скважине (включая нулевой),
  состояние (в работе / остановлена), скважины со значительным снижением дебита
  за последние 4 часа;
- отдельный суточный отчёт: результаты валидации данных и передатчики оффлайн;
- отчёт формируется в разрезе зоны ответственности пользователя
  («скважина – ГЗУ – ЦДН»), данные вне зоны не включаются.
"""
import datetime as dt
import io

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from sqlalchemy.orm import Session

from . import config, models, services
from .auth import visible_wells_query

# Заголовки на трёх языках отчёта: русский / английский / китайский
HEADERS = [
    ("Скважина", "Well", "油井"),
    ("ГЗУ", "GZU", "计量站"),
    ("ЦДН", "CDN", "采油车间"),
    ("Дебит, м³", "Rate, m³", "产量, m³"),
    ("Состояние", "Status", "状态"),
    ("Снижение дебита за 4 ч, %", "4h rate drop, %", "4小时产量降幅, %"),
]

STATUS_LABELS = {
    "in_operation": "В работе / In operation / 运行中",
    "stopped": "Остановлена / Stopped / 已停止",
    "offline": "Оффлайн / Offline / 离线",
}

_bold = Font(bold=True)
_warn_fill = PatternFill("solid", start_color="FFF2CC")


def _tri(ru: str, en: str, zh: str) -> str:
    return f"{ru} / {en} / {zh}"


def _write_headers(ws) -> None:
    for col, (ru, en, zh) in enumerate(HEADERS, start=1):
        cell = ws.cell(row=1, column=col, value=_tri(ru, en, zh))
        cell.font = _bold
        ws.column_dimensions[cell.column_letter].width = 28


def wells_report(db: Session, user: models.User, period_hours: int) -> bytes:
    """Четырёхчасовой (period_hours=4) или суточный (period_hours=24) отчёт."""
    now = services.utcnow()
    start = now - dt.timedelta(hours=period_hours)
    wb = Workbook()
    ws = wb.active
    ws.title = "4h" if period_hours == 4 else "Daily"
    _write_headers(ws)

    wells = visible_wells_query(db, user).order_by(models.Well.number).all()
    row = 2
    for well in wells:
        status = services.well_status(db, well)
        volume = services.volume_for_period(db, well.id, start, now)
        drop = services.rate_drop_pct(db, well.id, now)
        values = [well.number, well.gzu.name, well.gzu.cdn.name, volume,
                  STATUS_LABELS[status], drop]
        for col, value in enumerate(values, start=1):
            ws.cell(row=row, column=col, value=value)
        if drop >= config.RATE_DROP_THRESHOLD_PCT:
            for col in range(1, len(values) + 1):
                ws.cell(row=row, column=col).fill = _warn_fill
        row += 1

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def validation_report(db: Session) -> bytes:
    """Отдельный суточный отчёт: результаты валидации данных и передатчики
    в режиме «оффлайн» (п. 2.1.3.5 ТЗ). Формат по умолчанию — .xls(x)."""
    now = services.utcnow()
    day_ago = now - dt.timedelta(hours=24)
    wb = Workbook()

    ws = wb.active
    ws.title = "Validation"
    headers = [
        ("Скважина", "Well", "油井"),
        ("Всего измерений за сутки", "Readings per day", "每日读数"),
        ("Невалидных", "Invalid", "无效数据"),
    ]
    for col, (ru, en, zh) in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=_tri(ru, en, zh))
        cell.font = _bold
        ws.column_dimensions[cell.column_letter].width = 32
    row = 2
    for well in db.query(models.Well).order_by(models.Well.number).all():
        q = (db.query(models.Measurement)
             .filter(models.Measurement.well_id == well.id,
                     models.Measurement.received_at >= day_ago))
        total = q.count()
        invalid = q.filter(models.Measurement.is_valid.is_(False)).count()
        ws.cell(row=row, column=1, value=well.number)
        ws.cell(row=row, column=2, value=total)
        ws.cell(row=row, column=3, value=invalid)
        row += 1

    ws2 = wb.create_sheet("Offline")
    off_headers = [
        ("DevEUI", "DevEUI", "DevEUI"),
        ("Скважина", "Well", "油井"),
        ("Последний выход на связь", "Last seen", "最后在线时间"),
    ]
    for col, (ru, en, zh) in enumerate(off_headers, start=1):
        cell = ws2.cell(row=1, column=col, value=_tri(ru, en, zh))
        cell.font = _bold
        ws2.column_dimensions[cell.column_letter].width = 32
    for i, dev in enumerate(services.offline_devices(db), start=2):
        ws2.cell(row=i, column=1, value=dev.dev_eui)
        ws2.cell(row=i, column=2, value=dev.well.number if dev.well else "—")
        ws2.cell(row=i, column=3,
                 value=dev.last_seen_at.strftime("%d.%m.%Y %H:%M") if dev.last_seen_at else "—")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def wells_excel_export(db: Session, user: models.User) -> bytes:
    """Выгрузка отображаемых данных фонда скважин из интерфейса в Excel
    (п. 2.1.3.3 ТЗ)."""
    prev_start, prev_end = services.previous_day_bounds()
    wb = Workbook()
    ws = wb.active
    ws.title = "Wells"
    cols = ["Скважина", "ГЗУ", "ЦДН", "Фонд", "Тип расходомера",
            "Последнее показание, м³/сут", "Время опроса", "Время получения",
            "Дебит за пред. сутки, м³", "Статус"]
    for c, name in enumerate(cols, start=1):
        cell = ws.cell(row=1, column=c, value=name)
        cell.font = _bold
        ws.column_dimensions[cell.column_letter].width = 24
    fund_names = {models.FUND_PRODUCING: "Добывающий",
                  models.FUND_INJECTION: "Нагнетательный (ППД)",
                  models.FUND_IDLE: "Бездействие"}
    row = 2
    for well in visible_wells_query(db, user).order_by(models.Well.number).all():
        m = services.last_measurement(db, well.id)
        ws.cell(row=row, column=1, value=well.number)
        ws.cell(row=row, column=2, value=well.gzu.name)
        ws.cell(row=row, column=3, value=well.gzu.cdn.name)
        ws.cell(row=row, column=4, value=fund_names[well.fund])
        ws.cell(row=row, column=5, value=well.meter_type)
        ws.cell(row=row, column=6, value=m.flow_rate if m else None)
        ws.cell(row=row, column=7,
                value=m.measured_at.strftime("%d.%m.%Y %H:%M") if m else "—")
        ws.cell(row=row, column=8,
                value=m.received_at.strftime("%d.%m.%Y %H:%M") if m else "—")
        ws.cell(row=row, column=9,
                value=services.volume_for_period(db, well.id, prev_start, prev_end))
        ws.cell(row=row, column=10, value=STATUS_LABELS[services.well_status(db, well)])
        row += 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
