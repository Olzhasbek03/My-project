"""Рендеринг шаблонов с учётом языка пользователя (cookie optiwell_lang)."""
from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import config
from .i18n import RADIO_LANGS, t

templates = Jinja2Templates(directory="templates")


def render(request: Request, name: str, context: dict, status_code: int = 200,
           radio_mode: bool = False):
    lang = request.cookies.get("optiwell_lang", config.DEFAULT_LANGUAGE)
    if lang not in config.LANGUAGES:
        lang = config.DEFAULT_LANGUAGE
    # Модуль радиоанализа — казахский/английский/русский (п. 2.1.3.4 ТЗ)
    if radio_mode and lang not in RADIO_LANGS:
        lang = "ru"
    context = dict(context)
    context.update({
        "request": request,
        "lang": lang,
        "languages": RADIO_LANGS if radio_mode else config.LANGUAGES,
        "t": lambda key: t(key, lang),
    })
    return templates.TemplateResponse(request, name, context, status_code=status_code)
