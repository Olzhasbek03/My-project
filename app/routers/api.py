"""Интеграционные API (пп. 2.1.3.1, 2.1.3.2, 2.1.3.6 ТЗ).

1. Приём uplink-данных от LoRaWAN-сервера ChirpStack v3.16.106 через
   HTTP-интеграцию (POST /api/uplink). Совместим с форматом события "up"
   ChirpStack v3; поддерживает данные расходомеров (СКЖ/БЭСКЖ-2М, КССЖ,
   NuFlo MC-II/MC-III), преобразователей давления/температуры, дискретные
   сигналы БУС и до 10 регистров 16-bit ЧРП/ИСУ по Modbus RTU.

2. Отдача данных SCADA-системе Заказчика: JSON-срез и регистровая карта
   Modbus TCP (holding registers), а также узловая структура для OPC UA
   шлюза.
"""
import base64
import datetime as dt
import struct

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import config, models, radio, services
from ..database import get_db

router = APIRouter(prefix="/api")


# ---------- приём данных от ChirpStack ----------

class RxInfo(BaseModel):
    gatewayID: str = ""
    rssi: float = 0
    loRaSNR: float = 0


class UplinkEvent(BaseModel):
    """Событие "up" HTTP-интеграции ChirpStack v3."""
    devEUI: str
    fCnt: int = 0
    fPort: int = 1
    data: str = ""                      # base64-полезная нагрузка
    rxInfo: list[RxInfo] = Field(default_factory=list)
    objectJSON: str = ""


def _check_token(authorization: str | None) -> None:
    if config.CHIRPSTACK_API_TOKEN and authorization != f"Bearer {config.CHIRPSTACK_API_TOKEN}":
        raise HTTPException(status_code=401, detail="Неверный токен интеграции")


def decode_payload(raw: bytes) -> dict:
    """Бинарный формат терминала (little-endian):
    float32 flow_rate | float32 cumulative_total | float32 pressure |
    float32 temperature | uint8 alarm | uint16 battery_mV.
    Короткие пакеты допускаются — отсутствующие поля пропускаются."""
    out: dict = {}
    if len(raw) >= 4:
        out["flow_rate"] = struct.unpack_from("<f", raw, 0)[0]
    if len(raw) >= 8:
        out["cumulative_total"] = struct.unpack_from("<f", raw, 4)[0]
    if len(raw) >= 12:
        out["pressure"] = struct.unpack_from("<f", raw, 8)[0]
    if len(raw) >= 16:
        out["temperature"] = struct.unpack_from("<f", raw, 12)[0]
    if len(raw) >= 17:
        out["alarm"] = bool(raw[16])
    if len(raw) >= 19:
        out["battery_v"] = struct.unpack_from("<H", raw, 17)[0] / 1000
    return out


@router.post("/uplink")
def chirpstack_uplink(event: UplinkEvent, db: Session = Depends(get_db),
                      authorization: str | None = Header(default=None)):
    _check_token(authorization)
    now = dt.datetime.utcnow()

    device = (db.query(models.Device)
              .filter(models.Device.dev_eui == event.devEUI).first())
    if device is None:
        device = models.Device(dev_eui=event.devEUI)
        db.add(device)
        db.flush()
    device.last_seen_at = now

    raw = base64.b64decode(event.data) if event.data else b""
    decoded = decode_payload(raw)
    if "battery_v" in decoded:
        device.battery_v = decoded["battery_v"]

    # Радиокадры для модуля радиоанализа — сохраняются всегда,
    # в том числе пустые пакеты (п. 2.1.3.4-e ТЗ)
    gateways = {g.gateway_id: g for g in db.query(models.Gateway).all()}
    for rx in event.rxInfo:
        gw = gateways.get(rx.gatewayID)
        distance = 0.0
        if gw and device.well:
            distance = radio.haversine_km(device.well.latitude, device.well.longitude,
                                          gw.latitude, gw.longitude)
        if gw:
            gw.last_seen_at = now
            gw.is_online = True
        db.add(models.UplinkFrame(dev_eui=event.devEUI, gateway_id=rx.gatewayID,
                                  rssi=rx.rssi, snr=rx.loRaSNR,
                                  payload_size=len(raw), distance_km=distance))

    measurement_id = None
    if device.well and decoded:
        prev = services.last_measurement(db, device.well_id)
        m = models.Measurement(
            well_id=device.well_id,
            measured_at=now,
            received_at=now,
            flow_rate=round(decoded.get("flow_rate", 0.0), 3),
            cumulative_total=round(decoded.get("cumulative_total", 0.0), 3),
            pressure=decoded.get("pressure"),
            temperature=decoded.get("temperature"),
            alarm=decoded.get("alarm", False),
        )
        m.is_valid = services.validate_measurement(m, prev)
        db.add(m)
        db.flush()
        measurement_id = m.id

    db.commit()
    return {"status": "ok", "measurement_id": measurement_id}


# ---------- обмен со SCADA (п. 2.1.3.6 ТЗ) ----------

@router.get("/scada/snapshot")
def scada_snapshot(db: Session = Depends(get_db)):
    """Текущий срез по всем скважинам для интеграции с существующей
    телеметрией Заказчика."""
    result = []
    for well in db.query(models.Well).all():
        m = services.last_measurement(db, well.id)
        result.append({
            "well": well.number,
            "gzu": well.gzu.name,
            "cdn": well.gzu.cdn.name,
            "status": services.well_status(db, well),
            "flow_rate": m.flow_rate if m else None,
            "cumulative_total": m.cumulative_total if m else None,
            "pressure": m.pressure if m else None,
            "temperature": m.temperature if m else None,
            "measured_at": m.measured_at.isoformat() if m else None,
        })
    return {"wells": result}


@router.get("/scada/modbus-map")
def scada_modbus_map(db: Session = Depends(get_db)):
    """Регистровая карта Modbus TCP: на каждую скважину выделено 4 holding-
    регистра начиная с адреса 40001 (дебит ×10, накопленный расход старшее/
    младшее слово, статус). Карта используется шлюзом Modbus TCP / OPC UA."""
    registers = []
    addr = 40001
    for well in db.query(models.Well).order_by(models.Well.number).all():
        m = services.last_measurement(db, well.id)
        status_code = {"in_operation": 1, "stopped": 2, "offline": 0}[
            services.well_status(db, well)]
        flow = int(round((m.flow_rate if m else 0) * 10))
        total = int(m.cumulative_total) if m else 0
        registers.append({
            "well": well.number,
            "address": addr,
            "values": [flow, (total >> 16) & 0xFFFF, total & 0xFFFF, status_code],
        })
        addr += 4
    return {"unit_id": 1, "registers": registers}
