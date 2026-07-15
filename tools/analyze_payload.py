#!/usr/bin/env python3
"""Анализатор полезной нагрузки терминалов — инструмент обратной разработки
формата пакета (расходомеры СКЖ/SKG, NuFlo MC-II/MC2, данные ЧРП/VFD).

ChirpStack настроен без кодека (Payload codec = None), поэтому декодирование
выполняется на нашей стороне. Формат бинарного пакета документально не задан,
поэтому восстанавливается по сопоставленным парам «сырые байты ↔ известные
значения» (дебит, накопленный расход, давление, температура), которые видны
в существующем портале для той же скважины в тот же момент.

Использование:

    # 1) Просто разложить пакет на все возможные интерпретации:
    python tools/analyze_payload.py 0D20010007E5128B73

    # 2) Найти смещение и масштаб под известное значение (напр. дебит 12.5):
    python tools/analyze_payload.py 0D20010007E5128B73 --find 12.5

    # 3) Сравнить несколько пакетов одного типа (ищем постоянные/переменные байты):
    python tools/analyze_payload.py --diff 0D20010007E5128B73 0D2001000A11...

Каждое найденное значение печатается со смещением, типом, порядком байт и
масштабом — этого достаточно, чтобы прописать поле в app/decoder.py.
"""
import argparse
import base64
import binascii
import struct


def _u(s: str) -> bytes:
    """Принять hex-строку ИЛИ base64 (поле data из ChirpStack DEVICE DATA)."""
    s = s.strip()
    cleaned = s.replace(" ", "").replace("0x", "")
    # похоже на чистый hex?
    if all(c in "0123456789abcdefABCDEF" for c in cleaned) and len(cleaned) % 2 == 0:
        return bytes.fromhex(cleaned)
    try:
        return base64.b64decode(s)
    except (binascii.Error, ValueError):
        return bytes.fromhex(cleaned)


# Числовые форматы struct: (метка, код, размер)
FORMATS = [
    ("uint8",   "B", 1),
    ("int8",    "b", 1),
    ("uint16",  "H", 2),
    ("int16",   "h", 2),
    ("uint32",  "I", 4),
    ("int32",   "i", 4),
    ("float32", "f", 4),
]
ENDIANS = [("LE", "<"), ("BE", ">")]

# Типичные масштабы: значения датчиков часто передаются как целые ×10, ×100 и т.п.
SCALES = [1, 0.1, 0.01, 0.001, 10, 100, 1000]


def interpretations(raw: bytes):
    """Все разумные числовые интерпретации: (offset, label, endian, value)."""
    out = []
    for label, code, size in FORMATS:
        for ename, eprefix in ENDIANS:
            if size == 1 and ename == "BE":
                continue  # для 1 байта порядок не важен
            for off in range(0, len(raw) - size + 1):
                try:
                    val = struct.unpack_from(eprefix + code, raw, off)[0]
                except struct.error:
                    continue
                out.append((off, label, ename, val))
    return out


def dump(raw: bytes) -> None:
    print(f"\nПакет: {raw.hex().upper()}  ({len(raw)} байт)")
    print("Смещения:", " ".join(f"{i:>2}" for i in range(len(raw))))
    print("Байты   :", " ".join(f"{x:02X}" for x in raw))
    print()
    for off, label, ename, val in interpretations(raw):
        if isinstance(val, float):
            if val != val or abs(val) > 1e12 or (0 < abs(val) < 1e-6):
                continue  # NaN/inf/денормал — не физичное значение
            print(f"  off {off:>2}  {label:<8} {ename}  = {val:.4f}")
        else:
            print(f"  off {off:>2}  {label:<8} {ename}  = {val}")


def find(raw: bytes, target: float, tol: float = 0.05) -> None:
    print(f"\nПоиск значения ≈ {target} в {raw.hex().upper()} "
          f"(допуск {tol*100:.0f}%):")
    hits = 0
    for off, label, ename, val in interpretations(raw):
        if not isinstance(val, (int, float)):
            continue
        for scale in SCALES:
            scaled = val * scale
            if target == 0:
                match = abs(scaled) < 1e-6
            else:
                match = abs(scaled - target) <= abs(target) * tol
            if match:
                s = "" if scale == 1 else f" ×{scale}"
                print(f"  off {off:>2}  {label:<8} {ename}  raw={val}{s}  → {scaled:.4f}")
                hits += 1
    if not hits:
        print("  — совпадений не найдено; проверьте значение или тип пакета")


def diff(hexes: list[str]) -> None:
    raws = [_u(h) for h in hexes]
    n = min(len(r) for r in raws)
    print(f"\nСравнение {len(raws)} пакетов (по {n} общих байт):")
    print("off | " + " | ".join(f"пакет{i+1}" for i in range(len(raws))) + " | статус")
    for off in range(n):
        col = [r[off] for r in raws]
        status = "const" if len(set(col)) == 1 else "VAR"
        print(f"{off:>3} | " + " | ".join(f"  {c:02X}  " for c in col) + f" | {status}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("payload", nargs="*", help="hex-пакет(ы)")
    ap.add_argument("--find", type=float, help="искать известное значение")
    ap.add_argument("--diff", nargs="+", help="сравнить несколько пакетов побайтно")
    args = ap.parse_args()

    if args.diff:
        diff([_u(x).hex() for x in args.diff])
        return
    if not args.payload:
        ap.error("укажите hex- или base64-пакет")
    raw = _u(args.payload[0])
    if args.find is not None:
        find(raw, args.find)
    else:
        dump(raw)


if __name__ == "__main__":
    import sys
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)  # вывод оборван (напр. | head) — это не ошибка
