"""ИИ-советник: диагностика причин падения добычи и рекомендации к действию.

Два слоя:

1. Экспертный движок (всегда, офлайн) — правила на инженерных знаниях
   месторождения: связь/радио, тип насоса (ШГН/ВН), тип расходомера
   (СКЖ/NuFlo), пороги валидации существующей системы. Работает в закрытом
   контуре без внешних сервисов.

2. Claude (опционально) — если задан WELLAPP_AI_API_KEY / ANTHROPIC_API_KEY,
   сводка данных дополнительно отправляется в Anthropic Messages API, и
   советник формулирует связный анализ поверх правил. Ответы кэшируются.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time

from . import config

log = logging.getLogger("wellapp.advisor")

SEVERITY_ORDER = {"critical": 0, "warn": 1, "info": 2, "ok": 3}

# Действия по типу насоса при остановке/нулевом дебите
_PUMP_ACTIONS = {
    "ШГН": "Проверить станок-качалку: балансир, ремни, штанги; подтвердить остановку в цехе",
    "ВН": "Проверить ЧРП/ИСУ винтового насоса (VFD-регистры терминала), электропитание",
}


def _sorted(recs: list[dict]) -> list[dict]:
    recs.sort(key=lambda r: SEVERITY_ORDER.get(r["severity"], 9))
    return recs


# ------------------------------------------------------------------ правила

def advise_fleet(gzu_rows: list[dict], window_h: int) -> list[dict]:
    """Рекомендации уровня месторождения по изменениям ГЗУ."""
    recs: list[dict] = []
    falling = [g for g in gzu_rows if g["delta"] is not None and g["delta"] < 0]
    total_loss = round(sum(-g["delta"] for g in falling), 1)
    if falling:
        worst = falling[:3]
        recs.append({
            "severity": "critical" if total_loss > 100 else "warn",
            "title": f"Потеря добычи {total_loss} т/сут за {window_h} ч",
            "detail": "Наибольший вклад: " + ", ".join(
                f"{g['name']} ({g['delta']} т/сут)" for g in worst),
            "actions": [f"Открыть {g['name']} и разобрать скважины с падением"
                        for g in worst],
        })
    dark = [g for g in gzu_rows if g["wells"] and g["offline"] >= max(3, g["wells"] // 2)]
    for g in dark[:3]:
        recs.append({
            "severity": "warn",
            "title": f"{g['name']}: {g['offline']} из {g['wells']} скважин без связи",
            "detail": "Массовый оффлайн в одной группе — признак проблемы базовой "
                      "станции, а не отдельных терминалов.",
            "actions": ["Проверить ближайшую БС (питание, антенна) в радиоанализе",
                        "Сверить RSSI группы на странице «Радиоанализ»"],
        })
    no_data = [g for g in gzu_rows if g["paired"] == 0]
    if no_data:
        recs.append({
            "severity": "info",
            "title": f"Без пары замеров за окно: {len(no_data)} ГЗУ",
            "detail": "Для этих групп нет двух замеров с интервалом "
                      f"{window_h} ч — динамика не вычисляется.",
            "actions": ["Проверить периодичность опроса терминалов в админке"],
        })
    if not recs:
        recs.append({"severity": "ok", "title": "Существенных потерь не выявлено",
                     "detail": f"Суммарная добыча стабильна в окне {window_h} ч.",
                     "actions": []})
    return _sorted(recs)


def advise_gzu(gzu_name: str, well_rows: list[dict], window_h: int) -> list[dict]:
    """Рекомендации по скважинам одного ГЗУ."""
    recs: list[dict] = []
    offline = [r for r in well_rows if not r["online"]]
    if len(offline) >= max(3, len(well_rows) // 2) and well_rows:
        recs.append({
            "severity": "critical",
            "title": f"Массовая потеря связи: {len(offline)} из {len(well_rows)}",
            "detail": "Похоже на отказ базовой станции или питания узла, "
                      "а не отдельных терминалов.",
            "actions": ["Проверить БС группы (радиоанализ: RSSI, кадры за 24 ч)",
                        "Выезд на узел связи при подтверждении"],
        })
    elif offline:
        nums = ", ".join(r["number"] for r in offline[:8])
        recs.append({
            "severity": "warn",
            "title": f"Оффлайн {len(offline)} скв.: {nums}",
            "detail": "Терминалы не выходят на связь.",
            "actions": ["Проверить батарею/антенну терминалов",
                        "Сверить зону слабого сигнала (RSSI < −120 дБм) в радиоанализе"],
        })

    droppers = [r for r in well_rows
                if r["pct"] is not None and r["pct"] <= -config.RATE_DROP_THRESHOLD_PCT]
    for r in droppers[:6]:
        pump = _PUMP_ACTIONS.get(r["well_type"], "проверить насосное оборудование")
        recs.append({
            "severity": "critical" if r["pct"] <= -70 else "warn",
            "title": f"Скв. {r['number']}: дебит {r['q_prev']} → {r['q_now']} т/сут ({r['pct']}%)",
            "detail": f"Падение за {window_h} ч превышает порог "
                      f"{config.RATE_DROP_THRESHOLD_PCT:.0f}%.",
            "actions": [pump,
                        f"Проверить расходомер {r['meter_type']}: забивка, валидация замера",
                        "Сравнить с соседними скважинами ГЗУ (общий коллектор?)"],
        })

    zero = [r for r in well_rows
            if r["online"] and r["q_now"] == 0 and r["work_status"] != "Stop"]
    if zero:
        nums = ", ".join(r["number"] for r in zero[:8])
        recs.append({
            "severity": "warn",
            "title": f"Нулевой дебит при связи: {nums}",
            "detail": "Терминал на связи, но расход нулевой и остановка не оформлена.",
            "actions": ["Подтвердить фактическое состояние скважины в цехе",
                        "Проверить расходомер (датчик/импульсы)"],
        })

    rising = [r for r in well_rows if r["pct"] is not None and r["pct"] >= 30]
    if rising:
        recs.append({
            "severity": "info",
            "title": f"Рост дебита: {len(rising)} скв.",
            "detail": "Резкий рост тоже стоит проверить — возможна погрешность замера.",
            "actions": ["Выборочно сверить замеры с ручными"],
        })

    if not recs:
        recs.append({"severity": "ok", "title": f"{gzu_name}: отклонений не выявлено",
                     "detail": f"Дебит скважин стабилен в окне {window_h} ч.",
                     "actions": []})
    return _sorted(recs)


def advise_well(row: dict, forecast: dict, window_h: int) -> list[dict]:
    """Рекомендации по одной скважине с учётом прогноза."""
    recs: list[dict] = []
    if not row["online"]:
        recs.append({
            "severity": "critical",
            "title": "Терминал не на связи",
            "detail": "Данные не поступают — состояние скважины неизвестно.",
            "actions": ["Проверить батарею и антенну терминала",
                        "Проверить RSSI/ближайшую БС в радиоанализе",
                        "Запланировать выезд при отсутствии связи > 24 ч"],
        })
    if row["pct"] is not None and row["pct"] <= -config.RATE_DROP_THRESHOLD_PCT:
        pump = _PUMP_ACTIONS.get(row["well_type"], "проверить насосное оборудование")
        recs.append({
            "severity": "critical" if row["pct"] <= -70 else "warn",
            "title": f"Падение дебита {row['pct']}% за {window_h} ч",
            "detail": f"{row['q_prev']} → {row['q_now']} т/сут.",
            "actions": [pump,
                        f"Проверить расходомер {row['meter_type']}",
                        "Проверить обводнённость и линейное давление"],
        })
    slope = forecast.get("slope_per_h")
    projected = forecast.get("projected")
    if slope is not None and slope < 0 and row["q_now"]:
        hours_to_zero = row["q_now"] / -slope if slope else None
        if projected == 0:
            recs.append({
                "severity": "critical",
                "title": f"Прогноз: остановка в ближайшие {window_h} ч",
                "detail": f"Тренд {slope:+.2f} т/сут·ч — при сохранении динамики "
                          "дебит обнулится.",
                "actions": ["Немедленно проверить насос и подачу",
                            "Предупредить цех о вероятной остановке"],
            })
        elif hours_to_zero and hours_to_zero < 72:
            recs.append({
                "severity": "warn",
                "title": f"Нисходящий тренд: ~{hours_to_zero:.0f} ч до нуля",
                "detail": f"Прогноз через {window_h} ч: {projected} т/сут "
                          f"(тренд {slope:+.2f} т/сут·ч).",
                "actions": ["Поставить скважину на контроль",
                            "Проверить динамику соседних скважин ГЗУ"],
            })
    if not recs:
        recs.append({"severity": "ok", "title": "Отклонений не выявлено",
                     "detail": "Дебит стабилен, связь в норме.", "actions": []})
    return _sorted(recs)


# ---------------------------------------------------- опциональный Claude

_ai_cache: dict[str, tuple[float, str]] = {}
_AI_CACHE_TTL = 900  # 15 минут


def ai_enabled() -> bool:
    return bool(config.AI_API_KEY)


def ai_narrative(scope: str, payload: dict) -> str | None:
    """Связный анализ от Claude поверх правил (если настроен ключ).

    Возвращает текст рекомендаций или None (не настроено / ошибка) —
    страница в любом случае показывает результат экспертного движка.
    """
    if not ai_enabled():
        return None
    key = scope + hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    hit = _ai_cache.get(key)
    if hit and time.time() - hit[0] < _AI_CACHE_TTL:
        return hit[1]
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.AI_API_KEY, timeout=30.0)
        response = client.messages.create(
            model=config.AI_MODEL,
            max_tokens=1200,
            thinking={"type": "adaptive"},
            system=(
                "Ты — инженер-аналитик нефтедобычи на месторождении Каражанбас "
                "(ШГН и винтовые насосы, телеметрия LoRaWAN, расходомеры СКЖ и "
                "NuFlo MC-II, группы скважин ГЗУ). По сводке данных мониторинга "
                "дай краткий анализ по-русски: главные риски, вероятные причины, "
                "конкретные действия по приоритету. Не выдумывай данных, "
                "опирайся только на сводку. Максимум 8 пунктов."
            ),
            messages=[{"role": "user", "content": json.dumps(
                payload, ensure_ascii=False, default=str)}],
        )
        if response.stop_reason == "refusal":
            return None
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if text:
            _ai_cache[key] = (time.time(), text)
            return text
    except Exception:
        log.exception("Советник Claude недоступен — работает экспертный движок")
    return None
