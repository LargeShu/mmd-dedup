#!/usr/bin/env python3
"""
Этап 4 — исполнение решений. ЕДИНСТВЕННЫЙ скрипт, который меняет файлы.

Читает JSON, выгруженный кнопкой «Скачать решения» из decision.html, и
убирает отмеченные дубликаты.

ГЛАВНОЕ РЕШЕНИЕ: по умолчанию файлы НЕ УДАЛЯЮТСЯ, а ПЕРЕМЕЩАЮТСЯ в карантин
с сохранением структуры каталогов. Удаление необратимо, перемещение — нет.
Когда убедитесь, что ничего нужного не пропало, карантин удаляется вручную
обычным способом.

    # 1. Посмотреть план. Ничего не делает. Режим по умолчанию
    python3 04_apply.py --decisions ../reports/decisions_20260810.json

    # 2. Выполнить: перенести в карантин
    python3 04_apply.py --decisions ... --move

    # 3. Если что-то не так — вернуть всё обратно
    python3 04_apply.py --undo ../quarantine/2026-08-10_18-30/journal.csv

Перед каждым файлом выполняются проверки, и при любой неудаче файл
пропускается, а не «пробуется всё равно»:

  * файл существует и это обычный файл
  * его размер совпадает с записанным в решении
  * оригинал, ради которого удаляется дубль, СУЩЕСТВУЕТ и читается
  * оригинал сам не отмечен к удалению
  * файл лежит внутри одного из дисков из config.json
  * при --verify-hash содержимое дубля и оригинала совпадает побитово

Журнал пишется в карантин: по нему работает --undo и видно, что произошло.
"""

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mmdlib as L  # noqa: E402

JOURNAL = "journal.csv"
JOURNAL_COLS = ["время", "действие", "откуда", "куда", "байт", "sha256",
                "оригинал", "метод"]


def sha256(path, chunk=4 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def load_decisions(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError) as e:
        raise SystemExit(f"файл решений не читается: {e}") from e
    rows = d.get("к_удалению")
    if rows is None:
        raise SystemExit(
            "в файле нет раздела 'к_удалению'. Ожидается JSON, выгруженный "
            "кнопкой «Скачать решения» из decision.html")
    if not rows:
        raise SystemExit("в файле решений ничего не отмечено")
    return d, rows


def inside_any(path, roots):
    """Файл обязан лежать внутри настроенных дисков — защита от опечаток."""
    p = os.path.realpath(path)
    for r in roots:
        r = os.path.realpath(r)
        if p == r or p.startswith(r + os.sep):
            return True
    return False


def preflight(rows, roots, verify_hash=False):
    """
    Проверяет каждое решение. Возвращает (годные, отклонённые).

    Отклонение — не ошибка выполнения, а причина не трогать файл.
    """
    к_удалению = {os.path.realpath(r["путь"]) for r in rows}
    годные, отклонённые = [], []

    for r in rows:
        путь = r["путь"]
        оригинал = r.get("оставить")
        причина = None

        if not inside_any(путь, roots):
            причина = "вне настроенных дисков"
        elif not os.path.isfile(путь):
            причина = "файла нет или это не обычный файл"
        elif r.get("байт") is not None and os.path.getsize(путь) != r["байт"]:
            причина = (f"размер изменился: было {r['байт']:,}, "
                       f"стало {os.path.getsize(путь):,}")
        elif not оригинал:
            причина = "не указан оригинал, который остаётся"
        elif not os.path.isfile(оригинал):
            причина = "ОРИГИНАЛ НЕ НАЙДЕН — удалять дубль нельзя"
        elif os.path.realpath(оригинал) in к_удалению:
            причина = "ОРИГИНАЛ ТОЖЕ ОТМЕЧЕН — удалили бы все копии"
        elif os.path.realpath(оригинал) == os.path.realpath(путь):
            причина = "дубль и оригинал — один и тот же файл"
        else:
            try:
                with open(оригинал, "rb") as fh:
                    fh.read(1)
            except OSError as e:
                причина = f"оригинал не читается: {e.__class__.__name__}"

        if причина is None and verify_hash:
            try:
                if sha256(путь) != sha256(оригинал):
                    причина = "содержимое НЕ совпадает с оригиналом"
            except OSError as e:
                причина = f"не удалось прочитать: {e.__class__.__name__}"

        (отклонённые if причина else годные).append(
            {**r, "причина": причина} if причина else r)

    return годные, отклонённые


def quarantine_path(base, src, roots):
    """
    Внутри карантина сохраняется структура: <карантин>/<диск>/<путь/внутри>.

    Плоская свалка сделала бы возврат невозможным и склеила бы одноимённые
    файлы из разных папок.
    """
    real = os.path.realpath(src)
    for r in roots:
        rr = os.path.realpath(r)
        if real == rr or real.startswith(rr + os.sep):
            rel = os.path.relpath(real, rr)
            return os.path.join(base, os.path.basename(rr.rstrip(os.sep)), rel)
    return os.path.join(base, "прочее", os.path.basename(real))


def apply_move(годные, base, roots, verify_hash, quiet=False):
    os.makedirs(base, exist_ok=True)
    jpath = os.path.join(base, JOURNAL)
    новый = not os.path.exists(jpath)
    перенесено = ошибок = 0
    байт = 0
    t0 = time.time()

    with open(jpath, "a", newline="", encoding="utf-8-sig") as jf:
        w = csv.writer(jf, delimiter=";")
        if новый:
            w.writerow(JOURNAL_COLS)
        for i, r in enumerate(годные, 1):
            src = r["путь"]
            dst = quarantine_path(base, src, roots)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):
                    корень, расш = os.path.splitext(dst)
                    dst = f"{корень}__{r.get('id', i)}{расш}"
                h = sha256(src) if verify_hash else ""
                размер = os.path.getsize(src)
                shutil.move(src, dst)
                w.writerow(L.csv_row([
                    time.strftime("%Y-%m-%d %H:%M:%S"), "перенесён",
                    src, dst, размер, h, r.get("оставить", ""),
                    r.get("метод", "")]))
                jf.flush()
                перенесено += 1
                байт += размер
            except OSError as e:
                ошибок += 1
                print(f"  ! не удалось перенести {src}: {e}")
            if not quiet and i % 200 == 0:
                print(f"  перенесено {i:,}/{len(годные):,}", flush=True)

    return перенесено, ошибок, байт, time.time() - t0, jpath


def undo(journal, quiet=False):
    if not os.path.isfile(journal):
        raise SystemExit(f"журнал не найден: {journal}")
    with open(journal, encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh, delimiter=";")
                if r.get("действие") == "перенесён"]
    if not rows:
        raise SystemExit("в журнале нет перенесённых файлов")

    print(f"возврат {len(rows):,} файлов по журналу {journal}")
    вернулось = ошибок = 0
    for r in rows:
        откуда, куда = r["куда"], r["откуда"]
        try:
            if not os.path.isfile(откуда):
                print(f"  ! в карантине нет: {откуда}")
                ошибок += 1
                continue
            if os.path.exists(куда):
                print(f"  ! на месте уже что-то есть, пропуск: {куда}")
                ошибок += 1
                continue
            os.makedirs(os.path.dirname(куда), exist_ok=True)
            shutil.move(откуда, куда)
            вернулось += 1
        except OSError as e:
            ошибок += 1
            print(f"  ! {откуда}: {e}")
    print(f"\nвозвращено {вернулось:,}, не удалось {ошибок:,}")
    if вернулось:
        print("журнал оставлен на месте: он фиксирует, что происходило")
    return вернулось, ошибок


def печать_плана(годные, отклонённые, base, verify_hash):
    байт = sum(r.get("байт") or 0 for r in годные)
    по_методам = {}
    for r in годные:
        по_методам[r.get("метод", "?")] = по_методам.get(r.get("метод", "?"), 0) + 1

    print("\n" + "=" * 66)
    print("ПЛАН")
    print("=" * 66)
    print(f"  к переносу : {len(годные):,} файлов, {L.human(байт)}")
    for м, n in sorted(по_методам.items()):
        print(f"      {м:<8} {n:,}")
    print(f"  отклонено  : {len(отклонённые):,}")
    print(f"  карантин   : {base}")
    print(f"  сверка хешей: {'да' if verify_hash else 'нет (быстрее)'}")

    if отклонённые:
        причины = {}
        for r in отклонённые:
            причины[r["причина"]] = причины.get(r["причина"], 0) + 1
        print("\n  ПОЧЕМУ ОТКЛОНЕНО:")
        for п, n in sorted(причины.items(), key=lambda kv: -kv[1]):
            print(f"      {n:>6,}  {п}")
        опасные = [r for r in отклонённые if "ОРИГИНАЛ" in r["причина"]]
        if опасные:
            print(f"\n  Из них {len(опасные):,} — случаи, где удаление привело бы")
            print("  к потере единственной копии. Разберитесь с ними отдельно.")
            for r in опасные[:5]:
                print(f"      {r['путь']}")

    print("\n  Первые записи к переносу:")
    for r in годные[:5]:
        print(f"      {r['путь']}")
        print(f"        остаётся: {r.get('оставить', '?')}")


def main():
    ap = argparse.ArgumentParser(
        description="Исполнение решений: перенос отмеченных дублей в карантин.")
    L.add_version_arg(ap)
    ap.add_argument("--decisions", metavar="ФАЙЛ",
                    help="JSON из кнопки «Скачать решения» в decision.html")
    ap.add_argument("--quarantine", metavar="ПАПКА",
                    help="куда переносить; по умолчанию "
                         "<проект>/quarantine/<дата-время>")
    ap.add_argument("--move", action="store_true",
                    help="выполнить перенос. Без этого флага — только план")
    ap.add_argument("--verify-hash", action="store_true",
                    help="перед переносом сверить содержимое дубля и оригинала "
                         "побитово. Медленно, но исключает ошибку")
    ap.add_argument("--undo", metavar="ЖУРНАЛ",
                    help="вернуть файлы обратно по журналу карантина")
    ap.add_argument("--root", action="append", metavar="ИМЯ=ПУТЬ",
                    help="переопределить диски из config.json")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if a.undo:
        undo(a.undo, a.quiet)
        return
    if not a.decisions:
        ap.error("нужен --decisions ФАЙЛ (или --undo ЖУРНАЛ)")

    cfg = L.load_config(required=not a.root)
    roots = list(L.merge_roots(cfg["drives"], a.root).values())
    if not roots:
        raise SystemExit("не задано ни одного диска")

    d, rows = load_decisions(a.decisions)
    print(f"решений в файле: {len(rows):,}")
    print(f"отчёт: {d.get('отчёт', '?')}, сохранено {d.get('сохранено', '?')}")

    годные, отклонённые = preflight(rows, roots, a.verify_hash)

    base = a.quarantine or os.path.join(
        L.project_dir(), "quarantine", time.strftime("%Y-%m-%d_%H-%M-%S"))
    печать_плана(годные, отклонённые, base, a.verify_hash)

    if not a.move:
        print("\n" + "-" * 66)
        print("Это был предпросмотр: НИ ОДИН ФАЙЛ НЕ ТРОНУТ.")
        print("Выполнить перенос:  добавьте --move")
        print("Надёжнее, но дольше: --move --verify-hash")
        return

    if not годные:
        print("\nпереносить нечего")
        return

    print("\n" + "-" * 66)
    print(f"ПЕРЕНОС {len(годные):,} файлов в карантин…")
    n, err, байт, сек, jpath = apply_move(годные, base, roots, a.verify_hash,
                                          a.quiet)
    print(f"\nперенесено {n:,} файлов, {L.human(байт)}, за {L.dur(сек)}")
    if err:
        print(f"не удалось перенести: {err:,}")
    print(f"журнал: {jpath}")
    print("\nФайлы НЕ УДАЛЕНЫ, а лежат в карантине.")
    print("Проверьте, что ничего нужного не пропало, и удалите папку карантина")
    print("обычным способом. Вернуть обратно:")
    print(f"  python3 04_apply.py --undo {jpath}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nпрервано; уже перенесённые файлы остались в карантине, "
              "журнал цел")
