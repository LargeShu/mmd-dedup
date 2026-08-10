#!/usr/bin/env python3
"""
Этап 0 — точечный поиск оригинала конкретного файла (например 1238.JPG).

Работает без предварительной инвентаризации: обходит диски сам.
Открывает файлы ТОЛЬКО на чтение. Ничего не пишет на сетевые диски.

Зависимости ставятся ТОЛЬКО в venv проекта, в системный Python — никогда:
    python3 -m venv .venv && .venv/bin/pip install -r ../requirements.txt
    .venv/bin/python 00_find_original.py ...

Без библиотек работает режим --no-content (поиск по имени и датам файлов).

Три режима поиска, применяются вместе:
  1) по имени      — все файлы, чьё имя содержит образец (1238)
  2) по содержимому — SHA-256 совпадение с эталоном (--ref), если он задан
  3) по картинке   — pHash, ловит ресайзы и пересохранения того же кадра

Затем кандидаты ранжируются по «оригинальности»:
    наличие EXIF DateTimeOriginal > разрешение > размер файла >
    более ранний mtime > меньшая глубина пути > не в служебных папках.

Примеры:

    # найти всё похожее на имя
    python3 00_find_original.py 1238.JPG

    # если есть копия на руках — самый надёжный вариант
    python3 00_find_original.py 1238.JPG --ref ~/Downloads/1238.JPG

    # искать только в одной папке
    python3 00_find_original.py 1238 --root "/путь/к/одной/папке"

    # известна дата съёмки — самый надёжный признак, ищет по EXIF ВСЕ файлы,
    # а не только совпавшие по имени
    python3 00_find_original.py 1238 --date "2008-12-24 13:24" --tolerance 120
"""

import argparse
import csv
import hashlib
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mmdlib as L  # noqa: E402



def sha256(path, limit=None):
    h = hashlib.sha256()
    got = 0
    with open(path, "rb") as f:
        while True:
            want = 4 << 20 if limit is None else min(4 << 20, limit - got)
            if want <= 0:
                break
            b = f.read(want)
            if not b:
                break
            h.update(b)
            got += len(b)
    return h.hexdigest()


# Потолок на декодирование: None снял бы защиту Pillow от decompression bomb.
MAX_PIXELS = 512_000_000


def open_image(path):
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    return Image.open(path)


def img_info(path):
    """(width, height, exif_dt, camera, phash) — читает только то, что доступно."""
    try:
        with open_image(path) as im:
            w, h = im.size
            ex = im.getexif() or {}
            dt = ex.get(36867) or ex.get(306)
            cam = " ".join(str(ex.get(t, "")).strip() for t in (271, 272)).strip()
            ph = None
            try:
                import imagehash

                im.draft("RGB", (256, 256))
                ph = str(imagehash.phash(im.convert("L")))
            except ImportError:
                pass
            return w, h, (str(dt).strip() if dt else None), (cam or None), ph
    except Exception:  # noqa: BLE001
        return None, None, None, None, None


def parse_dt(s):
    """Принимает '24.12.2008 13:24', '2008-12-24 13:24', '2008:12:24 13:24:00'."""
    s = str(s).strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except ValueError:
            continue
    raise ValueError(f"не понимаю дату: {s!r}")


def walk_cheap(roots, pattern, quiet, want_dt=None, tol=0):
    """
    ПРОХОД 1 — дешёвый. Ни один файл не открывается: только scandir + lstat.

    Ловит совпадение по имени и по mtime в окне вокруг искомой даты.
    Находки печатаются СРАЗУ, как только встретились, — на большой библиотеке
    ответ обычно появляется в первые секунды, задолго до конца обхода.

    Возвращает (hits, images, n) — где images это список изображений
    для возможного прохода 2.
    """
    rx = re.compile(pattern, re.I)
    hits = []
    images = []
    seen = set()
    n = 0
    t0 = time.time()
    last = t0
    for root in roots:
        if not os.path.isdir(root):
            print(f"  !! недоступен: {root}")
            continue
        stack = [root]
        while stack:
            cur = stack.pop()
            try:
                entries = list(os.scandir(cur))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if not L.is_junk_dir(e.name):
                            stack.append(e.path)
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                n += 1
                kind, _ext = L.classify(e.name)
                why_hit = None

                if rx.search(e.name):
                    why_hit = "имя"
                elif want_dt is not None and kind in ("image", "raw") \
                        and abs(st.st_mtime - want_dt) <= max(tol, 86400):
                    why_hit = "mtime"

                if why_hit and e.path not in seen:
                    seen.add(e.path)
                    hits.append((e.path, st.st_size, st.st_mtime, why_hit))
                    print(f"  [+] {why_hit}: {e.path}  ({L.human(st.st_size)})",
                          flush=True)
                elif want_dt is not None and kind in ("image", "raw"):
                    images.append((e.path, st.st_size, st.st_mtime))
            now = time.time()
            if not quiet and now - last > 5:
                print(f"  … просмотрено {n:,}, найдено {len(hits)}", flush=True)
                last = now
    print(f"  проход 1 завершён: {n:,} файлов за {(time.time()-t0)/60:.1f} мин, "
          f"найдено {len(hits)} (файлы не открывались)")
    return hits, images, n


def walk_exif(images, want_dt, tol, quiet, threads=8):
    """
    ПРОХОД 2 — дорогой, запускается только по необходимости.

    Открывает заголовок каждого изображения ради DateTimeOriginal. Нужен лишь
    тогда, когда файл переименован и от исходного имени ничего не осталось.
    Прерывание Ctrl+C безопасно: ничего не пишется.
    """
    import concurrent.futures as cf

    hits = []
    t0 = time.time()
    last = t0
    done = 0
    total = len(images)
    print(f"\n  проход 2: EXIF у {total:,} изображений "
          f"(долго; Ctrl+C безопасен)")
    ex = cf.ThreadPoolExecutor(max_workers=threads)
    try:
        futs = {ex.submit(exif_dt_only, p): (p, s, m) for p, s, m in images}
        for fut in cf.as_completed(futs):
            p, s, m = futs[fut]
            done += 1
            dt = fut.result()
            if dt is not None and abs(dt - want_dt) <= tol:
                hits.append((p, s, m, "EXIF"))
                print(f"  [+] EXIF: {p}  ({L.human(s)})", flush=True)
            now = time.time()
            if not quiet and now - last > 5:
                rate = done / max(now - t0, 0.001)
                print(f"  … {done:,}/{total:,} | {rate:.0f}/с | "
                      f"осталось ~{(total-done)/max(rate,0.001)/60:.0f} мин",
                      flush=True)
                last = now
    except KeyboardInterrupt:
        # прервали — не теряем то, что уже нашли: отдаём наверх для отчёта
        print(f"\n  проход 2 прерван на {done:,}/{total:,}; "
              f"найденное сохраняется в отчёт")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    print(f"  проход 2: найдено {len(hits)} за {(time.time()-t0)/60:.1f} мин")
    return hits


def exif_dt_only(path):
    """Быстрое чтение только DateTimeOriginal -> epoch, либо None."""
    try:
        with open_image(path) as im:
            ex = im.getexif() or {}
            v = ex.get(36867) or ex.get(306)
        if not v:
            return None
        return time.mktime(time.strptime(str(v).strip()[:19], "%Y:%m:%d %H:%M:%S"))
    except Exception:  # noqa: BLE001
        return None


def hamming(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


CSV_HEADER = [
    "№", "вердикт", "путь", "папка", "имя", "найден по",
    "байт", "размер", "ширина", "высота", "мегапикселей",
    "дата съёмки (EXIF)", "сверка с искомой датой", "камера",
    "изменён", "pHash", "sha256", "сверка с эталоном",
]


def csv_rows(cands, want_dt):
    rows = []
    for i, c in enumerate(cands, 1):
        if want_dt is None:
            verdict = "вероятный оригинал" if i == 1 else "кандидат"
        elif c["dt_match"] == "совпадает":
            verdict = "ПОДТВЕРЖДЁН ДАТОЙ" if i == 1 else "подтверждён датой"
        elif c["dt_match"] == "противоречит":
            verdict = "ОТКЛОНЁН: дата не та"
        else:
            verdict = "не подтверждён (нет EXIF)"
        mp = round((c["w"] or 0) * (c["h"] or 0) / 1e6, 1) or ""
        rows.append([
            i, verdict, c["path"],
            os.path.dirname(c["path"]), os.path.basename(c["path"]),
            c.get("hit_by", ""),
            c["size"], L.human(c["size"]),
            c["w"] or "", c["h"] or "", mp,
            c["exif_dt"] or "", c.get("dt_match", ""), c["cam"] or "",
            time.strftime("%Y-%m-%d %H:%M", time.localtime(c["mtime"])),
            c["phash"] or "", (c["sha"] or "")[:16],
            ", ".join(c["match"]),
        ])
    return rows


def write_csv(path, cands, want_dt, target, roots):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow([f"Поиск оригинала: {target}"])
        w.writerow([f"Искомая дата съёмки: "
                    f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(want_dt))}"
                    if want_dt else "Искомая дата съёмки: не задана"])
        w.writerow([f"Корни: {'; '.join(roots)}"])
        w.writerow([f"Отчёт сформирован: {time.strftime('%Y-%m-%d %H:%M:%S')}"])
        w.writerow(["Файлы не изменялись: диски открывались только на чтение"])
        w.writerow([])
        w.writerow(CSV_HEADER)
        w.writerows(L.csv_row(r) for r in csv_rows(cands, want_dt))
    return path


def dt_verdict(c, want_dt, tol):
    """
    Сверка даты съёмки кандидата с искомой. Три исхода:
      'совпадает'   — EXIF в пределах допуска
      'противоречит'— EXIF есть и он про другую дату: это НЕ наш кадр
      'нет данных'  — EXIF отсутствует, судить не по чему

    Ключевой момент: 'противоречит' хуже, чем 'нет данных'. Наличие EXIF само
    по себе не достоинство — важно, что именно в нём записано.
    """
    if want_dt is None:
        return "не задано"
    if not c["exif_dt"]:
        return "нет данных"
    try:
        got = time.mktime(time.strptime(str(c["exif_dt"]).strip()[:19],
                                        "%Y:%m:%d %H:%M:%S"))
    except ValueError:
        return "нет данных"
    return "совпадает" if abs(got - want_dt) <= tol else "противоречит"


def score(c):
    """
    Меньше — вероятнее оригинал. Основа общая с 03_report.py
    (см. L.originality_key), сверху добавлен признак, которого нет в отчёте:
    чем именно кандидат был найден.
    """
    base = L.originality_key(
        path=c["path"], size=c["size"], mtime=c["mtime"],
        width=c["w"], height=c["h"], exif_dt=c["exif_dt"],
        dt_match=c.get("dt_match", "не задано"),
    )
    # вклинивается сразу после сверки с датой: находка по EXIF надёжнее,
    # чем случайное совпадение куска имени
    return (base[0],
            {"EXIF": 0, "имя": 1, "mtime": 2}.get(c.get("hit_by"), 3)) + base[1:]


def main():
    ap = argparse.ArgumentParser()
    L.add_version_arg(ap)
    ap.add_argument("target", help="имя или его часть, например 1238.JPG или 1238")
    ap.add_argument("--ref", help="путь к эталонной копии для сверки по содержимому")
    ap.add_argument("--root", action="append", default=None)
    ap.add_argument("--date", help="известная дата съёмки, напр. '24.12.2008 13:24'")
    ap.add_argument("--tolerance", type=int, default=120,
                    help="допуск по дате в секундах (по умолчанию 120)")
    ap.add_argument("--deep", action="store_true",
                    help="принудительно делать проход 2 (EXIF всех изображений), "
                         "даже если проход 1 уже что-то нашёл")
    ap.add_argument("--threads", type=int, default=8,
                    help="параллельные чтения в проходе 2")
    ap.add_argument("--phash-distance", type=int, default=8)
    ap.add_argument("--no-content", action="store_true",
                    help="не читать содержимое, только имена и метаданные ФС")
    ap.add_argument("--csv", metavar="ПУТЬ",
                    help="куда сохранить отчёт; по умолчанию "
                         "../reports/find_<цель>_<время>.csv")
    ap.add_argument("--no-csv", action="store_true", help="не сохранять отчёт")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    # По умолчанию — диски из config.json; --root перекрывает
    roots = a.root or list(L.load_config()["drives"].values())
    if not roots:
        raise SystemExit("не задано ни одного каталога для поиска")
    stem = os.path.splitext(a.target)[0]
    pattern = re.escape(stem)

    if not a.no_content:
        L.venv_notice()

    want_dt = parse_dt(a.date) if a.date else None

    print(f"поиск '{a.target}' (шаблон имени: /{pattern}/i)")
    if want_dt:
        print(f"дата съёмки: {time.strftime('%Y-%m-%d %H:%M', time.localtime(want_dt))}"
              f" ±{a.tolerance} с")
    print(f"корни: {', '.join(roots)}")
    print("проход 1 — по именам и датам файлов, без открытия файлов; "
          "находки печатаются сразу\n")

    ref = None
    if a.ref:
        if not os.path.isfile(a.ref):
            print(f"эталон не найден: {a.ref}")
            return
        rw, rh, rdt, rcam, rph = img_info(a.ref)
        ref = {
            "path": a.ref, "sha": sha256(a.ref), "size": os.path.getsize(a.ref),
            "w": rw, "h": rh, "exif_dt": rdt, "cam": rcam, "phash": rph,
        }
        print(f"эталон: {a.ref}")
        print(f"  {L.human(ref['size'])}, {rw}x{rh}, EXIF {rdt or '—'}, sha {ref['sha'][:16]}…\n")

    hits, images, n_all = walk_cheap(roots, pattern, a.quiet, want_dt, a.tolerance)

    def build(hit_list):
        out = []
        for p, size, mtime, hit_by in hit_list:
            c = {"path": p, "size": size, "mtime": mtime, "hit_by": hit_by,
                 "w": None, "h": None, "exif_dt": None, "cam": None,
                 "phash": None, "sha": None, "match": [], "dt_match": "не задано"}
            if not a.no_content:
                c["w"], c["h"], c["exif_dt"], c["cam"], c["phash"] = img_info(p)
                c["dt_match"] = dt_verdict(c, want_dt, a.tolerance)
                if ref:
                    if size == ref["size"]:
                        c["sha"] = sha256(p)
                        if c["sha"] == ref["sha"]:
                            c["match"].append("ТОЧНАЯ КОПИЯ")
                    if not c["match"] and c["phash"] and ref["phash"]:
                        d = hamming(c["phash"], ref["phash"])
                        if d <= a.phash_distance:
                            c["match"].append(f"похожа (pHash d={d})")
            out.append(c)
        return out

    cands = build(hits)

    # Проход 2 нужен не тогда, когда проход 1 «ничего не нашёл», а тогда,
    # когда среди находок нет НИ ОДНОЙ, подтверждённой искомой датой.
    # Совпадение по имени само по себе ничего не доказывает: IMG_1238_2.JPG
    # с EXIF 2018 года — это другой кадр, просто с похожим именем.
    confirmed = [c for c in cands if c["dt_match"] == "совпадает"]
    unresolved = [c for c in cands if c["dt_match"] in ("нет данных", "не задано")]

    need_deep = want_dt is not None and images and not a.no_content and (
        a.deep or (not confirmed and not unresolved)
    )
    if need_deep:
        if not a.deep:
            n_contra = sum(1 for c in cands if c["dt_match"] == "противоречит")
            print(f"\n  ни один из {len(cands)} кандидатов не подтверждён датой съёмки"
                  + (f" ({n_contra} прямо ей противоречат)" if n_contra else ""))
            print("  значит, искомый файл переименован — включаю проход 2")
        new = walk_exif(images, want_dt, a.tolerance, a.quiet, a.threads)
        cands += build(new)
    elif want_dt is not None and confirmed and not a.deep:
        print(f"\n  проход 2 (EXIF по {len(images):,} изображениям) пропущен: "
              f"дата съёмки подтвердила {len(confirmed)} кандидат(ов)")
        print("  добавьте --deep, если ищете ещё и переименованные копии")

    if not cands:
        print("\nНичего не найдено.")
        if ref:
            print("Запустите полный обход (01/02) и ищите по sha256/pHash эталона.")
        return

    cands.sort(key=score)

    # пересчёт после возможного прохода 2 — иначе итог опирался бы на
    # состояние до него
    confirmed = [c for c in cands if c["dt_match"] == "совпадает"]
    unresolved = [c for c in cands if c["dt_match"] in ("нет данных", "не задано")]

    best = cands[0]
    top_ok = best["dt_match"] in ("совпадает", "не задано")

    print("\n" + "=" * 78)
    if want_dt is None:
        print("КАНДИДАТЫ (первый — наиболее вероятный оригинал)")
    elif top_ok and best["dt_match"] == "совпадает":
        print("КАНДИДАТЫ (первый подтверждён датой съёмки)")
    elif best["dt_match"] == "нет данных":
        print("КАНДИДАТЫ (ни один не подтверждён датой — EXIF отсутствует)")
    else:
        print("КАНДИДАТЫ — ВНИМАНИЕ: НИ ОДИН НЕ ПОДХОДИТ ПО ДАТЕ")
    print("=" * 78)

    badge = {
        "совпадает": "✓ дата съёмки совпадает с искомой",
        "противоречит": "✗ ДАТА НЕ ТА — это другой кадр, совпало только имя",
        "нет данных": "? EXIF нет, по дате судить не по чему",
        "не задано": "",
    }
    for i, c in enumerate(cands, 1):
        if want_dt is None:
            mark = "  <<< ВЕРОЯТНЫЙ ОРИГИНАЛ" if i == 1 else ""
        elif i == 1 and c["dt_match"] == "совпадает":
            mark = "  <<< ВЕРОЯТНЫЙ ОРИГИНАЛ"
        elif i == 1 and c["dt_match"] == "нет данных":
            mark = "  <<< наиболее вероятный из имеющихся, датой НЕ подтверждён"
        else:
            mark = ""
        res = f'{c["w"]}x{c["h"]}' if c["w"] else "не изображение / не открылось"
        print(f"\n{i}. {c['path']}{mark}")
        print(f"   найден по: {c.get('hit_by','?')}")
        print(f"   размер   {L.human(c['size'])}   разрешение {res}")
        print(f"   EXIF     {c['exif_dt'] or '— (нет даты съёмки)'}"
              f"   камера {c['cam'] or '—'}")
        if badge.get(c["dt_match"]):
            print(f"   ДАТА     {badge[c['dt_match']]}")
        print(f"   изменён  {time.strftime('%Y-%m-%d %H:%M', time.localtime(c['mtime']))}")
        if c["phash"]:
            print(f"   pHash    {c['phash']}")
        if c["match"]:
            print(f"   СВЕРКА   {', '.join(c['match'])}")

    print("\n" + "-" * 78)
    if want_dt is not None and not confirmed:
        contra = [c for c in cands if c["dt_match"] == "противоречит"]
        if contra and not unresolved:
            print("Искомый кадр НЕ НАЙДЕН.")
            print(f"Все {len(contra)} находок противоречат дате "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(want_dt))} — "
                  "совпало только имя.")
            print("Дальше: --deep (EXIF всех изображений), либо --tolerance больше,")
            print("либо дата на снимке — это не EXIF-дата съёмки "
                  "(скан, отредактированный файл, впечатанная дата).")
        elif unresolved:
            print("Ни один кандидат не подтверждён датой съёмки: у них нет EXIF.")
            print("Проверьте глазами; --deep поищет переименованные копии с EXIF.")
    if len(cands) > 1:
        # кластеризуем по pHash с допуском, а не по точному совпадению строки
        hs = [c["phash"] for c in cands if c["phash"]]
        clusters = []
        for h in hs:
            for cl in clusters:
                if hamming(h, cl[0]) <= a.phash_distance:
                    cl.append(h)
                    break
            else:
                clusters.append([h])
        if len(clusters) == 1 and hs:
            print(f"Все {len(hs)} находок — один и тот же кадр в разных версиях "
                  "(ресайзы/пересохранения). Оригинал — первый в списке.")
        elif len(clusters) > 1:
            print(f"Среди находок {len(clusters)} разных кадра — часть совпала "
                  "только по имени. Сверьтесь глазами перед решением.")
    print("Файлы не изменялись. Решение о том, что оставить, — за вами.")

    if not a.no_csv:
        path = a.csv or default_csv_path(stem)
        write_csv(path, cands, want_dt, a.target, roots)
        print(f"\nОтчёт: {os.path.abspath(path)}")


def default_csv_path(stem):
    here = os.path.dirname(os.path.abspath(__file__))
    safe = re.sub(r"[^\w.-]+", "_", stem)[:40] or "search"
    return os.path.join(here, "..", "reports",
                        f"find_{safe}_{time.strftime('%Y%m%d-%H%M%S')}.csv")


if __name__ == "__main__":
    main()
