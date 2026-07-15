#!/usr/bin/env python3
"""Поиск калибровочных пакетов в накопленном сыром трафике (raw_uplink).

После развёртывания портал сохраняет КАЖДЫЙ uplink в таблицу raw_uplink.
Ручной сбор образцов постоянно попадал на пакеты с отключённым приводом/
расходомером (см. docs/PAYLOAD.md). Этот скрипт разбирает весь накопленный
трафик и выделяет то, что нужно для калибровки:

  • кластеры пакетов по (fport, длина, форма);
  • «мёртвые» VFD-пакеты (00 00 00 00 00 00 01 00 …) и FF-расходомеры —
    отбрасываются;
  • ВАЛИДНЫЕ flowmeter-пакеты (иная форма, ненулевые данные) — то, по чему
    калибруется дебит;
  • для скважин с несколькими пакетами — динамика: как меняется полезная
    часть между соседними опросами (прирост = расход за интервал).

Запуск (на сервере, где развёрнут портал):
    python -m tools.calibrate_from_db
    python -m tools.calibrate_from_db --dev a84041371185a06f   # одна скважина
"""
import argparse
from collections import defaultdict

from app.database import SessionLocal
from app import models


# «Мёртвый» VFD-пакет: 8-байтовый нулевой заголовок + маркер, привод не отвечает.
DEAD_VFD_PREFIX = "0000000000000100"


def is_dead_vfd(hex_str: str) -> bool:
    return hex_str.startswith(DEAD_VFD_PREFIX)


def is_flowmeter_fault(hex_str: str) -> bool:
    # 00 00 00 00 00 00 01 FF FF FF FF FF FF — расходомер физически отключён
    return hex_str.startswith("00000000000001FF")


def classify(hex_str: str) -> str:
    if is_flowmeter_fault(hex_str):
        return "flowmeter-fault (FF)"
    if is_dead_vfd(hex_str):
        return "dead-VFD (нет данных)"
    return "VALID (кандидат на калибровку)"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dev", help="фильтр по devEUI")
    ap.add_argument("--limit", type=int, default=20, help="сколько валидных показать")
    args = ap.parse_args()

    db = SessionLocal()
    q = db.query(models.RawUplink).order_by(models.RawUplink.received_at)
    if args.dev:
        q = q.filter(models.RawUplink.dev_eui == args.dev)
    rows = q.all()
    db.close()

    if not rows:
        print("raw_uplink пуст — портал ещё не принимал пакеты от ChirpStack.")
        print("Проверьте HTTP-интеграцию ChirpStack → POST /api/uplink.")
        return

    # Сводка по классам и формам
    by_class = defaultdict(int)
    by_shape = defaultdict(int)
    valid = []
    per_dev = defaultdict(list)
    for r in rows:
        cls = classify(r.raw_hex)
        by_class[cls] += 1
        by_shape[(r.fport, len(r.raw_hex) // 2)] += 1
        per_dev[r.dev_eui].append(r)
        if cls.startswith("VALID"):
            valid.append(r)

    print(f"Всего пакетов: {len(rows)}  |  скважин: {len(per_dev)}\n")
    print("По классам:")
    for cls, n in sorted(by_class.items(), key=lambda x: -x[1]):
        print(f"  {n:6d}  {cls}")
    print("\nПо форме (fport, длина в байтах):")
    for (fp, ln), n in sorted(by_shape.items(), key=lambda x: -x[1]):
        print(f"  {n:6d}  FPort {fp}, {ln} байт")

    print(f"\n=== ВАЛИДНЫЕ пакеты — калибровать по ним ({len(valid)}) ===")
    for r in valid[:args.limit]:
        print(f"  {r.received_at:%Y-%m-%d %H:%M}  {r.dev_eui}  "
              f"FPort {r.fport} FCnt {r.fcnt}  {r.raw_hex}")
    if not valid:
        print("  Пока нет — все пакеты «мёртвые». Дождитесь трафика с живых скважин.")

    # Динамика по скважинам с несколькими валидными пакетами
    print("\n=== Динамика (скважины с ≥2 валидными пакетами) ===")
    shown = 0
    for dev, drows in per_dev.items():
        vrows = [r for r in drows if classify(r.raw_hex).startswith("VALID")]
        if len(vrows) < 2:
            continue
        shown += 1
        print(f"  {dev} ({len(vrows)} валидных):")
        for r in vrows[-4:]:
            print(f"    FCnt {r.fcnt:>4}  {r.raw_hex}")
    if not shown:
        print("  Пока нет скважины с ≥2 валидными пакетами.")


if __name__ == "__main__":
    main()
