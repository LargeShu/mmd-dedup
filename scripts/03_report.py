#!/usr/bin/env python3
"""
Этап 3 — группировка дублей и отчёты для принятия решения.

Работает ТОЛЬКО с базой. Сетевые диски не открываются вообще.
Ничего не перемещает и не удаляет: результат — набор файлов в ../reports.

    python3 03_report.py
    python3 03_report.py --phash-distance 6     # мягче/строже поиск похожих

Отчёты:
    reports/00_summary.md            общая картина: объёмы, форматы, годы
    reports/folder_map.csv           карта папок обоих дисков
    reports/overlap_folders.csv      папки-тёзки на двух дисках
    reports/dup_exact.csv            точные копии (SHA-256)
    reports/dup_exif.csv             один кадр в разных версиях (EXIF)
    reports/dup_similar.csv          визуально похожие (pHash)
    reports/decision.html            сводный отчёт для просмотра глазами
"""

import argparse
import collections
import csv
import html
import os
import pathlib
import sys
import time


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mmdlib as L  # noqa: E402
from app_assets import BODY as APP_BODY  # noqa: E402
from app_assets import CSS as APP_CSS  # noqa: E402
from app_assets import JS as APP_JS  # noqa: E402

# Целевой диск читается из config.json при старте: зашивать его в код нельзя,
# у коллег раскладка другая. Значение подставляется в main().
TARGET_DRIVE = None


# ------------------------------------------------------------- выбор «оставить»


def rank(row):
    """
    Меньше — лучше. Общая с 00_find_original.py логика, см. L.originality_key.

    Раньше здесь был свой упрощённый порядок, который смотрел только на диск,
    разрешение, размер и mtime. Из-за этого пересохранённая копия без EXIF,
    но с большим разрешением, могла обойти настоящий оригинал от камеры.
    """
    return L.originality_key(
        path=row["relpath"], drive=row["drive"], target_drive=TARGET_DRIVE,
        size=row["size"], mtime=row["mtime"], depth=row["depth"],
        width=row.get("width"), height=row.get("height"),
        exif_dt=row.get("exif_dt"),
    )


def why(row, best):
    r = []
    if row["drive"] == TARGET_DRIVE:
        r.append("на целевом диске")
    if row.get("exif_dt"):
        r.append(f"EXIF сохранён ({str(row['exif_dt'])[:10]})")
    px = (row.get("width") or 0) * (row.get("height") or 0)
    bpx = (best.get("width") or 0) * (best.get("height") or 0)
    if px and px >= bpx:
        r.append(f"разрешение {row['width']}x{row['height']}")
    if row["size"] >= best["size"]:
        r.append("наибольший размер")
    return ", ".join(r) or "выбран по порядку сортировки"


def load_files(con):
    cur = con.execute(
        """SELECT f.id, f.drive, f.relpath, f.reldir, f.name, f.ext, f.kind,
                  f.size, f.mtime, f.depth,
                  s.sha256, s.phash, s.dhash, s.width, s.height,
                  s.exif_dt, s.cam_make, s.cam_model, s.exif_serial, s.duration
           FROM files f LEFT JOIN sigs s ON s.file_id=f.id
           WHERE f.kind IN ('image','raw','video')"""
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur]


# ------------------------------------------------------------------ группировка


def group_exact(files):
    by = collections.defaultdict(list)
    for f in files:
        if f["sha256"]:
            by[f["sha256"]].append(f)
    return {k: v for k, v in by.items() if len(v) > 1}


def group_exif(files):
    """Один и тот же кадр: дата съёмки + камера. Разные форматы/версии."""
    by = collections.defaultdict(list)
    for f in files:
        if f["exif_dt"] and len(str(f["exif_dt"])) >= 19:
            key = (
                f["exif_dt"],
                (f["cam_model"] or "").lower(),
                (f["exif_serial"] or "").lower(),
            )
            by[key].append(f)
    out = {}
    for k, v in by.items():
        if len(v) > 1:
            shas = {x["sha256"] for x in v if x["sha256"]}
            # если все файлы группы уже покрыты как точные копии — не дублируем
            if len(shas) <= 1 and all(x["sha256"] for x in v):
                continue
            out["|".join(k)] = v
    return out


BUCKET_CAP = 400


def band_bounds(nbands):
    """64 бита делятся на nbands полос максимально ровно. -> [(сдвиг, ширина)]"""
    step, rest = divmod(64, nbands)
    out, pos = [], 0
    for i in range(nbands):
        w = step + (1 if i < rest else 0)
        out.append((pos, w))
        pos += w
    return out


def group_phash(files, maxdist, quiet=False):
    """
    Похожие изображения.

    Кандидаты ищутся так: 64-битный хеш режется на полосы, в сравнение идут
    только файлы, у которых совпала хотя бы одна полоса целиком.

    ЧИСЛО ПОЛОС ВЫВОДИТСЯ ИЗ ПОРОГА, а не задано константой. По принципу
    Дирихле, если различающихся бит не больше, чем полос минус одна, то хотя
    бы одна полоса совпадёт целиком — и пара обязательно встретится.

    Раньше полос было ровно 4 при пороге по умолчанию 6. Полнота при этом
    гарантировалась лишь до расстояния 3, а на 6 терялось около 41% пар:
    поиск выглядел мягким, а на деле возвращал случайную выборку похожих.
    """
    nbands = maxdist + 1
    bounds = band_bounds(nbands)
    cand = [f for f in files if f["phash"]]
    if not quiet:
        print(f"  phash: порог {maxdist} -> {nbands} полос по "
              f"{bounds[0][1]} бит; полнота гарантирована")
    bands = collections.defaultdict(list)
    for i, f in enumerate(cand):
        h = int(f["phash"], 16)
        f["_h"] = h
        for b, (pos, w) in enumerate(bounds):
            bands[(b, (h >> pos) & ((1 << w) - 1))].append(i)

    parent = list(range(len(cand)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Отброшенные корзины — тоже потеря полноты, и раньше о ней молчали.
    dropped = sum(1 for v in bands.values() if len(v) > BUCKET_CAP)
    dropped_files = len({i for v in bands.values() if len(v) > BUCKET_CAP
                         for i in v})
    if dropped and not quiet:
        print(f"  phash: отброшено корзин крупнее {BUCKET_CAP}: {dropped:,} "
              f"(затронуто файлов {dropped_files:,})")
        print("  phash: так выглядят однотонные кадры и сканы; без отбрасывания "
              "они слиплись бы в одну группу")

    for idxs in bands.values():
        if len(idxs) < 2 or len(idxs) > BUCKET_CAP:  # однотонные кадры, сканы
            continue
        for i in range(len(idxs)):
            for j in range(i + 1, len(idxs)):
                a, b = cand[idxs[i]], cand[idxs[j]]
                if find(idxs[i]) == find(idxs[j]):
                    continue
                if bin(a["_h"] ^ b["_h"]).count("1") <= maxdist:
                    union(idxs[i], idxs[j])

    clusters = collections.defaultdict(list)
    for i, f in enumerate(cand):
        clusters[find(i)].append(f)
    out = {}
    for k, v in clusters.items():
        if len(v) < 2:
            continue
        if len({x["sha256"] for x in v if x["sha256"]}) <= 1 and all(
            x["sha256"] for x in v
        ):
            continue  # уже точные копии
        out[cand[k]["phash"]] = v
    return out


def persist(con, method, groups):
    con.execute("DELETE FROM group_members WHERE group_id IN "
                "(SELECT id FROM groups WHERE method=?)", (method,))
    con.execute("DELETE FROM groups WHERE method=?", (method,))
    for gkey, members in groups.items():
        members = sorted(members, key=rank)
        best = members[0]
        total = sum(m["size"] for m in members)
        cur = con.execute(
            "INSERT INTO groups (method, gkey, n, bytes, waste) VALUES (?,?,?,?,?)",
            (method, str(gkey)[:120], len(members), total, total - best["size"]),
        )
        gid = cur.lastrowid
        con.executemany(
            "INSERT INTO group_members (group_id, file_id, keep, reason) VALUES (?,?,?,?)",
            [
                (gid, m["id"], 1 if m is best else 0,
                 why(m, best) if m is best else "дубль")
                for m in members
            ],
        )
    con.commit()


# --------------------------------------------------------------------- отчёты


def w_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(header)
        w.writerows(L.csv_row(r) for r in rows)


def dump_groups(path, method, groups):
    rows = []
    for gi, (_gkey, members) in enumerate(sorted(
        groups.items(), key=lambda kv: -sum(m["size"] for m in kv[1])
    ), 1):
        members = sorted(members, key=rank)
        best = members[0]
        for m in members:
            rows.append([
                gi, method, "ОСТАВИТЬ" if m is best else "дубль",
                m["drive"], m["relpath"], m["size"],
                f'{m["width"]}x{m["height"]}' if m.get("width") else "",
                m.get("exif_dt") or "",
                time.strftime("%Y-%m-%d", time.localtime(m["mtime"])),
                why(m, best) if m is best else "",
            ])
    w_csv(path, ["группа", "метод", "решение", "диск", "путь", "байт",
                 "разрешение", "дата съёмки", "mtime", "обоснование"], rows)
    return rows


def folder_map(con, path):
    rows = list(con.execute(
        """SELECT drive, reldir, COUNT(*), SUM(size),
                  SUM(kind='image')+SUM(kind='raw'), SUM(kind='video'),
                  MIN(mtime), MAX(mtime)
           FROM files WHERE kind!='junk'
           GROUP BY drive, reldir ORDER BY SUM(size) DESC"""
    ))
    w_csv(path, ["диск", "папка", "файлов", "байт", "фото", "видео",
                 "старейший", "новейший"],
          [[d, p or "(корень)", n, b or 0, im or 0, v or 0,
            time.strftime("%Y-%m-%d", time.localtime(mn)),
            time.strftime("%Y-%m-%d", time.localtime(mx))]
           for d, p, n, b, im, v, mn, mx in rows])
    return rows


def dup_rows(groups_by_method, thumbs, rts):
    """
    Разворачивает группы в плоский список решений.

    Единица решения — ОДИН ДУБЛЬ: конкретный файл, который предлагается
    удалить, вместе с оригиналом для сравнения. Раскладываются они по папке,
    где лежит сам дубль, а не оригинал: вопрос, который решает человек,
    звучит как «что в этой папке лишнее и можно ли вычистить её целиком».

    Одна группа поэтому может попасть в несколько папок — если её копии
    разбросаны. Это не дублирование, а разные решения.
    """
    def полный(m):
        """
        Настоящий путь в файловой системе, а не наш внутренний псевдоним диска.

        Показывать `photo/АГМА/iu.jpeg` было ошибкой: `photo` — логическое имя
        из конфигурации, такой путь нельзя ни скопировать в Finder, ни открыть
        в терминале.
        """
        return os.path.join(rts.get(m["drive"], m["drive"]), m["relpath"])

    def карточка(m):
        p = полный(m)
        # pathlib.as_uri(), а не ручная склейка: на Windows путь C:\Users\...
        # превращался в file://C%3A%5CUsers — нерабочую ссылку
        return {"p": p, "u": pathlib.Path(p).as_uri(),
                "t": thumbs.get(m["id"], ""), "s": m["size"],
                "m": exif_line(m)}

    rows = []
    for method, groups in groups_by_method.items():
        for gi, (_gkey, members) in enumerate(groups.items(), 1):
            members = sorted(members, key=rank)
            best = members[0]
            keep = карточка(best)
            for d in members[1:]:
                rows.append({
                    # rid — ключ РЕШЕНИЯ, а не файла. Один файл попадает
                    # в несколько строк (группа exact и группа phash),
                    # и у этих строк разные оригиналы. Ключ по id файла
                    # молча связывал такие строки в одно решение.
                    "rid": f'{method}:{d["id"]}:{best["id"]}',
                    "id": d["id"], "method": method, "g": gi,
                    # папка тоже полным путём: её копируют в Finder
                    "folder": os.path.dirname(полный(d)),
                    "dup": карточка(d), "keep": keep, "size": d["size"],
                })
    return rows


def folders_index(rows):
    """Сводка по папкам: сколько дублей и сколько освободится."""
    agg = collections.defaultdict(lambda: {"n": 0, "bytes": 0, "methods": set()})
    for r in rows:
        a = agg[r["folder"]]
        a["n"] += 1
        a["bytes"] += r["size"]
        a["methods"].add(r["method"])
    out = [{"f": k, "n": v["n"], "b": v["bytes"],
            "methods": sorted(v["methods"])} for k, v in agg.items()]
    out.sort(key=lambda x: -x["b"])
    return out


def exif_line(m):
    """Строка с данными съёмки для подписи под превью."""
    bits = []
    if m.get("width"):
        bits.append(f'{m["width"]}×{m["height"]}')
    if m.get("exif_dt"):
        bits.append(str(m["exif_dt"]).replace(":", "-", 2))
    cam = " ".join(x for x in (m.get("cam_make"), m.get("cam_model")) if x)
    if cam:
        bits.append(cam)
    if m.get("duration"):
        bits.append(f'{m["duration"]:.0f} с')
    bits.append(time.strftime("mtime %Y-%m-%d", time.localtime(m["mtime"])))
    return " · ".join(bits)


# ------------------------------------------------------------------- превью

THUMB_DIR = "thumbs"


def export_thumbs(con, out, quiet=False):
    """
    Достаёт превью из базы и раскладывает файлами в reports/thumbs/.

    Диски НЕ читаются: источник — таблица thumbs, которую наполняет
    02_signatures.py --stage thumb. Гарантия «этап 3 не обращается к сетевым
    дискам» остаётся в силе.
    """
    rows = con.execute(
        "SELECT file_id, jpeg FROM thumbs WHERE status='ok' AND jpeg IS NOT NULL"
    ).fetchall()
    if not rows:
        return {}
    tdir = os.path.join(out, THUMB_DIR)
    os.makedirs(tdir, exist_ok=True)
    res = {}
    for fid, blob in rows:
        name = f"{fid}.jpg"
        dst = os.path.join(tdir, name)
        if not os.path.exists(dst) or os.path.getsize(dst) != len(blob):
            with open(dst, "wb") as fh:
                fh.write(blob)
        res[fid] = name
    if not quiet:
        print(f"  превью из базы: {len(res):,}")
    return res


def overlap(con, path):
    """Папки первого уровня с похожими именами на обоих дисках."""
    tops = collections.defaultdict(dict)
    for drive, reldir, n, b in con.execute(
        """SELECT drive, CASE WHEN instr(reldir,'/')>0
                  THEN substr(reldir,1,instr(reldir,'/')-1) ELSE reldir END,
                  COUNT(*), SUM(size)
           FROM files WHERE kind!='junk' GROUP BY 1,2"""
    ):
        key = (reldir or "(корень)").strip().lower()
        e = tops[key].setdefault(drive, [0, 0])
        e[0] += n
        e[1] += b or 0
    rows = []
    for key, per in sorted(tops.items()):
        if len(per) > 1:
            rows.append([key,
                         per.get("photo", [0, 0])[0], per.get("photo", [0, 0])[1],
                         per.get("300photos", [0, 0])[0], per.get("300photos", [0, 0])[1]])
    w_csv(path, ["папка", "photo: файлов", "photo: байт",
                 "300photos: файлов", "300photos: байт"], rows)
    return rows


def summary_md(con, path, stats):
    lines = ["# Инвентаризация библиотеки изображений", ""]
    lines.append(f"Дата отчёта: {time.strftime('%Y-%m-%d %H:%M')}  ")
    lines.append(f"Обход начат: {L.get_meta(con,'inventory_started','—')}  ")
    lines.append(f"Обход завершён: {L.get_meta(con,'inventory_finished','—')}")
    lines.append("")
    lines.append("## Состав по дискам")
    lines.append("")
    lines.append("| диск | класс | файлов | объём |")
    lines.append("|---|---|---:|---:|")
    for d, k, n, b in con.execute(
        "SELECT drive, kind, COUNT(*), SUM(size) FROM files "
        "GROUP BY drive, kind ORDER BY drive, SUM(size) DESC"
    ):
        lines.append(f"| {d} | {k} | {n:,} | {L.human(b or 0)} |")

    lines += ["", "## Топ форматов", "", "| расширение | файлов | объём |", "|---|---:|---:|"]
    for e, n, b in con.execute(
        "SELECT ext, COUNT(*), SUM(size) FROM files WHERE kind!='junk' "
        "GROUP BY ext ORDER BY SUM(size) DESC LIMIT 15"
    ):
        lines.append(f"| {e or '(без)'} | {n:,} | {L.human(b or 0)} |")

    lines += ["", "## Распределение по годам (mtime)", "",
              "| год | файлов | объём |", "|---|---:|---:|"]
    for y, n, b in con.execute(
        "SELECT strftime('%Y', mtime, 'unixepoch'), COUNT(*), SUM(size) "
        "FROM files WHERE kind!='junk' GROUP BY 1 ORDER BY 1"
    ):
        lines.append(f"| {y} | {n:,} | {L.human(b or 0)} |")

    lines += ["", "## Найденные дубликаты", "",
              "| метод | групп | файлов в группах | освободится |", "|---|---:|---:|---:|"]
    for m in ("exact", "exif", "phash"):
        g, n, wst = con.execute(
            "SELECT COUNT(*), SUM(n), SUM(waste) FROM groups WHERE method=?", (m,)
        ).fetchone()
        lines.append(f"| {m} | {g:,} | {(n or 0):,} | {L.human(wst or 0)} |")

    lines += ["", "## Как читать", "",
              "- **exact** — побайтово одинаковые файлы. Решение безопасно принимать по списку.",
              "- **exif** — один и тот же кадр в разных версиях (RAW+JPEG, экспорт, правка).",
              "  Оставлять обычно надо RAW/наибольшее разрешение — проверьте выборочно.",
              "- **phash** — визуально похожие. **Требует просмотра глазами**: сюда попадают",
              "  серии кадров подряд и кропы. Не удалять по списку без проверки.",
              "", "## Что дальше", "",
              "Файлы не перемещались и не удалялись — это только инвентаризация.",
              "Решение по слиянию `300 Photos` -> `photo` принимается вручную",
              "на основании `dup_*.csv` и `overlap_folders.csv`.", ""]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def decision_md(path, rows, folders, chunk=3000):
    """
    Чек-листы для Obsidian. Генерируются по флагу --md; основной инструмент
    теперь интерактивный HTML.

    Разложены так же, как в HTML: по папке, где лежит ДУБЛЬ. Раздел
    «iPhone Lev/2014» отвечает на вопрос «что в этой папке лишнее».

    Одним файлом список был бы порядка 15 МБ — в Obsidian с таким не
    поработать, поэтому папки нарезаны частями по chunk строк, а decision.md
    служит оглавлением. Существующие файлы не перезаписываются.
    """
    out_dir = os.path.dirname(os.path.abspath(path))
    stamp = time.strftime("%Y%m%d-%H%M%S")

    def сохранить(имя, строки):
        p = os.path.join(out_dir, имя)
        if os.path.exists(p):
            base, ext = os.path.splitext(имя)
            p = os.path.join(out_dir, f"{base}_{stamp}{ext}")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("\n".join(строки))
        return os.path.basename(p)

    по_папкам = collections.defaultdict(list)
    for r in rows:
        по_папкам[r["folder"]].append(r)

    части = []
    текущая, счёт = [], 0
    for f in folders:
        items = по_папкам[f["f"]]
        if счёт and счёт + len(items) > chunk:
            части.append(текущая)
            текущая, счёт = [], 0
        текущая.append((f, items))
        счёт += len(items)
    if текущая:
        части.append(текущая)

    имена = []
    for ci, часть in enumerate(части, 1):
        имя = ("decision_list.md" if len(части) == 1
               else f"decision_list_{ci:02}.md")
        строки = [
            "---",
            "tags: [инвентаризация, дубликаты, MMD-2026]",
            f"дата: {time.strftime('%Y-%m-%d')}",
            "статус: на рассмотрении",
            "---",
            "",
            "# Дубликаты по папкам"
            + (f" — часть {ci} из {len(части)}" if len(части) > 1 else ""),
            "",
            "[[decision]] ← оглавление",
            "",
            "> [!info] `- [ ]` — файл в ЭТОЙ папке, который предлагается удалить.",
            "> Рядом указан оригинал, который остаётся. Отметка означает",
            "> «согласен убрать» и ничего не запускает.",
            "",
        ]
        for f, items in часть:
            строки += [f"## {L.md_text(f['f'])}", "",
                       f"Дублей: {len(items)} · освободится: "
                       f"**{L.human(f['b'])}** · методы: "
                       f"{', '.join(f['methods'])}", ""]
            for r in items:
                d, k = r["dup"], r["keep"]
                строки.append(
                    f"- [ ] удалить [{L.md_text(d['p'])}]({d['u']}) — "
                    f"{L.human(d['s'])} · {L.md_text(d['m'])}")
                строки.append(
                    f"　　остаётся [{L.md_text(k['p'])}]({k['u']}) — "
                    f"{L.human(k['s'])} · {L.md_text(k['m'])}")
            строки.append("")
        имена.append(сохранить(имя, строки))

    всего_байт = sum(f["b"] for f in folders)
    idx = [
        "---",
        "tags: [инвентаризация, дубликаты, MMD-2026]",
        f"дата: {time.strftime('%Y-%m-%d')}",
        f"целевой_диск: {TARGET_DRIVE}",
        f"потенциально_освободится: {L.human(всего_байт)}",
        "---",
        "",
        "# Решения по дубликатам",
        "",
        "> [!tip] Основной инструмент — `decision.html`",
        "> Там превью, поиск, фильтр «только неразобранные» и сохранение",
        "> отметок. Эти markdown-файлы — архивная копия для Obsidian.",
        "",
        f"Всего дублей: **{len(rows):,}** в **{len(folders):,}** папках, "
        f"освободится **{L.human(всего_байт)}**.",
        "",
        "## Чек-листы",
        "",
    ]
    idx += [f"- [[{os.path.splitext(n)[0]}]]" for n in имена] or ["_пусто_"]
    idx += ["", "## Крупнейшие папки", "",
            "| папка | дублей | освободится |", "|---|---:|---:|"]
    for f in folders[:40]:
        idx.append(f"| `{f['f']}` | {f['n']} | {L.human(f['b'])} |")
    idx.append("")
    имя_idx = сохранить(os.path.basename(path), idx)
    return os.path.join(out_dir, имя_idx), имена


def selfcheck(rows, folders, quiet=False):
    """
    Инварианты на РЕАЛЬНЫХ данных, а не на синтетическом стенде.

    Юнит-тесты проверяют правила, которые мы уже знаем; стенды — по десятку
    файлов. На ста тысячах вылезает другое, поэтому отчёт проверяет сам себя
    после сборки. Найденное здесь — повод не доверять отчёту, а не «мелочь».
    """
    беды = []

    rids = [r["rid"] for r in rows]
    if len(rids) != len(set(rids)):
        из_повторов = len(rids) - len(set(rids))
        беды.append(f"ключи решений повторяются: {из_повторов:,} — отметки "
                    "в отчёте будут слипаться")

    без_оригинала = [r for r in rows if not r["keep"]["p"]]
    if без_оригинала:
        беды.append(f"строк без оригинала: {len(без_оригинала):,}")

    сам_себе = [r for r in rows if r["dup"]["p"] == r["keep"]["p"]]
    if сам_себе:
        беды.append(f"дубль совпадает с оригиналом: {len(сам_себе):,}")

    # Файл, предложенный к удалению в одной строке и как оригинал в другой:
    # решения по таким строкам конфликтуют, и 04_apply их отклонит
    дубли = {r["dup"]["p"] for r in rows}
    оригиналы = {r["keep"]["p"] for r in rows}
    спорные = дубли & оригиналы
    нулевые = [r for r in rows if not r["size"]]

    сумма = sum(f["n"] for f in folders)
    if сумма != len(rows):
        беды.append(f"по папкам разложено {сумма:,} строк из {len(rows):,}")

    if not quiet:
        if беды:
            print("\n  !! САМОПРОВЕРКА ОТЧЁТА НАШЛА ПРОБЛЕМЫ:")
            for b in беды:
                print(f"     - {b}")
        else:
            print("  самопроверка отчёта: инварианты соблюдены")
        if спорные:
            print(f"  внимание: {len(спорные):,} файлов предлагаются к удалению "
                  "в одной строке и как оригинал в другой")
            print("     это не ошибка отчёта, но решения по ним связаны: "
                  "04_apply отклонит противоречивые")
        if нулевые:
            print(f"  внимание: файлов нулевого размера среди дублей: "
                  f"{len(нулевые):,}")
    return беды


def decision_app(path, rows, folders, title):
    """
    Интерактивный отчёт: превью, ссылки на файлы, отметки с сохранением.

    Отметки живут в localStorage браузера и дублируются экспортом в JSON.
    localStorage привязан к браузеру и гибнет вместе с кэшем — поэтому
    экспорт не украшение, а основной способ сохранить работу.

    Папки раскрываются по клику и рендерятся только в этот момент: при
    десятках тысяч строк разом браузер не справился бы.
    """
    import json

    data = {
        "report_id": time.strftime("%Y%m%d-%H%M%S"),
        # Отчёт живёт дольше запуска и уходит к другому человеку.
        # Версия внутри него — единственный способ понять, чем он собран.
        "version": L.VERSION,
        "rows": rows,
        "folders": folders,
    }
    # Экранирование обязательно: имя файла вида  <script>alert(1)</script>.jpg
    # закрывает тег раньше времени и превращает данные в исполняемый код.
    # json.dumps сам по себе от этого не защищает — он про JSON, а не про HTML.
    blob = (json.dumps(data, ensure_ascii=False)
            .replace("</", r"<\/")
            .replace(" ", r" ")
            .replace(" ", r" "))
    js = APP_JS.replace("__DATA__", blob)
    body = (APP_BODY.replace("__TITLE__", html.escape(title))
            .replace("__TOTAL__", f"{len(rows):,}".replace(",", " "))
            .replace("__VERSION__", html.escape(L.version_line())))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("<!doctype html>\n<html lang=ru>\n<meta charset=utf-8>\n"
                 "<title>Решения по дубликатам</title>\n"
                 f"<style>{APP_CSS}</style>\n{body}\n"
                 f"<script>{js}</script>\n</html>")
    return path


def main():
    ap = argparse.ArgumentParser()
    L.add_version_arg(ap)
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--phash-distance", type=int, default=4, metavar="0..8",
                    help="0=бит в бит, 3=строго, 4=по умолчанию, 6=свободно. "
                         "Число полос выводится отсюда, поэтому цена растёт "
                         "резко: 4 -> ~5 млн сверок, 6 -> ~80 млн")
    ap.add_argument("--target", metavar="ИМЯ",
                    help="целевой диск: файл на нём выигрывает при выборе "
                         "«кого оставить». По умолчанию из config.json")
    ap.add_argument("--md", action="store_true",
                    help="дополнительно сгенерировать чек-листы для Obsidian")
    ap.add_argument("--md-chunk", type=int, default=3000, metavar="N",
                    help="сколько групп в одном файле чек-листа")
    a = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    db = a.db or os.path.join(here, "..", "data", "inventory.db")
    out = a.out or os.path.join(here, "..", "reports")
    os.makedirs(out, exist_ok=True)

    con = L.connect(db)
    L.init_db(con)
    files = load_files(con)
    print(f"медиафайлов в базе: {len(files):,}")
    if not files:
        print("база пуста — сначала 01_inventory.py")
        return

    have_sha = sum(1 for f in files if f["sha256"])
    have_ph = sum(1 for f in files if f["phash"])
    have_ex = sum(1 for f in files if f["exif_dt"])
    print(f"сигнатуры: sha256 {have_sha:,} | exif {have_ex:,} | phash {have_ph:,}")

    g = {
        "exact": group_exact(files),
        "exif": group_exif(files),
        "phash": group_phash(files, a.phash_distance),
    }
    for m, gs in g.items():
        persist(con, m, gs)
        print(f"  {m}: групп {len(gs):,}")

    dump_groups(os.path.join(out, "dup_exact.csv"), "exact", g["exact"])
    dump_groups(os.path.join(out, "dup_exif.csv"), "exif", g["exif"])
    dump_groups(os.path.join(out, "dup_similar.csv"), "phash", g["phash"])
    folder_map(con, os.path.join(out, "folder_map.csv"))
    overlap(con, os.path.join(out, "overlap_folders.csv"))
    summary_md(con, os.path.join(out, "00_summary.md"), None)
    rts = {k[5:]: v for k, v in
           con.execute("SELECT key, value FROM meta WHERE key LIKE 'root_%'")}

    # Целевой диск: сначала явный флаг, затем config.json, затем — тот, что
    # первым попал в базу. Зашивать его в код нельзя: у коллег своя раскладка.
    global TARGET_DRIVE
    cfg = L.load_config(required=False)
    TARGET_DRIVE = a.target or cfg.get("target") or (next(iter(rts), None))
    if TARGET_DRIVE and TARGET_DRIVE not in rts:
        raise SystemExit(f"целевой диск '{TARGET_DRIVE}' не найден в базе. "
                         f"Есть: {', '.join(rts) or '(пусто)'}")
    print(f"целевой диск: {TARGET_DRIVE or 'не задан'}")
    thumbs = export_thumbs(con, out)
    rows = dup_rows(g, thumbs, rts)
    folders = folders_index(rows)
    selfcheck(rows, folders)
    decision_app(os.path.join(out, "decision.html"), rows, folders,
                 f"{len(rows):,} дублей в {len(folders):,} папках"
                 .replace(",", " "))
    print(f"  интерактивный отчёт: {len(rows):,} дублей "
          f"в {len(folders):,} папках")
    md, parts = (None, [])
    if a.md:
        md, parts = decision_md(os.path.join(out, "decision.md"), rows,
                                folders, a.md_chunk)

    print(f"\nотчёты записаны в: {os.path.abspath(out)}")
    if md and os.path.basename(md) != "decision.md":
        print(f"  ВНИМАНИЕ: decision.md уже существует и не тронут "
              f"(ваши отметки целы).\n  Новое оглавление: {os.path.basename(md)}")
    if parts:
        print(f"  чек-листы: {', '.join(parts)}")
    for f in sorted(os.listdir(out)):
        print("  " + f)
    con.close()


if __name__ == "__main__":
    main()
