"""Точка входа для Vercel (@vercel/python, ASGI).

Инициализация данных выполняется при холодном старте: на serverless SQLite
живёт в /tmp и наполняется из data/Optiwell_Cloud.csv (см. app/bootstrap.py).
"""
from app.bootstrap import ensure_data
from app.main import app  # noqa: F401  (Vercel ищет переменную `app`)

ensure_data()
