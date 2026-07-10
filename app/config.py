"""Конфигурация программного комплекса Optiwell.

Все параметры переопределяются переменными окружения, чтобы комплекс можно
было развернуть на облачном сервере Заказчика без правки кода (п. 3.9 ТЗ).
"""
import os

# База данных: по умолчанию SQLite (для разработки/демо),
# в продуктиве — PostgreSQL / ClickHouse / MS SQL (п. 2.1.3.2 ТЗ), например:
#   OPTIWELL_DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/optiwell
DATABASE_URL = os.environ.get("OPTIWELL_DATABASE_URL", "sqlite:///./optiwell.db")

# Секрет подписи сессионных cookie
SECRET_KEY = os.environ.get("OPTIWELL_SECRET_KEY", "change-me-in-production")

# SMTP для рассылки отчётов (п. 2.1.3.5 ТЗ)
SMTP_HOST = os.environ.get("OPTIWELL_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("OPTIWELL_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("OPTIWELL_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("OPTIWELL_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("OPTIWELL_SMTP_FROM", "optiwell@kbm.kz")

# Токен HTTP-интеграции ChirpStack v3 (заголовок Authorization при POST /api/uplink)
CHIRPSTACK_API_TOKEN = os.environ.get("OPTIWELL_CHIRPSTACK_TOKEN", "")

# Порог «передатчик оффлайн»: нет данных дольше N минут
OFFLINE_THRESHOLD_MINUTES = int(os.environ.get("OPTIWELL_OFFLINE_MINUTES", "180"))

# Порог значительного снижения дебита за 4 часа, в процентах (п. 2.1.3.5 ТЗ)
RATE_DROP_THRESHOLD_PCT = float(os.environ.get("OPTIWELL_RATE_DROP_PCT", "30"))

# Языки интерфейса (п. 2.1.3.3 ТЗ)
LANGUAGES = ["ru", "kk", "en", "zh"]
DEFAULT_LANGUAGE = "ru"
