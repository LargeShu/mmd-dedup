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
    python3 04_apply.py --undo "/Volumes/photo/_MMD_quarantine/2026-08-10_18-30"

Перед каждым файлом выполняются проверки, и при любой неудаче файл
пропускается, а не «пробуется всё равно»:

  * файл существует и это обычный файл
  * его размер совпадает с записанным в решении
  * оригинал, ради которого удаляется дубль, СУЩЕСТВУЕТ и читается
  * оригинал сам не отмечен к удалению
  * файл лежит внутри одного из дисков из config.json
  * при --verify-hash содержимое дубля и оригинала совпадает побитово

КАРАНТИН СОЗДАЁТСЯ НА ТОМ ЖЕ ДИСКЕ, где лежит файл:
<корень диска>/_MMD_quarantine/<дата-время>. Перенос внутри одной файловой
системы — переименование: мгновенное и атомарное. Перенос между дисками —
копирование, и для сетевого хранилища это часы трафика вместо секунд.

Журнал пишется в карантин рядом с файлами: по нему работает --undo и видно,
что произошло. Журналов столько, сколько задействовано дисков, поэтому
--undo принимает и папку карантина целиком.
"""

import argparse
import csv
import hashlib
import json
import os
import shutil
import stat
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mmdlib as L  # noqa: E402

JOURNAL = "journal.csv"
# Каталог карантина на самом диске. Имя латиницей и с подчёркиванием:
# так он всплывает в начале списка в Finder и не спорит с кодировками
# сетевых томов.
QUARANTINE_DIRNAME = "_MMD_quarantine"

# Начался ли перенос. Нужен для честного сообщения при Ctrl+C: обработчик
# писал «уже перенесённые файлы остались в карантине» даже в предпросмотре,
# где не тронут ни один файл. Пугать пользователя тем, чего не было, — тот же
# класс дефекта, что и подсказка, обещающая не то действие.
ПЕРЕНОС_НАЧАТ = False
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


def inside_any(p, real_roots):
    """Файл обязан лежать внутри настроенных дисков — защита от опечаток."""
    return any(p == r or p.startswith(r + os.sep) for r in real_roots)


class РеальныеПути:
    """
    realpath с памятью.

    На сетевом хранилище каждый realpath — обращение по сети, а в решениях
    один и тот же оригинал повторяется десятками строк. Без запоминания
    предпросмотр 2 700 решений уходил в минуты тишины: файлы не трогаются,
    но каждый проверяется полудюжиной сетевых вызовов.
    """

    def __init__(self):
        self._m = {}

    def __call__(self, p):
        r = self._m.get(p)
        if r is None:
            r = self._m[p] = os.path.realpath(p)
        return r


def preflight(rows, roots, verify_hash=False, quiet=False):
    """
    Проверяет каждое решение. Возвращает (годные, отклонённые).

    Отклонение — не ошибка выполнения, а причина не трогать файл.
    """
    real = РеальныеПути()
    real_roots = [real(r) for r in roots]
    к_удалению = {real(r["путь"]) for r in rows}
    годные, отклонённые = [], []

    # Проверка молчала минутами: пользователь видел «решений в файле: 2 690»
    # и пустой экран. Работа идёт по сети, её темп неочевиден — значит,
    # о ней надо сообщать.
    всего = len(rows)
    t0 = послед = time.time()
    if not quiet:
        print(f"проверка {всего:,} решений…", flush=True)

    for i, r in enumerate(rows, 1):
        путь = r["путь"]
        оригинал = r.get("оставить")
        причина = None

        # Одна stat вместо isfile + getsize: на сетевом диске это два
        # обращения вместо одного, и на трёх тысячах файлов разница видна.
        try:
            st = os.stat(путь)
        except OSError:
            st = None

        if not inside_any(real(путь), real_roots):
            причина = "вне настроенных дисков"
        elif st is None or not stat.S_ISREG(st.st_mode):
            причина = "файла нет или это не обычный файл"
        elif r.get("байт") is not None and st.st_size != r["байт"]:
            причина = (f"размер изменился: было {r['байт']:,}, "
                       f"стало {st.st_size:,}")
        elif not оригинал:
            причина = "не указан оригинал, который остаётся"
        elif not os.path.isfile(оригинал):
            причина = "ОРИГИНАЛ НЕ НАЙДЕН — удалять дубль нельзя"
        elif real(оригинал) in к_удалению:
            причина = "ОРИГИНАЛ ТОЖЕ ОТМЕЧЕН — удалили бы все копии"
        elif real(оригинал) == real(путь):
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

        сейчас = time.time()
        if not quiet and сейчас - послед >= 3 and i < всего:
            послед = сейчас
            темп = i / max(сейчас - t0, 0.001)
            осталось = (всего - i) / темп if темп else 0
            print(f"  проверено {i:,}/{всего:,} · {темп:.0f}/с · "
                  f"осталось ~{L.dur(осталось)}", flush=True)

    if not quiet:
        print(f"  проверено {всего:,} за {L.dur(time.time() - t0)}", flush=True)
    return годные, отклонённые


def drive_of(path, drives):
    """Имя диска из config.json, которому принадлежит файл, и его корень."""
    real = os.path.realpath(path)
    for имя, корень in drives.items():
        rr = os.path.realpath(корень)
        if real == rr or real.startswith(rr + os.sep):
            return имя, rr
    return None, None


def quarantine_bases(drives, arg, stamp):
    """
    Карантин по умолчанию — НА ТОМ ЖЕ ДИСКЕ, где лежит файл.

    Причина не в удобстве. Перемещение внутри одной файловой системы —
    переименование: мгновенное и атомарное, файл в любой момент существует
    либо там, либо там. Перемещение между дисками — копирование с последующим
    удалением: для сетевого хранилища это часы трафика, а обрыв посреди
    копирования оставляет файл в неопределённом состоянии.

    На архиве в 70 ГБ разница между этими двумя вариантами — секунды против
    часов, и целостность против надежды.

    --quarantine ПАПКА сохраняет старое поведение: всё в один каталог. Это
    осмысленно, когда карантин намеренно уносят на другой носитель.
    """
    if arg:
        return {имя: os.path.join(arg, имя) for имя in drives}
    return {имя: os.path.join(корень, QUARANTINE_DIRNAME, stamp)
            for имя, корень in drives.items()}


def same_device(src, dst):
    """
    Один ли том. Сравниваем st_dev ближайшего существующего предка
    назначения: самого каталога карантина ещё нет.
    """
    предок = os.path.dirname(os.path.abspath(dst))
    while предок and not os.path.exists(предок):
        выше = os.path.dirname(предок)
        if выше == предок:
            break
        предок = выше
    try:
        return os.stat(src).st_dev == os.stat(предок).st_dev
    except OSError:
        return False


def quarantine_path(base, src, корень):
    """
    Внутри карантина сохраняется структура диска: <карантин>/<путь/внутри>.

    Плоская свалка сделала бы возврат невозможным и склеила бы одноимённые
    файлы из разных папок.
    """
    real = os.path.realpath(src)
    return os.path.join(base, os.path.relpath(real, корень))


def apply_move(годные, bases, drives, verify_hash, quiet=False):
    """
    Переносит файлы, по журналу на каждый диск.

    Журнал лежит рядом с самим карантином намеренно: карантин и запись о том,
    откуда что взято, должны переезжать и удаляться вместе. Журнал в проекте
    отдельно от файлов на NAS — верный способ однажды остаться с папкой
    непонятного содержимого.
    """
    global ПЕРЕНОС_НАЧАТ
    ПЕРЕНОС_НАЧАТ = True
    журналы = {}
    файлы = {}
    перенесено = ошибок = 0
    байт = 0
    t0 = последний_отчёт = time.time()

    try:
        for i, r in enumerate(годные, 1):
            src = r["путь"]
            имя_диска, корень = drive_of(src, drives)
            if имя_диска is None:          # preflight такое уже отсеял
                ошибок += 1
                print(f"  ! вне настроенных дисков, пропуск: {src}")
                continue
            base = bases[имя_диска]
            if имя_диска not in журналы:
                os.makedirs(base, exist_ok=True)
                jpath = os.path.join(base, JOURNAL)
                новый = not os.path.exists(jpath)
                # ruff: контекстный менеджер тут не годится — журналов
                # столько, сколько дисков, и живут они до конца цикла.
                # Закрываются в finally ниже.
                jf = open(jpath, "a", newline="",  # noqa: SIM115
                          encoding="utf-8-sig")
                файлы[имя_диска] = jf
                журналы[имя_диска] = (csv.writer(jf, delimiter=";"), jpath)
                if новый:
                    журналы[имя_диска][0].writerow(JOURNAL_COLS)
            w, jpath = журналы[имя_диска]
            jf = файлы[имя_диска]

            dst = quarantine_path(base, src, корень)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):
                    без_расш, расш = os.path.splitext(dst)
                    dst = f"{без_расш}__{r.get('id', i)}{расш}"
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
            # Прогресс по ВРЕМЕНИ, а не по числу файлов.
            #
            # «Каждые 200 файлов» на быстром томе печатает простыню, а на
            # сетевом молчит минутами: шаг счётчика ничего не знает о том,
            # с какой скоростью идёт работа. Раз в 3 секунды — знает.
            сейчас = time.time()
            if not quiet and (сейчас - последний_отчёт >= 3) and i < len(годные):
                последний_отчёт = сейчас
                прошло = max(сейчас - t0, 0.001)
                вбс = байт / прошло
                осталось_файлов = len(годные) - i
                оценка = (осталось_файлов / (i / прошло)) if i else 0
                print(f"  перенесено {i:,}/{len(годные):,} · "
                      f"{L.human(байт)} · {L.human(вбс)}/с · "
                      f"осталось ~{L.dur(оценка)}", flush=True)
    finally:
        for jf in файлы.values():
            jf.close()

    пути = [jpath for _, jpath in журналы.values()]
    return перенесено, ошибок, байт, time.time() - t0, пути


def найти_журналы(путь):
    """
    --undo принимает и журнал, и папку карантина.

    Журналов теперь столько, сколько дисков: карантин у каждого свой.
    Требовать от человека перечислить их вручную — значит подталкивать
    к тому, чтобы один из них забыли, а файлы остались в карантине.
    """
    if os.path.isfile(путь):
        return [путь]
    if os.path.isdir(путь):
        найдено = []
        for корень, _, файлы in os.walk(путь):
            if JOURNAL in файлы:
                найдено.append(os.path.join(корень, JOURNAL))
        return sorted(найдено)
    return []


def undo(journal, quiet=False):
    журналы = найти_журналы(journal)
    if not журналы:
        raise SystemExit(f"журнал не найден: {journal}")
    if len(журналы) > 1:
        print(f"журналов найдено: {len(журналы)}")
        всего_в = всего_ош = 0
        for j in журналы:
            в, ош = undo(j, quiet)
            всего_в += в
            всего_ош += ош
        print(f"\nИТОГО возвращено {всего_в:,}, не удалось {всего_ош:,}")
        return всего_в, всего_ош
    journal = журналы[0]
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


def печать_плана(годные, отклонённые, bases, drives, verify_hash):
    байт = sum(r.get("байт") or 0 for r in годные)
    по_методам = {}
    по_дискам = {}
    for r in годные:
        по_методам[r.get("метод", "?")] = по_методам.get(r.get("метод", "?"), 0) + 1
        имя, _ = drive_of(r["путь"], drives)
        по_дискам[имя] = по_дискам.get(имя, 0) + 1

    print("\n" + "=" * 66)
    print("ПЛАН")
    print("=" * 66)
    print(f"  к переносу : {len(годные):,} файлов, {L.human(байт)}")
    for м, n in sorted(по_методам.items()):
        print(f"      {м:<8} {n:,}")
    print(f"  отклонено  : {len(отклонённые):,}")
    print(f"  сверка хешей: {'да' if verify_hash else 'нет (быстрее)'}")
    print("\n  КАРАНТИН:")
    for имя in sorted(по_дискам):
        if имя is None:
            continue
        base = bases[имя]
        свой = same_device(drives[имя], base)
        print(f"      {имя}: {base}")
        print(f"         {по_дискам[имя]:,} файлов · " +
              ("тот же том — перенос мгновенный"
               if свой else
               "ДРУГОЙ ТОМ — копирование, на сетевом диске это долго"))
    if any(имя is not None and not same_device(drives[имя], bases[имя])
           for имя in по_дискам):
        print("\n  Карантин на другом томе означает копирование каждого файла")
        print("  целиком. Для сетевого хранилища это часы вместо секунд,")
        print("  а обрыв связи посреди копирования оставляет файл")
        print("  в неопределённом состоянии. Уберите --quarantine, чтобы")
        print("  карантин создавался на самих дисках.")

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


def печать_итога(n, err, байт, сек, журналы, bases, drives, старт):
    """
    Итог прогона одним блоком.

    Раньше вывод заканчивался последней строкой прогресса, и понять
    постфактум — сколько перенесено, за сколько, куда и как вернуть —
    можно было только пролистав всю простыню вверх. Для операции, которая
    трогает файлы, это неправильно: итог должен быть виден сразу.
    """
    print("\n" + "=" * 66)
    print("ИТОГ")
    print("=" * 66)
    print(f"  перенесено   : {n:,} файлов, {L.human(байт)}")
    if err:
        print(f"  НЕ УДАЛОСЬ   : {err:,} — причины выше по строкам с «!»")
    else:
        print("  ошибок       : нет")
    скорость = байт / сек if сек > 0 else 0
    print(f"  время        : {L.dur(сек)}"
          + (f" · {L.human(скорость)}/с" if n else ""))
    print(f"  начало       : {time.strftime('%H:%M:%S', time.localtime(старт))}")
    print(f"  конец        : {time.strftime('%H:%M:%S')}")
    print(f"  всего с запуска: {L.dur(time.time() - старт)}")

    if журналы:
        print("\n  КАРАНТИН:")
        for j in журналы:
            каталог = os.path.dirname(j)
            имя = next((имя for имя, b in bases.items()
                        if os.path.realpath(b) == os.path.realpath(каталог)),
                       "?")
            свой = имя in drives and same_device(drives[имя], каталог)
            размер = "переименование" if свой else "копирование между томами"
            print(f"      {каталог}")
            print(f"         диск {имя} · {размер}")


def main():
    ap = argparse.ArgumentParser(
        description="Исполнение решений: перенос отмеченных дублей в карантин.")
    L.add_version_arg(ap)
    ap.add_argument("--decisions", metavar="ФАЙЛ",
                    help="JSON из кнопки «Скачать решения» в decision.html")
    ap.add_argument("--quarantine", metavar="ПАПКА",
                    help="ОДИН каталог для всех дисков. По умолчанию карантин "
                         "создаётся НА КАЖДОМ ДИСКЕ: <корень>/"
                         + QUARANTINE_DIRNAME + "/<дата-время> — так перенос "
                         "остаётся переименованием, а не копированием по сети")
    ap.add_argument("--move", action="store_true",
                    help="выполнить перенос. Без этого флага — только план")
    ap.add_argument("--verify-hash", action="store_true",
                    help="перед переносом сверить содержимое дубля и оригинала "
                         "побитово. Медленно, но исключает ошибку")
    ap.add_argument("--undo", metavar="ЖУРНАЛ",
                    help="вернуть файлы обратно по журналу карантина")
    ap.add_argument("--root", action="append", metavar="ИМЯ=ПУТЬ",
                    help="переопределить диски из config.json")
    ap.add_argument("--skip-mount-check", action="store_true",
                    help="не проверять, подключены ли диски. Нужен разве что "
                         "для отладки: непримонтированная шара выглядит как "
                         "пустая локальная папка, и работа пойдёт мимо архива")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if a.undo:
        undo(a.undo, a.quiet)
        return
    if not a.decisions:
        ap.error("нужен --decisions ФАЙЛ (или --undo ЖУРНАЛ)")

    cfg = L.load_config(required=not a.root)
    drives = L.merge_roots(cfg["drives"], a.root)
    roots = list(drives.values())
    if not roots:
        raise SystemExit("не задано ни одного диска")

    старт = time.time()
    # Время старта в выводе — не украшение. Прогон на сетевом хранилище
    # длится часами, и вернувшись к терминалу через полдня, нужно понять,
    # сколько он уже работает, не гадая по времени файлов.
    print(f"{L.version_line()}")
    print(f"запуск: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Диски проверяются ДО чтения решений: если шара не примонтирована,
    # незачем тратить минуты на проверку 2 700 путей, которых нет.
    # Строго только при --move: предпросмотр ничего не трогает, и мешать
    # человеку посмотреть план из-за неподключённого диска незачем.
    print()
    if not L.сообщить_о_дисках(drives, строго=a.move) and not a.skip_mount_check:
        raise SystemExit(
            "\nПодключите тома и повторите. Проверка снимается флагом "
            "--skip-mount-check,\nно тогда карантин может уехать "
            "на системный диск.")

    d, rows = load_decisions(a.decisions)
    print(f"решений в файле: {len(rows):,}")
    print(f"отчёт: {d.get('отчёт', '?')}, сохранено {d.get('сохранено', '?')}")

    годные, отклонённые = preflight(rows, roots, a.verify_hash, a.quiet)

    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    bases = quarantine_bases(drives, a.quarantine, stamp)
    печать_плана(годные, отклонённые, bases, drives, a.verify_hash)

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
    n, err, байт, сек, журналы = apply_move(годные, bases, drives,
                                            a.verify_hash, a.quiet)
    печать_итога(n, err, байт, сек, журналы, bases, drives, старт)
    print("\nФайлы НЕ УДАЛЕНЫ, а лежат в карантине.")
    print("Проверьте, что ничего нужного не пропало, и удалите папку карантина")
    print("обычным способом. Вернуть обратно:")
    for j in журналы:
        print(f"  python3 04_apply.py --undo {os.path.dirname(j)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        if ПЕРЕНОС_НАЧАТ:
            print("\nпрервано; уже перенесённые файлы остались в карантине, "
                  "журнал цел")
        else:
            print("\nпрервано; НИ ОДИН ФАЙЛ НЕ ТРОНУТ — шла только проверка")
