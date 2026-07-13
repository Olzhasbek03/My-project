"""Планировщик рассылки отчётов (п. 2.1.3.5 ТЗ).

Четырёхчасовые отчёты уходят в 00/04/08/12/16/20 часов; время суточного
отчёта и отчёта валидации настраивается администратором через интерфейс
платформы (вкладка «Расписание отчётов», п. 2.1.3.3 ТЗ)."""
import asyncio
import datetime as dt
import logging

from .database import SessionLocal
from . import emailer, services

log = logging.getLogger("wellapp.scheduler")

CHECK_INTERVAL_SEC = 60


async def scheduler_loop() -> None:
    last_fired: dict[str, str] = {}
    while True:
        try:
            _tick(last_fired)
        except Exception:
            log.exception("Ошибка планировщика отчётов")
        await asyncio.sleep(CHECK_INTERVAL_SEC)


def _tick(last_fired: dict[str, str]) -> None:
    now = dt.datetime.utcnow()
    hhmm = now.strftime("%H:%M")
    day_hour = now.strftime("%Y-%m-%d %H")

    with SessionLocal() as db:
        daily_at = services.get_setting(db, "daily_report_time", "06:00")
        validation_at = services.get_setting(db, "validation_report_time", "07:00")

        # Четырёхчасовой отчёт — на границе каждого 4-часового интервала
        if now.hour % 4 == 0 and now.minute == 0 and last_fired.get("4h") != day_hour:
            last_fired["4h"] = day_hour
            n = emailer.send_periodic_reports(db, 4)
            log.info("Разослан 4-часовой отчёт: %d получателей", n)

        if hhmm == daily_at and last_fired.get("daily") != day_hour:
            last_fired["daily"] = day_hour
            n = emailer.send_periodic_reports(db, 24)
            log.info("Разослан суточный отчёт: %d получателей", n)

        if hhmm == validation_at and last_fired.get("validation") != day_hour:
            last_fired["validation"] = day_hour
            n = emailer.send_validation_report(db)
            log.info("Разослан отчёт валидации: %d получателей", n)
