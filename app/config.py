"""Конфигурация программного комплекса мониторинга скважин.

Все параметры переопределяются переменными окружения, чтобы комплекс можно
было развернуть на облачном сервере Заказчика без правки кода (п. 3.9 ТЗ).
"""
import os

# База данных: по умолчанию SQLite (для разработки/демо),
# в продуктиве — PostgreSQL / ClickHouse / MS SQL (п. 2.1.3.2 ТЗ), например:
#   WELLAPP_DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/wells
DATABASE_URL = os.environ.get("WELLAPP_DATABASE_URL", "sqlite:///./app.db")

# Serverless (Vercel): файловая система read-only, пишем SQLite в /tmp.
# Для постоянного хранения задайте WELLAPP_DATABASE_URL (напр. Neon Postgres).
IS_SERVERLESS = bool(os.environ.get("VERCEL"))
if IS_SERVERLESS and "WELLAPP_DATABASE_URL" not in os.environ:
    DATABASE_URL = "sqlite:////tmp/app.db"

# Секрет подписи сессионных cookie
SECRET_KEY = os.environ.get("WELLAPP_SECRET_KEY", "change-me-in-production")

# SMTP для рассылки отчётов (п. 2.1.3.5 ТЗ)
SMTP_HOST = os.environ.get("WELLAPP_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("WELLAPP_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("WELLAPP_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("WELLAPP_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("WELLAPP_SMTP_FROM", "monitoring@kbm.kz")

# Токен HTTP-интеграции ChirpStack v3 (заголовок Authorization при POST /api/uplink)
CHIRPSTACK_API_TOKEN = os.environ.get("WELLAPP_CHIRPSTACK_TOKEN", "")

# Порог «передатчик оффлайн»: нет данных дольше N минут
OFFLINE_THRESHOLD_MINUTES = int(os.environ.get("WELLAPP_OFFLINE_MINUTES", "180"))

# Порог значительного снижения дебита за 4 часа, в процентах (п. 2.1.3.5 ТЗ)
RATE_DROP_THRESHOLD_PCT = float(os.environ.get("WELLAPP_RATE_DROP_PCT", "30"))

# Языки интерфейса (п. 2.1.3.3 ТЗ)
LANGUAGES = ["ru", "kk", "en", "zh"]
DEFAULT_LANGUAGE = "ru"

# ИИ-советник (страница /predict). Встроенный экспертный анализ работает
# всегда и полностью офлайн (закрытый контур). Если задать ключ Anthropic API —
# рекомендации дополнительно формулирует Claude (нужен выход в интернет).
AI_API_KEY = os.environ.get("WELLAPP_AI_API_KEY",
                            os.environ.get("ANTHROPIC_API_KEY", ""))
AI_MODEL = os.environ.get("WELLAPP_AI_MODEL", "claude-opus-4-8")
