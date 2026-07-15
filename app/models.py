"""Модель данных программного комплекса мониторинга скважин.

Иерархия объектов Заказчика: ЦДН (цех добычи нефти) → ГЗУ (групповая
замерная установка) → скважина. Права доступа пользователей привязываются
к уровню иерархии (п. 2.1.3.5 ТЗ).
"""
import datetime as dt

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

utcnow = dt.datetime.utcnow


class Cdn(Base):
    """ЦДН — цех добычи нефти."""
    __tablename__ = "cdn"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    gzus: Mapped[list["Gzu"]] = relationship(back_populates="cdn")


class Gzu(Base):
    """ГЗУ — групповая замерная установка."""
    __tablename__ = "gzu"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    cdn_id: Mapped[int] = mapped_column(ForeignKey("cdn.id"))
    cdn: Mapped[Cdn] = relationship(back_populates="gzus")
    wells: Mapped[list["Well"]] = relationship(back_populates="gzu")


# Типы фонда скважин
FUND_PRODUCING = "producing"      # добывающий фонд
FUND_INJECTION = "injection"      # нагнетательный фонд (ППД)
FUND_IDLE = "idle"                # бездействие (БД)

# Типы расходомеров по ТЗ (п. 2.1)
METER_TYPES = ["СКЖ (БЭСКЖ-2М)", "КССЖ", "NuFlo MC-II", "NuFlo MC-III"]

# Типы скважин (способ эксплуатации): ШГН — штанговый глубинный насос (Srp),
# ВН — винтовой насос (Pcp)
WELL_TYPES = ["ШГН", "ВН"]


class Well(Base):
    __tablename__ = "well"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(32), unique=True)   # номер скважины
    gzu_id: Mapped[int] = mapped_column(ForeignKey("gzu.id"))
    gzu: Mapped[Gzu] = relationship(back_populates="wells")
    fund: Mapped[str] = mapped_column(String(16), default=FUND_PRODUCING)
    meter_type: Mapped[str] = mapped_column(String(32), default=METER_TYPES[0])
    well_type: Mapped[str] = mapped_column(String(8), default="ШГН")     # ШГН | ВН
    work_status: Mapped[str] = mapped_column(String(16), default="")     # Running | Stop | ""
    work_hours: Mapped[str] = mapped_column(String(16), default="-")     # наработка, ч
    latitude: Mapped[float] = mapped_column(Float, default=0.0)
    longitude: Mapped[float] = mapped_column(Float, default=0.0)
    water_cut_pct: Mapped[float] = mapped_column(Float, default=0.0)  # обводнённость, %
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)    # опрос активирован
    device: Mapped["Device"] = relationship(back_populates="well", uselist=False)
    comments: Mapped[list["WellComment"]] = relationship(back_populates="well")


class Device(Base):
    """Скважинный приёмо-передающий терминал LoRaWAN (п. 2.1.1 ТЗ)."""
    __tablename__ = "device"
    id: Mapped[int] = mapped_column(primary_key=True)
    dev_eui: Mapped[str] = mapped_column(String(23), unique=True)
    serial_number: Mapped[str] = mapped_column(String(64), default="")
    well_id: Mapped[int | None] = mapped_column(ForeignKey("well.id"), nullable=True)
    well: Mapped[Well | None] = relationship(back_populates="device")
    # Настраиваемые удалённо параметры (п. 2.1.1 ТЗ)
    report_interval_sec: Mapped[int] = mapped_column(Integer, default=3600)  # 1 сек … 10 ч
    modbus_registers: Mapped[str] = mapped_column(String(255), default="")   # считываемые регистры
    battery_v: Mapped[float] = mapped_column(Float, default=3.6)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class Gateway(Base):
    """Базовая станция LoRaWAN (п. 2.1.2 ТЗ). Всего на месторождении 14 ед."""
    __tablename__ = "gateway"
    id: Mapped[int] = mapped_column(primary_key=True)
    gateway_id: Mapped[str] = mapped_column(String(23), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    latitude: Mapped[float] = mapped_column(Float, default=0.0)
    longitude: Mapped[float] = mapped_column(Float, default=0.0)
    is_online: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class Measurement(Base):
    """Показание расходомера, принятое с терминала."""
    __tablename__ = "measurement"
    id: Mapped[int] = mapped_column(primary_key=True)
    well_id: Mapped[int] = mapped_column(ForeignKey("well.id"), index=True)
    well: Mapped[Well] = relationship()
    measured_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)   # время опроса на скважине
    received_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)  # время получения на сервере
    flow_rate: Mapped[float] = mapped_column(Float, default=0.0)        # мгновенный дебит, м3/сут
    cumulative_total: Mapped[float] = mapped_column(Float, default=0.0)  # накопленный расход
    pressure: Mapped[float | None] = mapped_column(Float, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    alarm: Mapped[bool] = mapped_column(Boolean, default=False)  # дискретный вход: остановка скважины
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)  # результат валидации


class UplinkFrame(Base):
    """Радиопараметры каждого uplink-пакета — основа модуля радиоанализа
    (п. 2.1.3.4 ТЗ). Модуль самостоятельный, не на стороне ChirpStack."""
    __tablename__ = "uplink_frame"
    id: Mapped[int] = mapped_column(primary_key=True)
    dev_eui: Mapped[str] = mapped_column(String(23), index=True)
    gateway_id: Mapped[str] = mapped_column(String(23), index=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    rssi: Mapped[float] = mapped_column(Float, default=0.0)
    snr: Mapped[float] = mapped_column(Float, default=0.0)
    frequency: Mapped[float] = mapped_column(Float, default=868.1)
    payload_size: Mapped[int] = mapped_column(Integer, default=0)  # 0 = пустой пакет
    distance_km: Mapped[float] = mapped_column(Float, default=0.0)  # терминал → БС


class RawUplink(Base):
    """Сырой uplink-пакет как есть, до декодирования. Служит журналом для
    обратной разработки формата пакета (ChirpStack без кодека, декодирование
    на нашей стороне) — по сопоставлению raw_hex с известными значениями
    портала калибруются раскладки в app/decoder.py. Сохраняется всегда,
    даже если пакет не удалось декодировать."""
    __tablename__ = "raw_uplink"
    id: Mapped[int] = mapped_column(primary_key=True)
    dev_eui: Mapped[str] = mapped_column(String(23), index=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    fport: Mapped[int] = mapped_column(Integer, default=0)
    fcnt: Mapped[int] = mapped_column(Integer, default=0)
    raw_hex: Mapped[str] = mapped_column(String(255), default="")   # полезная нагрузка в hex
    decoded: Mapped[bool] = mapped_column(Boolean, default=False)   # удалось ли распознать поля


class WellComment(Base):
    """Комментарий по скважине с историей: дата, время, автор (п. 2.1.3.3 ТЗ)."""
    __tablename__ = "well_comment"
    id: Mapped[int] = mapped_column(primary_key=True)
    well_id: Mapped[int] = mapped_column(ForeignKey("well.id"), index=True)
    well: Mapped[Well] = relationship(back_populates="comments")
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    text: Mapped[str] = mapped_column(Text)


# Роли пользователей
ROLE_ADMIN = "admin"
ROLE_CDN = "cdn"    # пользователь уровня ЦДН
ROLE_GZU = "gzu"    # пользователь уровня ГЗУ


class User(Base):
    __tablename__ = "user"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    email: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default=ROLE_GZU)
    # Зона ответственности (п. 2.1.3.5 ТЗ): для роли gzu — список ГЗУ,
    # для роли cdn — список ЦДН; admin видит всё.
    scope: Mapped[list["UserScope"]] = relationship(back_populates="user",
                                                    cascade="all, delete-orphan")
    receive_reports: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class UserScope(Base):
    __tablename__ = "user_scope"
    __table_args__ = (UniqueConstraint("user_id", "level", "object_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    user: Mapped[User] = relationship(back_populates="scope")
    level: Mapped[str] = mapped_column(String(8))  # "gzu" | "cdn"
    object_id: Mapped[int] = mapped_column(Integer)


class Setting(Base):
    """Настройки платформы, редактируемые администратором без доступа к СУБД:
    расписание отчётов, параметры SCADA-обмена и т.п. (п. 2.1.3.3, 2.1.3.7 ТЗ)."""
    __tablename__ = "setting"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), default="")
