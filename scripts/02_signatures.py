#!/usr/bin/env python3
"""
Этап 2 — снятие сигнатур. Файлы открываются ТОЛЬКО на чтение ('rb').

Три независимые стадии, каждая resumable и запускается отдельно:

  exact  — SHA-256 полного содержимого. Считается только для файлов, у которых
           размер совпадает хотя бы с одним другим файлом (иначе дубля быть
           не может). Даёт 100% достоверные точные копии.
  exif   — DateTimeOriginal + модель камеры + серийник из заголовка.
           Читается только начало файла. Даёт группы «один и тот же кадр»
           (RAW+JPEG, экспорт в разном качестве, копия после редактора).
  phash  — perceptual hash (pHash + dHash) изображений. Ловит ресайзы,
           пересохранения, лёгкие правки. Самая дорогая стадия.

Порядок: exact → exif → phash. Каждая следующая опирается на предыдущую,
но может быть прервана и продолжена.

    python3 02_signatures.py --plan             # оценка объёма работ, ничего не читает
    python3 02_signatures.py --stage exact
    python3 02_signatures.py --stage exif
    python3 02_signatures.py --stage phash
    python3 02_signatures.py --stage all

Стадию можно писать и короче: --exact, --exif, --phash, --all

Зависимости ставятся ТОЛЬКО в venv проекта, в системный Python — никогда:
    python3 -m venv .venv && .venv/bin/pip install -r ../requirements.txt
    .venv/bin/python 02_signatures.py --stage exact

Без библиотек стадия exact работает как есть, exif и phash аккуратно
пропускаются с подсказкой.
Для длительности видео желателен ffprobe (brew install ffmpeg) — необязателен.
"""

import argparse
import collections
import csv
import datetime as dt
import hashlib
import os
import queue
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mmdlib as L  # noqa: E402

HEAD = 256 * 1024
CHUNK = 4 * 1024 * 1024

# Потолок на число пикселей при декодировании. Полное отключение
# (MAX_IMAGE_PIXELS = None) снимает защиту Pillow от decompression bomb:
# крошечный файл может развернуться в гигабайты в памяти. 512 Мпикс с запасом
# перекрывает любую реальную фотокамеру и среднеформатные сканы.
MAX_PIXELS = 512_000_000

# Версия «рецепта» каждой стадии. Стадии не пересчитывают уже заполненные
# строки — это и делает конвейер возобновляемым. Но если поменять алгоритм
# (размер pHash, способ декодирования, набор EXIF-тегов), в базе окажутся
# сигнатуры двух поколений, и группировка молча смешает их между собой.
#
# Поэтому рецепт хранится в meta и сверяется при каждом запуске.
# Меняете логику стадии — поднимите здесь версию.
SIG_RECIPE = {
    "exact": "sha256-full+head256k/v1",
    "exif":  "pillow-getexif:36867,271,272,42033/v1",
    "phash": "imagehash.phash8+dhash8/draft-rgb-256/v1",
}

RECIPE_COLS = {
    "exact": ("sha256", "head_sha"),
    "exif":  ("exif_dt", "cam_make", "cam_model", "exif_serial", "width", "height"),
    "phash": ("phash", "dhash"),
}


def check_recipe(con, stage, recompute):
    """
    Сверяет версию алгоритма стадии с той, которой посчитаны данные в базе.

    Возвращает True, если стадию можно запускать.
    """
    key = f"recipe_{stage}"
    was = L.get_meta(con, key)
    now = SIG_RECIPE[stage]
    cols = RECIPE_COLS[stage]
    filled = con.execute(
        f"SELECT COUNT(*) FROM sigs WHERE {cols[0]} IS NOT NULL"  # noqa: S608
    ).fetchone()[0]

    if recompute:
        sets = ", ".join(f"{c}=NULL" for c in cols)
        con.execute(f"UPDATE sigs SET {sets}")  # noqa: S608
        L.set_meta(con, key, now)
        con.commit()
        print(f"[{stage}] --recompute: сброшено {filled:,} ранее посчитанных значений")
        return True

    if was is None or not filled:
        L.set_meta(con, key, now)
        con.commit()
        return True

    if was == now:
        return True

    print(f"\n!! [{stage}] алгоритм изменился с прошлого запуска")
    print(f"   было:  {was}")
    print(f"   стало: {now}")
    print(f"   В базе {filled:,} значений старого поколения. Смешивать их "
          "с новыми нельзя:")
    print("   группировка дала бы неверные результаты.")
    print(f"\n   Пересчитать только эту стадию:  --stage {stage} --recompute")
    print("   Удалять базу целиком не нужно: инвентарь этапа 1 и остальные "
          "стадии не пострадали.")
    return False


# ------------------------------------------------------------------ хелперы


def roots(con):
    out = {}
    for k, v in con.execute("SELECT key, value FROM meta WHERE key LIKE 'root_%'"):
        out[k[5:]] = v
    return out


def fullpath(rts, drive, relpath):
    return os.path.join(rts[drive], relpath)


def sha_file(path, limit=None):
    h = hashlib.sha256()
    got = 0
    with open(path, "rb") as f:
        while True:
            want = CHUNK if limit is None else min(CHUNK, limit - got)
            if want <= 0:
                break
            b = f.read(want)
            if not b:
                break
            h.update(b)
            got += len(b)
    return h.hexdigest()


# Имена колонок sigs подставляются в SQL как идентификаторы, а не параметры.
# Сейчас они приходят только из кода, но белый список убирает саму возможность
# ошибки при будущих правках.
SIG_COLS = {
    "sha256", "head_sha", "phash", "dhash", "width", "height", "exif_dt",
    "cam_make", "cam_model", "exif_serial", "duration", "status", "note",
}


def upsert(con, file_id, **kw):
    bad = set(kw) - SIG_COLS
    if bad:
        raise ValueError(f"недопустимые колонки sigs: {sorted(bad)}")
    cols = ", ".join(kw)
    ph = ", ".join("?" * len(kw))
    upd = ", ".join(f"{c}=excluded.{c}" for c in kw)
    con.execute(
        # cols/upd — только имена из белого списка SIG_COLS (проверено выше),
        # значения идут параметрами. Идентификаторы SQLite параметризовать не умеет.
        f"INSERT INTO sigs (file_id, {cols}) VALUES (?, {ph}) "  # noqa: S608
        f"ON CONFLICT(file_id) DO UPDATE SET {upd}",
        (file_id, *kw.values()),
    )


PROGRESS_EVERY = 15   # секунд; при 5 с за час набегает 700 строк
WINDOW = 60           # секунд, окно для оценки текущей скорости


def run_pool(items, worker, nthreads, label, con, commit_every=200,
             weight=None, upsert_fn=None):
    """
    items -> worker(item) -> (file_id, dict) для записи в sigs.

    weight(item) -> байт: если задан, прогресс и ETA считаются ПО БАЙТАМ.
    Для exact это принципиально: кандидаты идут по возрастанию размера, и
    доля обработанных файлов не имеет отношения к доле выполненной работы —
    на 27% файлов бывает прочитано 3% байт.

    upsert_fn — куда писать результат; по умолчанию таблица sigs.

    Возвращает статистику прогона для журнала.
    """
    write = upsert_fn or upsert
    stat = {"label": label, "done": 0, "total": len(items), "bytes_done": 0,
            "bytes_total": sum(weight(i) for i in items) if weight else 0,
            "errors": 0, "interrupted": False, "seconds": 0.0}
    if not items:
        print(f"  {label}: работы нет")
        return stat

    q = queue.Queue()
    for it in items:
        q.put(it)
    results = queue.Queue()
    total = stat["total"]
    stop = threading.Event()

    def run():
        while not stop.is_set():
            try:
                it = q.get_nowait()
            except queue.Empty:
                return
            try:
                results.put((worker(it), weight(it) if weight else 0))
            except Exception as e:  # noqa: BLE001
                results.put((((it[0], {"status": "error", "note": repr(e)[:200]})),
                             weight(it) if weight else 0))

    threads = [threading.Thread(target=run, daemon=True) for _ in range(nthreads)]
    for t in threads:
        t.start()

    done = 0
    nbytes = 0
    errors = 0
    t0 = time.time()
    last = t0
    pending = 0
    # (время, файлов, байт) — для скорости по скользящему окну: среднее
    # с начала прогона инертно и маскирует переход к крупным файлам
    window = collections.deque([(t0, 0, 0)])

    def progress():
        now = time.time()
        while len(window) > 1 and now - window[0][0] > WINDOW:
            window.popleft()
        t_ago, d_ago, b_ago = window[0]
        span = max(now - t_ago, 0.001)
        fps = (done - d_ago) / span
        bps = (nbytes - b_ago) / span
        if weight and stat["bytes_total"]:
            pct = 100 * nbytes / stat["bytes_total"]
            eta = (stat["bytes_total"] - nbytes) / max(bps, 1)
            return (f"  {label}: {done:,}/{total:,} файлов "
                    f"({100*done/total:.1f}%) | "
                    f"{L.human(nbytes)}/{L.human(stat['bytes_total'])} "
                    f"({pct:.1f}%) | {bps/1e6:.0f} МБ/с | ~{L.dur(eta)}")
        eta = (total - done) / max(fps, 0.001)
        return (f"  {label}: {done:,}/{total:,} ({100*done/total:.1f}%) "
                f"| {fps:.0f}/с | ~{L.dur(eta)}")

    try:
        while done < total:
            try:
                (fid, data), w = results.get(timeout=1)
            except queue.Empty:
                if not any(t.is_alive() for t in threads):
                    break
                continue
            write(con, fid, **data)
            done += 1
            nbytes += w
            if data.get("status") == "error":
                errors += 1
            pending += 1
            if pending >= commit_every:
                con.commit()
                pending = 0
            now = time.time()
            if now - last > PROGRESS_EVERY:
                window.append((now, done, nbytes))
                print(progress(), flush=True)
                last = now
    except KeyboardInterrupt:
        stop.set()
        con.commit()
        stat["interrupted"] = True
        print(f"\n  прервано на {done:,}/{total:,}. "
              "Перезапуск продолжит с этого места.")
    con.commit()
    stat.update(done=done, bytes_done=nbytes, errors=errors,
                seconds=time.time() - t0)
    return stat


# ------------------------------------------------------------- журнал и сводка

LOG_HEADER = [
    "стадия", "старт", "финиш", "длительность", "секунд",
    "обработано", "всего было", "прочитано байт", "прочитано",
    "файл/с", "МБ/с", "ошибок", "завершено", "рецепт", "потоков", "база",
]


def log_run(path, stat, started, finished, threads, db):
    """
    Дописывает строку в журнал прогонов. Именно дописывает: история нужна,
    чтобы сравнивать запуски между собой и видеть, как менялась скорость.

    Прерванные прогоны тоже записываются — они интереснее всего при разборе.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    new = not os.path.exists(path)
    sec = stat["seconds"]
    row = [
        stat["label"],
        started.strftime("%Y-%m-%d %H:%M:%S"),
        finished.strftime("%Y-%m-%d %H:%M:%S"),
        L.dur(sec), round(sec, 1),
        stat["done"], stat["total"],
        stat["bytes_done"], L.human(stat["bytes_done"]) if stat["bytes_done"] else "",
        round(stat["done"] / max(sec, 0.001), 1),
        round(stat["bytes_done"] / max(sec, 0.001) / 1e6, 1) if stat["bytes_done"] else "",
        stat["errors"],
        "прервано" if stat["interrupted"] else "да",
        SIG_RECIPE.get(stat["label"], ""),
        threads, os.path.basename(db),
    ]
    with open(path, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        if new:
            w.writerow(LOG_HEADER)
        w.writerow(L.csv_row(row))


def print_summary(stat, started, finished, log_path):
    sec = stat["seconds"]
    line = "=" * 62
    print(f"\n== ЭТАП 2 / {stat['label']} {line[:62 - 14 - len(stat['label'])]}")
    print(f"  старт      {started.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  финиш      {finished.strftime('%Y-%m-%d %H:%M:%S')}   ({L.dur(sec)})")
    print(f"  обработано {stat['done']:,} из {stat['total']:,}"
          + ("   ПРЕРВАНО" if stat["interrupted"] else ""))
    if stat["bytes_done"]:
        print(f"  прочитано  {L.human(stat['bytes_done'])}"
              f"  |  {stat['bytes_done']/max(sec,0.001)/1e6:.1f} МБ/с"
              f"  |  {stat['done']/max(sec,0.001):.0f} файл/с")
    else:
        print(f"  скорость   {stat['done']/max(sec,0.001):.0f} файл/с")
    if stat["errors"]:
        print(f"  ошибок     {stat['errors']}   (см. sigs.status='error')")
    if stat["interrupted"]:
        print("  перезапуск продолжит с этого места, пересчёта не будет")
    print(f"  журнал     {log_path}")


# ------------------------------------------------------------------ стадии


def select_exact(con):
    """Файлы, чей размер встречается >1 раза. Только они могут быть точными копиями."""
    return con.execute(
        """
        SELECT f.id, f.drive, f.relpath, f.size FROM files f
        JOIN (SELECT size FROM files
              WHERE kind IN ('image','raw','video') AND size > 0
              GROUP BY size HAVING COUNT(*) > 1) d ON d.size = f.size
        LEFT JOIN sigs s ON s.file_id = f.id
        WHERE f.kind IN ('image','raw','video') AND s.sha256 IS NULL
        ORDER BY f.size
        """
    ).fetchall()


def stage_exact(con, rts, nthreads):
    items = select_exact(con)
    print(f"\n[exact] кандидатов по совпадению размера: {len(items):,}")
    if items:
        print(f"[exact] к прочтению: {L.human(sum(i[3] for i in items))}")

    def worker(it):
        fid, drive, rel, size = it
        p = fullpath(rts, drive, rel)
        head = sha_file(p, HEAD)
        full = head if size <= HEAD else sha_file(p)
        return fid, {"head_sha": head, "sha256": full, "status": "ok"}

    return run_pool(items, worker, nthreads, "exact", con,
                    weight=lambda it: it[3])


def stage_exif(con, rts, nthreads):
    try:
        from PIL import Image, ExifTags
    except ImportError:
        print("[exif] нет Pillow -> .venv/bin/pip install pillow pillow-heif"
              " ; стадия пропущена, остальное работает")
        return None
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        print("[exif] нет pillow-heif: HEIC будет пропущен")

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    TAGS = {v: k for k, v in ExifTags.TAGS.items()}
    T_DT = TAGS.get("DateTimeOriginal")
    T_MK = TAGS.get("Make")
    T_MD = TAGS.get("Model")
    T_SN = TAGS.get("BodySerialNumber")

    # Признак «файл уже разобран» — width, а НЕ exif_dt.
    #
    # Раньше здесь стояло s.exif_dt IS NULL, и снимки без даты съёмки (после
    # мессенджеров, редакторов, сканы) никогда не помечались обработанными:
    # каждый перезапуск читал их заново, и стадия не сходилась. width при
    # успешном открытии заполняется всегда, независимо от наличия EXIF.
    items = con.execute(
        """SELECT f.id, f.drive, f.relpath FROM files f
           LEFT JOIN sigs s ON s.file_id=f.id
           WHERE f.kind IN ('image','raw') AND f.size>0 AND s.width IS NULL
             AND (s.status IS NULL OR s.status='ok')"""
    ).fetchall()
    без_exif = con.execute(
        """SELECT COUNT(*) FROM files f JOIN sigs s ON s.file_id=f.id
           WHERE f.kind IN ('image','raw') AND s.width IS NOT NULL
             AND s.exif_dt IS NULL"""
    ).fetchone()[0]
    if без_exif:
        print(f"[exif] уже разобрано и без даты съёмки: {без_exif:,} "
              "— повторно не читаются")
    print(f"\n[exif] изображений к разбору: {len(items):,}")

    def norm(v):
        if not v:
            return None
        v = str(v).strip().replace("\x00", "")
        if len(v) >= 19 and v[4] == ":" and v[7] == ":":
            v = v[:4] + "-" + v[5:7] + "-" + v[8:]
        return v or None

    def worker(it):
        fid, drive, rel = it
        p = fullpath(rts, drive, rel)
        try:
            with Image.open(p) as im:
                w, h = im.size
                ex = im.getexif() or {}
                dt = norm(ex.get(T_DT)) or norm(ex.get(306))
                mk = norm(ex.get(T_MK))
                md = norm(ex.get(T_MD))
                sn = norm(ex.get(T_SN))
            return fid, {
                "width": w, "height": h, "exif_dt": dt,
                "cam_make": mk, "cam_model": md, "exif_serial": sn,
                "status": "ok",
            }
        except Exception as e:  # noqa: BLE001
            return fid, {"status": "error", "note": f"exif: {e.__class__.__name__}"}

    st = run_pool(items, worker, nthreads, "exif", con)
    # Прервали — значит прервали. Раньше здесь безусловно стартовала стадия
    # video, и после Ctrl+C скрипт продолжал работать: со стороны выглядело
    # так, будто он не реагирует на прерывание.
    if st["interrupted"]:
        print("  стадия video пропущена: прогон прерван")
        return st
    stage_video_meta(con, rts, min(nthreads, 4))
    return st


def stage_video_meta(con, rts, nthreads):
    from shutil import which

    exe = which("ffprobe")
    if not exe:
        print("[video] ffprobe не найден — длительность видео пропущена (не критично)")
        return None
    items = con.execute(
        """SELECT f.id, f.drive, f.relpath FROM files f
           LEFT JOIN sigs s ON s.file_id=f.id
           WHERE f.kind='video' AND s.duration IS NULL
             AND (s.status IS NULL OR s.status='ok')"""
    ).fetchall()
    print(f"\n[video] роликов к разбору: {len(items):,}")

    def worker(it):
        fid, drive, rel = it
        p = fullpath(rts, drive, rel)
        try:
            # exe — абсолютный путь (не полагаемся на PATH),
            # путь к файлу передаётся через -i: иначе имя, начинающееся
            # с дефиса, было бы разобрано ffprobe как опция.
            # shell=False (по умолчанию), аргументы списком — инъекция команд
            # невозможна.
            out = subprocess.run(  # noqa: S603
                [exe, "-v", "error", "-show_entries",
                 "format=duration:stream=width,height",
                 "-of", "default=nw=1:nk=1", "-i", p],
                capture_output=True, text=True, timeout=60,
                shell=False, check=False,
            ).stdout.split()
            vals = [float(x) for x in out if x.replace(".", "", 1).isdigit()]
            # duration проставляется ВСЕГДА, даже нулём: та же ловушка, что
            # была в стадии exif — иначе ролик, у которого ffprobe не смог
            # определить длительность, переспрашивался бы при каждом запуске.
            d = {"status": "ok", "duration": 0.0}
            if vals:
                d["duration"] = round(max(vals), 2)
                if len(vals) >= 3:
                    d["width"], d["height"] = int(vals[0]), int(vals[1])
            return fid, d
        except Exception as e:  # noqa: BLE001
            return fid, {"status": "error", "note": f"ffprobe: {e.__class__.__name__}"}

    return run_pool(items, worker, nthreads, "video", con)


THUMB_SIZE = 240


def stage_thumb(con, rts, nthreads, size=THUMB_SIZE):
    """
    Превью для визуальной сверки — только там, где файлы РАЗЛИЧАЮТСЯ.

    Точные копии (метод exact) исключены намеренно: они побайтово идентичны,
    и разглядывать там нечего. Смотреть надо группы exif и phash, где решение
    без картинки принять нельзя.

    Требует, чтобы группы уже были посчитаны: сначала 03_report.py, потом эта
    стадия, потом 03_report.py ещё раз — уже с превью.
    """
    try:
        from PIL import Image
    except ImportError:
        print("[thumb] нет Pillow -> .venv/bin/pip install pillow ; пропуск")
        return None
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS

    from shutil import which
    ffmpeg = which("ffmpeg")

    items = con.execute(
        """SELECT DISTINCT f.id, f.drive, f.relpath, f.kind, f.size
           FROM group_members gm
           JOIN groups g ON g.id = gm.group_id
           JOIN files f ON f.id = gm.file_id
           LEFT JOIN thumbs t ON t.file_id = f.id
           WHERE g.method IN ('exif','phash') AND t.file_id IS NULL
           ORDER BY f.size"""
    ).fetchall()

    всего_групп = con.execute(
        "SELECT COUNT(*) FROM groups WHERE method IN ('exif','phash')"
    ).fetchone()[0]
    if not всего_групп:
        print("[thumb] групп exif/phash нет — сначала запустите 03_report.py")
        return None
    print(f"\n[thumb] файлов к превью: {len(items):,} "
          f"(из {всего_групп:,} групп, где файлы различаются)")
    if not ffmpeg:
        print("[thumb] ffmpeg не найден — кадры из видео извлечь не удастся")

    import io

    def worker(it):
        fid, drive, rel, kind, _size = it
        p = fullpath(rts, drive, rel)
        try:
            if kind == "video":
                if not ffmpeg:
                    return fid, {"status": "error", "note": "нет ffmpeg"}
                r = subprocess.run(  # noqa: S603
                    [ffmpeg, "-v", "error", "-ss", "1", "-i", p,
                     "-frames:v", "1", "-vf", f"scale={size}:-1",
                     "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                    capture_output=True, timeout=90, check=False, shell=False)
                if not r.stdout:
                    return fid, {"status": "error", "note": "кадр не извлечён"}
                return fid, {"jpeg": r.stdout, "status": "ok"}
            with Image.open(p) as im:
                im.draft("RGB", (size * 2, size * 2))
                im = im.convert("RGB")
                im.thumbnail((size, size))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=78)
                return fid, {"jpeg": buf.getvalue(), "w": im.width,
                             "h": im.height, "status": "ok"}
        except Exception as e:  # noqa: BLE001
            return fid, {"status": "error", "note": f"thumb: {e.__class__.__name__}"}

    # Вес — размер файла: стоимость превью различается стократно между
    # мелким JPEG и роликом на 40 МБ, и счётчик файлов о прогрессе не говорит
    # ничего. Видео дороже даже при равном размере (запуск ffmpeg с
    # перемоткой), поэтому ему добавляется фиксированная надбавка.
    def вес(it):
        return it[4] + (8 << 20 if it[3] == "video" else 0)

    видео = sum(1 for i in items if i[3] == "video")
    if видео:
        print(f"[thumb] из них видео: {видео:,} — они заметно дороже картинок")
    return run_pool(items, worker, nthreads, "thumb", con,
                    weight=вес, upsert_fn=upsert_thumb)


def upsert_thumb(con, file_id, **kw):
    kw.setdefault("made_at", time.time())
    cols = ", ".join(kw)
    ph = ", ".join("?" * len(kw))
    upd = ", ".join(f"{c}=excluded.{c}" for c in kw)
    con.execute(
        f"INSERT INTO thumbs (file_id, {cols}) VALUES (?, {ph}) "  # noqa: S608
        f"ON CONFLICT(file_id) DO UPDATE SET {upd}",
        (file_id, *kw.values()),
    )


def stage_phash(con, rts, nthreads, max_side=0, min_size=1024):
    try:
        import imagehash
        from PIL import Image
    except ImportError:
        print("[phash] нет imagehash -> .venv/bin/pip install imagehash pillow"
              " ; стадия пропущена, остальное работает")
        return None
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        pass

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    q = """SELECT f.id, f.drive, f.relpath FROM files f
           LEFT JOIN sigs s ON s.file_id=f.id
           WHERE f.kind IN ('image','raw') AND f.size>=? AND s.phash IS NULL
             AND (s.status IS NULL OR s.status='ok')"""
    if max_side:
        q += f" AND (s.width IS NULL OR s.width<={max_side*20})"
    items = con.execute(q, (min_size,)).fetchall()

    # Раньше порог 1024 байта был зашит в запрос молча: изображения меньше
    # килобайта просто не получали pHash, и нигде об этом не говорилось.
    skipped = con.execute(
        """SELECT COUNT(*) FROM files f LEFT JOIN sigs s ON s.file_id=f.id
           WHERE f.kind IN ('image','raw') AND f.size<? AND s.phash IS NULL""",
        (min_size,)).fetchone()[0]
    print(f"\n[phash] изображений к декодированию: {len(items):,}")
    if skipped:
        print(f"[phash] пропущено как слишком мелкие (<{min_size} Б): {skipped:,}"
              f" — изменить: --min-size")
    print("[phash] это самая долгая стадия; прерывание Ctrl+C безопасно")

    def worker(it):
        fid, drive, rel = it
        p = fullpath(rts, drive, rel)
        try:
            with Image.open(p) as im:
                im.draft("RGB", (256, 256))  # быстрый путь для JPEG
                im = im.convert("L")
                return fid, {
                    "phash": str(imagehash.phash(im, hash_size=8)),
                    "dhash": str(imagehash.dhash(im, hash_size=8)),
                    "status": "ok",
                }
        except Exception as e:  # noqa: BLE001
            return fid, {"status": "error", "note": f"phash: {e.__class__.__name__}"}

    return run_pool(items, worker, nthreads, "phash", con)


# ------------------------------------------------------------------ план


def plan(con):
    print("=" * 68)
    print("ПЛАН ЭТАПА 2 (оценка по метаданным, диски не читаются)")
    print("=" * 68)
    ex = select_exact(con)
    ex_bytes = sum(i[3] for i in ex)
    img = con.execute(
        "SELECT COUNT(*) FROM files WHERE kind IN ('image','raw')"
    ).fetchone()[0]
    vid = con.execute("SELECT COUNT(*), SUM(size) FROM files WHERE kind='video'").fetchone()
    print(f"exact : {len(ex):,} файлов, прочитать {L.human(ex_bytes)}")
    print(f"        при 80 МБ/с по сети ~ {ex_bytes/80e6/60:.0f} мин")
    print(f"exif  : {img:,} изображений, читается только заголовок")
    print(f"        при 60 файл/с ~ {img/60/60:.0f} мин")
    print(f"phash : {img:,} изображений, полное декодирование")
    print(f"        при 25 файл/с ~ {img/25/60:.0f} мин")
    print(f"video : {vid[0]:,} роликов, {L.human(vid[1] or 0)} (ffprobe, только заголовки)")
    print("\nОценки грубые: скорость сетевого диска решает всё. Начните с exact.")


STAGES = ("exact", "exif", "phash", "video", "thumb", "all")


def normalize_stage_args(argv):
    """
    Позволяет писать --exact вместо --stage exact.

    Форма `--stage exact` логична для разработчика и неудобна для рук: промах
    на `--exact` естественен и до этого приводил лишь к сухому
    «unrecognized arguments». Принимаем обе формы, о подмене сообщаем.
    """
    out = []
    for a in argv:
        name = a[2:] if a.startswith("--") else None
        if name in STAGES:
            print(f"(принято как --stage {name})")
            out += ["--stage", name]
        else:
            out.append(a)
    return out


_interrupts = 0


def install_sigint_handler():
    """
    Первое Ctrl+C — аккуратная остановка: досчитать текущее, закоммитить,
    записать журнал. Второе — выход немедленно.

    Второй предохранитель нужен потому, что «аккуратно» иногда означает
    «подождите, идёт запись», а пользователь в этот момент не понимает,
    реагирует скрипт или нет.

    Данные при жёстком выходе не портятся: SQLite коммитит пачками, теряется
    максимум последняя незакоммиченная пачка, которая пересчитается при
    следующем запуске.
    """
    def handler(_signum, _frame):
        global _interrupts
        _interrupts += 1
        if _interrupts == 1:
            print("\n[Ctrl+C] останавливаюсь аккуратно… "
                  "нажмите ещё раз для немедленного выхода", flush=True)
            raise KeyboardInterrupt
        print("\n[Ctrl+C x2] выхожу немедленно; "
              "посчитанное сохранено, прогресс не потерян", flush=True)
        os._exit(130)

    signal.signal(signal.SIGINT, handler)


def main():
    install_sigint_handler()
    sys.argv[1:] = normalize_stage_args(sys.argv[1:])
    ap = argparse.ArgumentParser(
        epilog="Стадию можно указывать и как --stage exact, и как --exact.")
    L.add_version_arg(ap)
    ap.add_argument("--db", default=None)
    ap.add_argument("--stage", choices=list(STAGES),
                    default="all")
    ap.add_argument("--threads", type=int, default=8,
                    help="параллельные чтения; на сетевом диске 8-16 обычно оптимум")
    ap.add_argument("--min-size", type=int, default=1024, metavar="БАЙТ",
                    help="не считать pHash для файлов мельче (по умолчанию 1024); "
                         "пропущенные всегда показываются в отчёте стадии")
    ap.add_argument("--recompute", action="store_true",
                    help="пересчитать стадию заново, сбросив прежние значения "
                         "(нужно после смены алгоритма стадии)")
    ap.add_argument("--thumb-size", type=int, default=THUMB_SIZE,
                    metavar="ПИКС", help="сторона превью (по умолчанию 240)")
    ap.add_argument("--log", metavar="ПУТЬ",
                    help="журнал прогонов; по умолчанию reports/signatures_log.csv")
    ap.add_argument("--no-log", action="store_true", help="не вести журнал")
    ap.add_argument("--plan", action="store_true", help="только оценка, без чтения")
    a = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    db = a.db or os.path.join(here, "..", "data", "inventory.db")
    con = L.connect(db)
    L.init_db(con)

    if a.plan:
        plan(con)
        return

    if a.stage in ("exif", "phash", "all"):
        L.venv_notice()

    rts = roots(con)
    if not rts:
        print("в базе нет корней — сначала запустите 01_inventory.py")
        return
    for k, v in rts.items():
        if not os.path.isdir(v):
            print(f"!! диск не смонтирован: {k} -> {v}")
            return

    log_path = a.log or os.path.join(here, "..", "reports", "signatures_log.csv")
    t0 = time.time()
    interrupted = False

    def run_stage(fn, *args, **kw):
        """Обёртка: время, журнал, сводка — одинаково для всех стадий."""
        nonlocal interrupted
        started = dt.datetime.now()
        try:
            stat = fn(*args, **kw)
        except KeyboardInterrupt:
            interrupted = True
            raise
        finished = dt.datetime.now()
        if stat and (stat["done"] or stat["interrupted"]):
            print_summary(stat, started, finished, os.path.abspath(log_path))
            if not a.no_log:
                log_run(log_path, stat, started, finished, a.threads, db)
        if stat and stat["interrupted"]:
            interrupted = True
        return stat

    if a.stage in ("exact", "all") and check_recipe(con, "exact", a.recompute):
        run_stage(stage_exact, con, rts, a.threads)
    if not interrupted and a.stage in ("exif", "all") \
            and check_recipe(con, "exif", a.recompute):
        run_stage(stage_exif, con, rts, a.threads)
    if not interrupted and a.stage == "video":
        run_stage(stage_video_meta, con, rts, min(a.threads, 4))
    if not interrupted and a.stage in ("phash", "all") \
            and check_recipe(con, "phash", a.recompute):
        run_stage(stage_phash, con, rts, a.threads, min_size=a.min_size)
    # thumb не входит в "all": он требует уже посчитанных групп,
    # то есть запускается ПОСЛЕ 03_report.py
    if not interrupted and a.stage == "thumb":
        run_stage(stage_thumb, con, rts, a.threads, size=a.thumb_size)

    L.set_meta(con, "signatures_finished", time.strftime("%Y-%m-%d %H:%M:%S"))
    con.commit()
    print(f"\nвсего {L.dur(time.time()-t0)}")
    if interrupted:
        print("прогон прерван; повторный запуск продолжит с места остановки")
    else:
        print("следующий шаг:  python3 03_report.py")
    con.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nостановлено пользователем")
