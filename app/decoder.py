"""Декодер бинарной полезной нагрузки скважинных терминалов LoRaWAN
(п. 2.1.3.1 ТЗ).

ВАЖНО: ChirpStack настроен без кодека (Payload codec = None) — сервер сети
передаёт «сырые» байты, а декодирование выполняется здесь. Точный формат
пакета документально не задан производителем и восстанавливается методом
обратной разработки по сопоставленным парам «сырые байты ↔ известные
значения» (см. docs/PAYLOAD.md и tools/analyze_payload.py).

Пока смещения полей не подтверждены реальными образцами, каждое поле
описано как ЗАГОТОВКА (`FieldSpec`) и легко правится в таблицах ниже — без
изменения остального кода. Любой принятый пакет всегда сохраняется целиком
(app/models.RawUplink), поэтому калибровку можно выполнить и задним числом.

Наблюдения по образцам из журнала существующей системы:
  • пакеты короткие: VFD ≈ 13 байт, flowmeter ≈ 9 байт;
  • повторяется маркер `01 00` (uint16 LE = 1) на фиксированном смещении;
  • типы сообщений: VFD (ЧРП), flowmeter (расходомер), TS (метка времени) —
    вероятно, разнесены по разным FPort (наблюдались FPort 2 и FPort 5);
  • правило существующей системы: изменение накопленного расхода > 100000
    между пакетами → «TRASH PACKET» (см. valid_accumulated_delta).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

# Порог «мусорного» пакета из существующей системы: скачок накопленного
# расхода больше этого значения между соседними пакетами — брак.
ACCUMULATED_DELTA_LIMIT = 100000.0


@dataclass(frozen=True)
class FieldSpec:
    """Описание одного числового поля в пакете.

    name    — ключ результата (flow_rate, cumulative_total, pressure, …);
    offset  — смещение в байтах;
    fmt     — код struct: 'f' float32, 'H' uint16, 'I' uint32, 'h' int16, …;
    endian  — '<' little-endian (по умолчанию) или '>' big-endian;
    scale   — множитель (датчики часто шлют целое ×10/×100);
    confirmed — подтверждено ли смещение реальными образцами.
    """
    name: str
    offset: int
    fmt: str
    endian: str = "<"
    scale: float = 1.0
    confirmed: bool = False

    @property
    def size(self) -> int:
        return struct.calcsize(self.endian + self.fmt)

    def read(self, raw: bytes):
        if self.offset + self.size > len(raw):
            return None
        val = struct.unpack_from(self.endian + self.fmt, raw, self.offset)[0]
        return val * self.scale


@dataclass
class PayloadLayout:
    """Раскладка пакета для конкретного типа сообщения / расходомера."""
    label: str
    fields: list[FieldSpec] = field(default_factory=list)

    def decode(self, raw: bytes) -> dict:
        out: dict = {}
        for spec in self.fields:
            val = spec.read(raw)
            if val is not None:
                out[spec.name] = val
        return out

    @property
    def calibrated(self) -> bool:
        return bool(self.fields) and all(f.confirmed for f in self.fields)


# --------------------------------------------------------------------------
# ТАБЛИЦЫ РАСКЛАДОК — заполняются по мере подтверждения образцами.
# Смещения ниже помечены confirmed=False (ГИПОТЕЗА). После получения
# сопоставленных пар прогнать tools/analyze_payload.py --find <значение>,
# вписать реальные offset/fmt/scale и выставить confirmed=True.
# --------------------------------------------------------------------------

# Тип расходомера из фонда → семейство раскладки. Значения приходят из
# импортируемого CSV/фонда: skg, skg3v1, mc2 и т.п.
METER_FAMILY = {
    "skg": "SKG",
    "skg3v1": "SKG",
    "СКЖ (БЭСКЖ-2М)": "SKG",
    "КССЖ": "SKG",
    "mc2": "MC2",
    "NuFlo MC-II": "MC2",
    "NuFlo MC-III": "MC2",
}

# Раскладки flowmeter-пакета по семейству расходомера (ГИПОТЕЗЫ).
FLOWMETER_LAYOUTS: dict[str, PayloadLayout] = {
    "SKG": PayloadLayout("SKG flowmeter", [
        # TODO: подтвердить образцами. Пакет ≈ 9 байт.
        # FieldSpec("flow_rate", offset=?, fmt="f", confirmed=True),
        # FieldSpec("cumulative_total", offset=?, fmt="I", confirmed=True),
    ]),
    "MC2": PayloadLayout("NuFlo MC-II flowmeter", [
        # TODO: подтвердить образцами.
    ]),
}

# Раскладка composite/VFD-пакета (FPort 2, 13 байт). По 10 образцам
# (2 живых + 8 из журнала) структура такова:
#   [0]     флаг статуса (почти всегда 00, изредка 01)
#   [1:6]   00 00 00 00 00 — КОНСТАНТА (ноль во всех образцах)
#   [6]     01 — КОНСТАНТА, маркер
#   [7]     00 = секция VFD присутствует | FF = расходомер отключён
#   [8:13]  5-байтовый VFD-хвост (регистры ЧРП)
# ВАЖНО: в этих пакетах НЕТ данных о расходе — байты [0:6] всегда нули, а
# VFD-хвост у всех собранных образцов невалиден («wrong VFD payload»).
# Реальный дебит приходит в отдельном flowmeter-пакете (форма 0D 20 01 00 …).
# Поэтому раскладка пока пуста: пакеты копятся в raw_uplink до тех пор, пока
# не будет пойман валидный flowmeter-пакет живой скважины для калибровки.
COMPOSITE_LAYOUT = PayloadLayout("composite/VFD (FPort 2)", [
    # TODO: заполнить по валидному flowmeter-пакету (см. docs/PAYLOAD.md).
])

# Раскладка чистого VFD-пакета (данные ЧРП, до 10 регистров Modbus 16-bit).
VFD_LAYOUT = PayloadLayout("VFD / ЧРП", [
    # TODO: подтвердить образцами.
])


# --------------------------------------------------------------------------
# Диспетчеризация
# --------------------------------------------------------------------------

# FPort → тип сообщения (по наблюдениям):
#   FPort 2 — рабочий composite-пакет (накопл. расход + VFD), 13 байт;
#   FPort 5 — только первый пакет после подключения (FCnt 0), стартовое/TS.
FPORT_MESSAGE_TYPE = {
    2: "composite",
    5: "ts",
}


def message_type(fport: int) -> str:
    return FPORT_MESSAGE_TYPE.get(fport, "composite")


def meter_family(meter_type: str | None) -> str:
    if not meter_type:
        return "SKG"
    return METER_FAMILY.get(meter_type, METER_FAMILY.get(meter_type.lower(), "SKG"))


def layout_for(fport: int, meter_type: str | None) -> PayloadLayout:
    kind = message_type(fport)
    if kind == "composite":
        return COMPOSITE_LAYOUT
    if kind == "vfd":
        return VFD_LAYOUT
    return FLOWMETER_LAYOUTS.get(meter_family(meter_type),
                                 FLOWMETER_LAYOUTS["SKG"])


def decode(raw: bytes, fport: int = 0, meter_type: str | None = None) -> dict:
    """Декодировать пакет. Возвращает словарь распознанных полей.

    Пока раскладка не откалибрована, вернёт пустой словарь — но сырой пакет
    всё равно сохраняется вызывающей стороной (app/routers/api.py), поэтому
    ничего не теряется и калибровка возможна задним числом.
    """
    if not raw:
        return {}
    layout = layout_for(fport, meter_type)
    result = layout.decode(raw)
    result["_layout"] = layout.label
    result["_calibrated"] = layout.calibrated
    return result


def valid_accumulated_delta(new_total: float | None,
                            prev_total: float | None) -> bool:
    """Правило существующей системы: скачок накопленного расхода больше
    ACCUMULATED_DELTA_LIMIT между соседними пакетами — «TRASH PACKET»."""
    if new_total is None or prev_total is None:
        return True
    return abs(new_total - prev_total) <= ACCUMULATED_DELTA_LIMIT
