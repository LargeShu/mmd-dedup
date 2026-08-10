#!/usr/bin/env python3
"""
Этап 1 — полный обход обоих дисков, ТОЛЬКО метаданные.

Читает исключительно результат os.scandir()/lstat(): имя, размер, mtime.
Содержимое файлов НЕ открывается. Ничего не пишется на сетевые диски.

Обход resumable: прогресс по каталогам пишется в БД, повторный запуск
продолжает с места остановки. Прерывать Ctrl+C безопасно.

Запуск (на macOS, локально — не через сетевую песочницу):

    python3 01_inventory.py
    python3 01_inventory.py --resume        # продолжить прерванный обход
    python3 01_inventory.py --rescan        # начать заново

По умолчанию база кладётся рядом со скриптами: ../data/inventory.db
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mmdlib as L  # noqa: E402

BATCH = 2000


def scan_drive(con, drive, root, resume, quiet=False, max_depth=0, run_ts=None):
    """
    Обход одного корня в ширину, только метаданные файловой системы.

    max_depth=0 — без ограничения (полная инвентаризация, режим по умолчанию).
    max_depth=N — не спускаться глубже N уровней от корня; всё, что осталось
    за границей, записывается в scan_errors как 'depth_limit', чтобы урезанный
    обход нельзя было принять за полный.
    """
    if not os.path.isdir(root):
        print(f"  !! корень недоступен, пропуск: {root}")
        con.execute(
            "INSERT OR REPLACE INTO scan_errors (drive, path, what, err) "
            "VALUES (?,?,?,?)", (drive, root, "dir", "корень недоступен"))
        con.commit()
        return 0, 0

    # reldir -> mtime каталога на момент прошлого обхода.
    # dmtime IS NULL (база от старой версии) => каталог перепроверяется.
    done = {}
    if resume:
        done = {
            r[0]: r[1]
            for r in con.execute(
                "SELECT reldir, dmtime FROM scanned_dirs WHERE drive=?", (drive,)
            )
            if r[1] is not None
        }
        if done:
            print(f"  инкрементально: в базе каталогов {len(done):,}, "
                  "будут перечитаны только изменившиеся")

    rows = []
    errs = []
    n_files = n_bytes = n_dirs = n_skip = n_changed = 0
    n_err_dir = n_err_file = n_link = n_cut = 0
    t0 = time.time()
    last = t0

    stack = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        reldir = os.path.relpath(cur, root)
        reldir = "" if reldir == "." else reldir

        try:
            entries = list(os.scandir(cur))
        except (PermissionError, OSError) as e:
            n_err_dir += 1
            errs.append((drive, cur, "dir", e.__class__.__name__))
            if not quiet:
                print(f"  ! нет доступа: {cur} ({e.__class__.__name__})")
            continue

        # Каталог пропускается, только если он уже обойдён И его собственный
        # mtime не изменился. mtime каталога меняется при добавлении, удалении
        # и переименовании файлов внутри — значит, новые фото в старой папке
        # не будут потеряны. Не ловится лишь правка файла на месте, что для
        # фотоархива нехарактерно.
        #
        # При пропуске не вызывается stat ни для одного файла — а это и есть
        # основная цена обхода на сетевом диске.
        try:
            cur_dmtime = os.stat(cur).st_mtime
        except OSError:
            cur_dmtime = None
        already = (reldir in done and cur_dmtime is not None
                   and abs(done[reldir] - cur_dmtime) < 0.001)
        if already:
            n_skip += 1
        elif reldir in done:
            n_changed += 1

        subdirs = []
        local = []
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if not L.is_junk_dir(e.name):
                        subdirs.append(e.path)
                    continue
                if e.is_symlink():
                    n_link += 1
                    errs.append((drive, e.path, "symlink", "пропущен"))
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue  # сокеты, устройства, FIFO
                if already:
                    continue  # см. комментарий выше: stat не вызываем
                st = e.stat(follow_symlinks=False)
            except OSError as ex:
                n_err_file += 1
                errs.append((drive, e.path, "file", ex.__class__.__name__))
                continue

            kind, ext = L.classify(e.name)
            relpath = os.path.join(reldir, e.name) if reldir else e.name
            local.append(
                (drive, relpath, reldir, e.name, ext, kind,
                 st.st_size, st.st_mtime, relpath.count(os.sep), run_ts)
            )
            if kind != "junk":
                n_bytes += st.st_size

        if max_depth and depth >= max_depth:
            for sd in subdirs:
                n_cut += 1
                errs.append((drive, sd, "depth_limit",
                             f"глубже -L {max_depth}"))
        else:
            stack.extend((sd, depth + 1) for sd in subdirs)
        n_dirs += 1

        if already:
            # Каталог не читали, но его файлы никуда не делись — метку обхода
            # обновляем, иначе --prune счёл бы их удалёнными. Это операция
            # в локальной базе, к сетевому диску обращений нет.
            con.execute(
                "UPDATE files SET seen=? WHERE drive=? AND reldir=?",
                (run_ts, drive, reldir))
            continue

        rows.extend(local)
        n_files += len(local)
        con.execute(
            "INSERT OR REPLACE INTO scanned_dirs (drive, reldir, nfiles, dmtime) "
            "VALUES (?,?,?,?)",
            (drive, reldir, len(local), cur_dmtime),
        )

        if len(rows) >= BATCH:
            flush(con, rows, errs)
            rows.clear()
            errs.clear()

        now = time.time()
        if not quiet and now - last > 5:
            rate = n_files / max(now - t0, 0.001)
            print(
                f"  {drive}: каталогов {n_dirs:,} | файлов {n_files:,} | "
                f"{L.human(n_bytes)} | {rate:,.0f} файл/с",
                flush=True,
            )
            last = now

    flush(con, rows, errs)
    dt = time.time() - t0
    print(f"  ГОТОВО {drive}: каталогов {n_dirs:,}"
          + (f" (пропущено без изменений: {n_skip:,}" if n_skip else "")
          + (f", перечитано изменившихся: {n_changed:,}" if n_skip and n_changed else "")
          + (")" if n_skip else "")
          + f", файлов {n_files:,}, {L.human(n_bytes)}, за {dt/60:.1f} мин")
    if n_err_dir or n_err_file or n_link or n_cut:
        print(f"  ВНИМАНИЕ {drive}: недоступных каталогов {n_err_dir}, "
              f"файлов {n_err_file}, симлинков пропущено {n_link}, "
              f"обрезано по глубине {n_cut}")
    return n_files, n_bytes


def flush(con, rows, errs=None):
    if rows:
        # ВАЖНО: именно ON CONFLICT DO UPDATE, а не INSERT OR REPLACE.
        # REPLACE удаляет старую строку и вставляет новую с НОВЫМ id, из-за чего
        # записи в sigs (хеши, EXIF, pHash — самая дорогая часть работы) молча
        # теряли привязку к файлам. DO UPDATE сохраняет id.
        con.executemany(
            """INSERT INTO files
               (drive, relpath, reldir, name, ext, kind, size, mtime, depth, seen)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(drive, relpath) DO UPDATE SET
                 reldir=excluded.reldir, name=excluded.name, ext=excluded.ext,
                 kind=excluded.kind, size=excluded.size, mtime=excluded.mtime,
                 depth=excluded.depth, seen=excluded.seen""",
            rows,
        )
    if errs:
        con.executemany(
            "INSERT OR REPLACE INTO scan_errors (drive, path, what, err) "
            "VALUES (?,?,?,?)", errs)
    if rows or errs:
        con.commit()


def summarize(con):
    """
    Всё считается ПО БАЗЕ, а не по счётчикам текущего запуска: при --resume
    счётчики сессии показывают только дозаписанное и вводят в заблуждение.
    """
    print("\n" + "=" * 68)
    print("СВОДКА ПО КЛАССАМ (по всей базе)")
    print("=" * 68)
    print(f"{'диск':<12}{'класс':<10}{'файлов':>12}{'объём':>16}")
    for drive, kind, n, b in con.execute(
        """SELECT drive, kind, COUNT(*), SUM(size) FROM files
           GROUP BY drive, kind ORDER BY drive, SUM(size) DESC"""
    ):
        print(f"{drive:<12}{kind:<10}{n:>12,}{L.human(b or 0):>16}")
    tot = con.execute(
        "SELECT COUNT(*), SUM(size) FROM files WHERE kind!='junk'"
    ).fetchone()
    print("-" * 68)
    print(f"{'ИТОГО':<22}{tot[0]:>12,}{L.human(tot[1] or 0):>16}")


def report_stale(con, drives, run_ts, a):
    """
    Записи о файлах, которых в этом обходе не встретилось: удалены,
    переименованы или перенесены с прошлого раза.

    Сама по себе устаревшая запись не опасна, но она попадёт в отчёт как
    существующий файл. Поэтому о ней сообщаем всегда, а удаляем только по
    явному --prune и только после честного полного обхода.
    """
    # ph — только строка из знаков "?", сами значения передаются параметрами.
    # Конкатенация здесь неизбежна: SQLite не умеет параметризовать список IN.
    ph = ",".join("?" * len(drives))
    stale = con.execute(
        f"SELECT COUNT(*) FROM files WHERE drive IN ({ph}) "  # noqa: S608
        f"AND (seen IS NULL OR seen < ?)", (*drives, run_ts)).fetchone()[0]
    if not stale:
        return

    full_pass = not a.max_depth
    print(f"\nУстаревших записей в базе: {stale:,} "
          "(файлы не встретились в этом обходе)")
    if not full_pass:
        why = "--resume" if a.resume else f"-L {a.max_depth}"
        print(f"  Обход был неполным ({why}), поэтому судить об удалении нельзя.")
        print("  Это ожидаемо и ничего не значит.")
        return
    if not a.prune:
        print("  Похоже, эти файлы удалены или перемещены с прошлого обхода.")
        print("  Они останутся в отчётах как существующие. Убрать: --prune")
        return
    con.execute(
        f"DELETE FROM sigs WHERE file_id IN (SELECT id FROM files "  # noqa: S608
        f"WHERE drive IN ({ph}) AND (seen IS NULL OR seen < ?))",
        (*drives, run_ts))
    con.execute(
        f"DELETE FROM files WHERE drive IN ({ph}) "  # noqa: S608
        f"AND (seen IS NULL OR seen < ?)", (*drives, run_ts))
    con.commit()
    print(f"  --prune: удалено записей из базы: {stale:,} "
          "(сами файлы на дисках не тронуты)")


def report_completeness(con):
    """
    Полнота обхода. Без этого блока урезанный или частично сбойный обход
    выглядел бы как полный: недостающие файлы просто не попали бы в инвентарь.
    """
    rows = list(con.execute(
        "SELECT what, COUNT(*) FROM scan_errors GROUP BY what ORDER BY 2 DESC"))
    if not rows:
        print("\nПолнота обхода: замечаний нет, инвентарь полный.")
        return
    names = {"dir": "каталогов не прочитано", "file": "файлов не прочитано",
             "symlink": "символических ссылок пропущено",
             "depth_limit": "ветвей обрезано ограничением -L"}
    print("\n" + "!" * 68)
    print("ИНВЕНТАРЬ НЕПОЛНЫЙ")
    print("!" * 68)
    for what, n in rows:
        print(f"  {names.get(what, what):<38}{n:>8,}")
    if any(w == "depth_limit" for w, _ in rows):
        print("\n  Это тестовый прогон с -L. Для боевого запуска уберите флаг.")
    print("  Подробности:  SELECT * FROM scan_errors;  либо колонка в CSV-отчёте")


def report_mtime_trust(con):
    """
    Насколько можно верить mtime.

    Урок из поиска оригинала: наличие метаданных не означает их правдивость.
    После массового копирования у всех файлов проставляется дата копирования,
    и распределение «по годам» начинает врать. Дешёвый признак такой папки —
    все файлы в ней имеют одинаковый mtime с точностью до минуты.
    """
    total, susp = con.execute(
        """WITH d AS (
             SELECT drive, reldir, COUNT(*) n,
                    COUNT(DISTINCT CAST(mtime/60 AS INT)) u
             FROM files WHERE kind!='junk' GROUP BY drive, reldir
           )
           SELECT COALESCE(SUM(n),0), COALESCE(SUM(CASE WHEN n>=5 AND u=1
                  THEN n ELSE 0 END),0) FROM d"""
    ).fetchone()
    if not total:
        return
    pct = 100.0 * susp / total
    print(f"\nДостоверность дат: {susp:,} из {total:,} файлов ({pct:.1f}%) лежат "
          "в папках,\nгде у всех файлов одинаковый mtime — признак массового "
          "копирования.")
    if pct >= 10:
        print("  Для таких файлов mtime — дата копирования, а не съёмки.")
        print("  Опирайтесь на EXIF (этап 2, --stage exif), а не на разбивку по годам.")


def write_csv(con, path):
    import csv

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Инвентаризация, этап 1 — метаданные"])
        w.writerow([f"Сформировано: {time.strftime('%Y-%m-%d %H:%M:%S')}"])
        w.writerow([f"Обход начат: {L.get_meta(con,'inventory_started','—')}"])
        w.writerow([f"Ограничение глубины: "
                    f"{L.get_meta(con,'max_depth','нет (полный обход)')}"])
        w.writerow(["Файлы не изменялись: диски открывались только на чтение"])
        w.writerow([])
        w.writerow(["диск", "папка", "файлов", "байт", "размер",
                    "фото", "видео", "старейший mtime", "новейший mtime",
                    "одинаковый mtime у всех"])
        for d, p, n, b, im, v, mn, mx, u in con.execute(
            """SELECT drive, reldir, COUNT(*), SUM(size),
                      SUM(kind IN ('image','raw')), SUM(kind='video'),
                      MIN(mtime), MAX(mtime),
                      COUNT(DISTINCT CAST(mtime/60 AS INT))
               FROM files WHERE kind!='junk'
               GROUP BY drive, reldir ORDER BY SUM(size) DESC"""
        ):
            w.writerow(L.csv_row([d, p or "(корень)", n, b or 0, L.human(b or 0),
                        im or 0, v or 0,
                        time.strftime("%Y-%m-%d", time.localtime(mn)),
                        time.strftime("%Y-%m-%d", time.localtime(mx)),
                        "да" if n >= 5 and u == 1 else ""]))
        w.writerow([])
        w.writerow(["НЕ ПРОЧИТАНО"])
        w.writerow(["диск", "путь", "что", "причина"])
        for r in con.execute("SELECT drive, path, what, err FROM scan_errors"):
            w.writerow(L.csv_row(r))
    return path


def main():
    ap = argparse.ArgumentParser()
    L.add_version_arg(ap)
    ap.add_argument("--db", default=None)
    ap.add_argument("--restat", action="store_true",
                    help="перечитать все каталоги, даже неизменившиеся "
                         "(прежнее поведение по умолчанию)")
    ap.add_argument("--rescan", action="store_true",
                    help="стереть данные по дискам и начать с нуля")
    ap.add_argument("--resume", action="store_true",
                    help=argparse.SUPPRESS)  # оставлен для совместимости
    ap.add_argument("--drive", help="обойти только один диск (по имени из config.json)")
    ap.add_argument("--root", action="append", metavar="ИМЯ=ПУТЬ",
                    help="переопределить/добавить корень")
    ap.add_argument("-L", "--max-depth", type=int, default=0, metavar="1..20",
                    help="ограничить глубину вложенности папок (для тестового "
                         "прогона). По умолчанию 0 — полная инвентаризация")
    ap.add_argument("--csv", metavar="ПУТЬ",
                    help="куда сохранить сводку; по умолчанию "
                         "../reports/inventory_<время>.csv")
    ap.add_argument("--no-csv", action="store_true")
    ap.add_argument("--prune", action="store_true",
                    help="убрать из БАЗЫ записи о файлах, которых больше нет "
                         "на дисках (сами файлы не трогаются)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if a.max_depth and not 1 <= a.max_depth <= 20:
        ap.error("-L принимает значение от 1 до 20; 0 или без флага — полный обход")

    here = os.path.dirname(os.path.abspath(__file__))
    db = a.db or os.path.join(here, "..", "data", "inventory.db")
    os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)

    # Пути живут только в config.json; --root дополняет и переопределяет
    cfg = L.load_config(required=not a.root)
    drives = L.merge_roots(cfg["drives"], a.root)
    if not drives:
        raise SystemExit("не задано ни одного диска: заполните config.json "
                         "или укажите --root ИМЯ=ПУТЬ")
    if a.drive:
        if a.drive not in drives:
            raise SystemExit(f"диск '{a.drive}' не найден. Есть: "
                             f"{', '.join(drives)}")
        drives = {a.drive: drives[a.drive]}

    con = L.connect(db)
    L.init_db(con)

    if a.rescan:
        for d in drives:
            con.execute("DELETE FROM files WHERE drive=?", (d,))
            con.execute("DELETE FROM scanned_dirs WHERE drive=?", (d,))
        con.commit()
        print("предыдущие данные по указанным дискам очищены")

    # scan_errors чистится ВСЕГДА, в том числе при --resume.
    #
    # Причина: обход заново проходит по всем каталогам (даже уже обойдённым —
    # чтобы найти их подкаталоги), поэтому недоступные каталоги, симлинки и
    # обрезанные ветви заново обнаруживаются на каждом запуске. Если не
    # чистить, записи от прошлого прогона остаются: после -L 2 → -L 3 отчёт
    # продолжал утверждать, что ветви обрезаны, хотя они уже обойдены.
    for d in drives:
        con.execute("DELETE FROM scan_errors WHERE drive=?", (d,))
    con.commit()

    # Инкрементальность — режим по умолчанию. Каталог перечитывается, только
    # если изменился его mtime; неизменившиеся пропускаются без единого stat.
    # Раньше по умолчанию всё перечитывалось заново, и лестница -L 2 -> -L 3
    # стоила полного повторного обхода.
    incremental = not a.restat and not a.rescan
    if a.resume:
        print("(--resume больше не нужен: инкрементальный режим включён "
              "по умолчанию)")

    print(f"база: {os.path.abspath(db)}")
    if a.max_depth:
        print(f"ТЕСТОВЫЙ ПРОГОН: глубина ограничена -L {a.max_depth}; "
              "инвентарь будет неполным")
    L.set_meta(con, "inventory_started", time.strftime("%Y-%m-%d %H:%M:%S"))
    L.set_meta(con, "max_depth", a.max_depth or "нет (полный обход)")
    con.commit()

    t0 = time.time()
    run_ts = t0
    for name, root in drives.items():
        print(f"\n--- обход {name}: {root}")
        L.set_meta(con, f"root_{name}", root)
        scan_drive(con, name, root, resume=incremental,
                   quiet=a.quiet, max_depth=a.max_depth, run_ts=run_ts)

    report_stale(con, drives, run_ts, a)

    L.set_meta(con, "inventory_finished", time.strftime("%Y-%m-%d %H:%M:%S"))
    con.commit()
    summarize(con)
    report_mtime_trust(con)
    report_completeness(con)
    dt = time.time() - t0
    total_files = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    rate = total_files / max(dt, 0.001)
    print(f"\nвсего {dt/60:.1f} мин, скорость {rate:,.0f} файл/с")

    if a.max_depth:
        cut = con.execute(
            "SELECT COUNT(*) FROM scan_errors WHERE what='depth_limit'"
        ).fetchone()[0]
        avg = total_files / max(con.execute(
            "SELECT COUNT(*) FROM scanned_dirs").fetchone()[0], 1)
        print(f"\nЭто был тестовый прогон -L {a.max_depth}. Не просмотрено "
              f"ветвей: {cut:,}")
        if cut:
            print(f"  При средней населённости {avg:,.0f} файлов на каталог "
                  "и той же скорости")
            print(f"  полный обход займёт ориентировочно "
                  f"{dt/60 * (1 + cut*avg/max(total_files,1)):,.0f}–"
                  f"{dt/60 * (1 + 3*cut*avg/max(total_files,1)):,.0f} мин.")
            print("  Оценка грубая: вложенность и населённость папок неравномерны.")
            print(f"  Уточнить — прогнать -L {a.max_depth + 1} и сравнить рост.")

    if not a.no_csv:
        p = a.csv or os.path.join(
            here, "..", "reports",
            f"inventory_{time.strftime('%Y%m%d-%H%M%S')}.csv")
        write_csv(con, p)
        print(f"Сводка: {os.path.abspath(p)}")

    print("\nследующий шаг:  .venv/bin/python scripts/02_signatures.py --plan")
    con.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nпрервано. Повторный запуск с --resume продолжит с этого места.")
