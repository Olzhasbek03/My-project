"""Email-рассылка четырёхчасовых и суточных отчётов, а также сведений о
результатах валидации данных и состоянии передатчиков (пп. 2.1.3.5, 3.9 ТЗ).

Отчёт направляется пользователю строго в его зоне ответственности —
формирование выполняется персонально для каждого получателя.
"""
import datetime as dt
import logging
import smtplib
from email.message import EmailMessage

from sqlalchemy.orm import Session

from . import config, models, reports

log = logging.getLogger("optiwell.email")


def _send(to: str, subject: str, body: str,
          attachment: bytes, filename: str) -> bool:
    if not config.SMTP_HOST:
        log.warning("SMTP не настроен (OPTIWELL_SMTP_HOST); отчёт %s для %s не отправлен",
                    filename, to)
        return False
    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(
        attachment,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            if config.SMTP_USER:
                smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception:
        log.exception("Ошибка отправки отчёта %s на %s", filename, to)
        return False


def send_periodic_reports(db: Session, period_hours: int) -> int:
    """Разослать 4-часовой либо суточный отчёт всем подписанным пользователям."""
    stamp = dt.datetime.utcnow().strftime("%Y-%m-%d_%H%M")
    kind = "4h" if period_hours == 4 else "daily"
    sent = 0
    users = (db.query(models.User)
             .filter(models.User.receive_reports.is_(True),
                     models.User.is_active.is_(True),
                     models.User.email != "")
             .all())
    for user in users:
        data = reports.wells_report(db, user, period_hours)
        ok = _send(
            user.email,
            f"Optiwell: {'четырёхчасовой' if period_hours == 4 else 'суточный'} отчёт {stamp}",
            "Автоматический отчёт системы мониторинга скважин Optiwell.",
            data,
            f"optiwell_{kind}_{stamp}.xlsx",
        )
        sent += int(ok)
    return sent


def send_validation_report(db: Session) -> int:
    """Отдельный суточный отчёт о валидации данных и оффлайн-передатчиках."""
    stamp = dt.datetime.utcnow().strftime("%Y-%m-%d")
    data = reports.validation_report(db)
    sent = 0
    users = (db.query(models.User)
             .filter(models.User.receive_reports.is_(True),
                     models.User.role == models.ROLE_ADMIN,
                     models.User.email != "")
             .all())
    for user in users:
        ok = _send(
            user.email,
            f"Optiwell: отчёт валидации данных и оффлайн-передатчиков {stamp}",
            "Автоматический отчёт системы мониторинга скважин Optiwell.",
            data,
            f"optiwell_validation_{stamp}.xlsx",
        )
        sent += int(ok)
    return sent
