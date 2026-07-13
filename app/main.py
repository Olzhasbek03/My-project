"""Программный комплекс мониторинга, анализа и оптимизации скважин.

Запуск:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import contextlib
import logging

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .database import Base, engine
from .routers import admin, api, pages
from .scheduler import scheduler_loop

logging.basicConfig(level=logging.INFO)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    if config.IS_SERVERLESS:
        # на serverless нет фонового процесса — расписание отчётов должно
        # запускаться внешним cron (Vercel Cron → GET /api/cron/reports)
        yield
        return
    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()


app = FastAPI(title="Well Monitor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(pages.router)
app.include_router(admin.router)
app.include_router(api.router)


@app.exception_handler(307)
async def redirect_handler(request: Request, exc):
    return RedirectResponse(exc.headers.get("Location", "/login"))
