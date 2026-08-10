"""
mmdlib.py — общие утилиты конвейера инвентаризации MMD-2026.

ВСЕ операции строго read-only по отношению к сетевым дискам.
Ни одна функция здесь не открывает файл на запись, не удаляет и не переименовывает.
"""

import os
import sqlite3
import sys

# Версия конвейера. Одна на весь проект, а не по версии на скрипт: скрипты
# передают работу друг другу через одну базу, и «02 версии 1.3 с 03 версии 1.1»
# — состояние, которое незачем разрешать. Меняется вручную при изменении
# поведения, видимого пользователю.
#
# SCHEMA_VERSION живёт отдельно и означает другое: совместимость файла базы.
# База переживает обновление скриптов, поэтому её версия растёт реже.
VERSION = "1.0.0"

SCHEMA_VERSION = 7


def version_line() -> str:
    """Строка для --version и для штампа в отчётах."""
    return f"MMD-2026 duplicate finder {VERSION} (схема базы {SCHEMA_VERSION})"


def add_version_arg(ap) -> None:
    """
    --version у каждого скрипта. Без него единственный способ понять, что
    у коллеги за копия, — сверять файлы глазами.
    """
    ap.add_argument("--version", action="version", version=version_line())


def _setup_console():
    """
    Windows: консоль по умолчанию в cp866 или cp1251, и символы вроде ✓ ✗ ▶
    вызывают UnicodeEncodeError прямо посреди работы. Переключаем вывод
    на UTF-8, а нечитаемое заменяем, а не роняем процесс.

    На macOS и Linux ничего не меняется: там UTF-8 и так.
    """
    if os.name != "nt":
        return
    import contextlib
    for поток in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            поток.reconfigure(encoding="utf-8", errors="replace")


_setup_console()


def venv_notice():
    """
    Правило проекта: сторонние пакеты ставятся ТОЛЬКО в ../.venv.
    Системный Python не трогаем никогда — ни `pip3 install`,
    ни тем более `--break-system-packages`.

    Функция ничего не запрещает и не устанавливает, только предупреждает,
    если скрипт запущен мимо venv.
    """
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if in_venv:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    # venv ищем и рядом со скриптами (scripts/.venv — наш случай),
    # и в корне проекта: жёсткий путь тут только вводил бы в заблуждение
    candidates = [os.path.join(here, ".venv"),
                  os.path.normpath(os.path.join(here, "..", ".venv"))]
    venv = next((v for v in candidates
                 if os.path.isfile(os.path.join(v, "bin", "python"))),
                candidates[0])
    py = os.path.join(venv, "bin", "python")
    print("  ! запущено системным Python, не из venv проекта")
    if os.path.isfile(py):
        print(f"  ! venv уже создан — запускайте так:  {py} {os.path.basename(sys.argv[0])}")
    else:
        print("  ! создать один раз:")
        print(f"  !   python3 -m venv {venv}")
        print(f"  !   {os.path.join(venv,'bin','pip')} install -r "
              f"{os.path.normpath(os.path.join(here,'..','requirements.txt'))}")
    print("  ! ставить пакеты в системный Python не нужно: без них работают")
    print("  ! этап 1, этап 2 --stage exact и этап 3 — отпадают только EXIF и pHash")
    print()

# ---------------------------------------------------------------- классы медиа

IMAGE_EXT = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".heic", ".heif", ".avif", ".psd", ".ico",
    # добавлено по итогам первого прогона: .tga лежал в 'other' и потому
    # полностью выпадал из поиска дублей
    ".tga", ".jp2", ".jpf", ".pcx", ".ppm", ".pgm", ".xcf", ".svg",
}

# Архивы выделены в отдельный класс намеренно: внутри может лежать фотоархив
# (у нас нашлись Япония.part1-4.rar на 2.4 ГБ). Скрипты внутрь не заглядывают,
# но в отчёте такие файлы видно отдельно, а не в общей куче 'other'.
ARCHIVE_EXT = {
    ".rar", ".zip", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz",
    ".dmg", ".iso", ".sit", ".sitx",
}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wma", ".aif", ".aiff"}
RAW_EXT = {
    ".raw", ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".orf",
    ".rw2", ".raf", ".dng", ".pef", ".x3f", ".erf", ".3fr", ".mrw", ".kdc",
}
VIDEO_EXT = {
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".wmv", ".mpg", ".mpeg", ".m2ts",
    ".mts", ".ts", ".3gp", ".3g2", ".flv", ".webm", ".vob", ".ogv", ".mod",
}
SIDECAR_EXT = {".xmp", ".aae", ".thm", ".pp3", ".dop", ".on1", ".lrv"}

# Служебный мусор — считаем отдельно, в дедупликации не участвует.
JUNK_NAMES = {
    ".ds_store", "thumbs.db", "desktop.ini", "picasa.ini", ".picasa.ini",
    "zbthumbnail.info", ".apdisk", "album.dat", "albumdata.xml",
}
JUNK_DIR_PARTS = {
    "#recycle", "#snapshot", ".@__thumb", "@eadir", ".thumbnails",
    ".spotlight-v100", ".fseventsd", ".trashes", ".trash",
    "lightroom catalog previews.lrdata", "caches",
}


def classify(name: str) -> tuple[str, str]:
    """-> (класс, расширение в нижнем регистре)"""
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if low in JUNK_NAMES or low.startswith("._"):
        return "junk", ext
    if ext in IMAGE_EXT:
        return "image", ext
    if ext in RAW_EXT:
        return "raw", ext
    if ext in VIDEO_EXT:
        return "video", ext
    if ext in SIDECAR_EXT:
        return "sidecar", ext
    if ext in ARCHIVE_EXT:
        return "archive", ext
    if ext in AUDIO_EXT:
        return "audio", ext
    return "other", ext


def is_junk_dir(dirname: str) -> bool:
    return dirname.lower() in JUNK_DIR_PARTS or dirname.startswith("@eaDir")


# ---------------------------------------------------------------- база данных


# ------------------------------------------------------------- конфигурация

CONFIG_NAME = "config.json"


def project_dir() -> str:
    """Корень проекта — родитель каталога scripts/."""
    return os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def config_path() -> str:
    return os.path.join(project_dir(), CONFIG_NAME)


def load_config(required: bool = True) -> dict:
    """
    Читает config.json — единственное место, где живут пути к вашим дискам.

    Ни один скрипт не содержит путей: инструмент рассчитан на то, что его
    возьмут коллеги с совершенно другой раскладкой каталогов.

    Формат:
        {
          "drives": {"photo": "/Volumes/photo", "archive": "/mnt/old"},
          "target": "photo"
        }

    Ключи словаря drives — короткие логические имена, они попадают в базу и
    отчёты. Значение "target" указывает, какой из дисков считается целевым:
    при выборе «кого оставить» файл на нём получает преимущество.
    """
    import json

    p = config_path()
    if not os.path.isfile(p):
        if not required:
            return {"drives": {}, "target": None}
        raise SystemExit(
            f"Не найден {CONFIG_NAME}.\n"
            f"  Ожидается здесь: {p}\n"
            f"  Скопируйте образец и впишите свои пути:\n"
            f"    cp {os.path.join(project_dir(), 'config.example.json')} {p}\n"
            f"  Либо задайте диски флагами --root ИМЯ=ПУТЬ."
        )
    try:
        with open(p, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as e:
        raise SystemExit(f"{CONFIG_NAME} не читается: {e}") from e

    drives = cfg.get("drives") or {}
    if not isinstance(drives, dict) or not drives:
        raise SystemExit(f"{CONFIG_NAME}: раздел 'drives' пуст или не словарь")
    bad = [k for k in drives if not str(k).strip()]
    if bad:
        raise SystemExit(f"{CONFIG_NAME}: пустые имена дисков")
    target = cfg.get("target")
    if target and target not in drives:
        raise SystemExit(
            f"{CONFIG_NAME}: target='{target}' не найден среди дисков "
            f"({', '.join(drives)})")
    return {"drives": {str(k): str(v) for k, v in drives.items()},
            "target": target or next(iter(drives))}


def merge_roots(cfg_drives: dict, root_args) -> dict:
    """Флаги --root ИМЯ=ПУТЬ дополняют и переопределяют конфигурацию."""
    out = dict(cfg_drives)
    for spec in root_args or []:
        name, _, path = str(spec).partition("=")
        if not name or not path:
            raise SystemExit(f"--root ожидает ИМЯ=ПУТЬ, получено: {spec!r}")
        out[name] = path
    return out


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        -- Этап 1: чистые метаданные файловой системы. Содержимое не читается.
        CREATE TABLE IF NOT EXISTS files (
            id        INTEGER PRIMARY KEY,
            drive     TEXT NOT NULL,     -- логическое имя корня: photo | 300photos
            relpath   TEXT NOT NULL,     -- путь относительно корня диска
            reldir    TEXT NOT NULL,
            name      TEXT NOT NULL,
            ext       TEXT NOT NULL,
            kind      TEXT NOT NULL,     -- image|raw|video|sidecar|junk|other
            size      INTEGER NOT NULL,
            mtime     REAL NOT NULL,
            depth     INTEGER NOT NULL,
            seen      REAL,              -- метка последнего обхода, для --prune
            UNIQUE (drive, relpath)
        );
        CREATE INDEX IF NOT EXISTS ix_files_size  ON files (size);
        CREATE INDEX IF NOT EXISTS ix_files_kind  ON files (kind);
        CREATE INDEX IF NOT EXISTS ix_files_name  ON files (name);
        CREATE INDEX IF NOT EXISTS ix_files_dir   ON files (drive, reldir);

        -- Прогресс обхода: позволяет прервать и продолжить.
        CREATE TABLE IF NOT EXISTS scanned_dirs (
            drive   TEXT NOT NULL,
            reldir  TEXT NOT NULL,
            nfiles  INTEGER NOT NULL,
            dmtime  REAL,          -- mtime самого каталога на момент обхода
            PRIMARY KEY (drive, reldir)
        );

        -- Что не удалось прочитать. Без этой таблицы неполный обход выглядел бы
        -- как полный: файлы просто не попали бы в инвентарь, и никто бы не узнал.
        CREATE TABLE IF NOT EXISTS scan_errors (
            drive  TEXT NOT NULL,
            path   TEXT NOT NULL,
            what   TEXT NOT NULL,   -- dir | file | depth_limit | symlink
            err    TEXT,
            PRIMARY KEY (drive, path, what)
        );

        -- Этап 2: результат чтения содержимого. Заполняется только для кандидатов.
        CREATE TABLE IF NOT EXISTS sigs (
            file_id     INTEGER PRIMARY KEY REFERENCES files(id),
            sha256      TEXT,      -- полный хеш содержимого
            head_sha    TEXT,      -- хеш первых 256 КБ (дешёвый префильтр)
            phash       TEXT,      -- perceptual hash изображения (hex)
            dhash       TEXT,
            width       INTEGER,
            height      INTEGER,
            exif_dt     TEXT,      -- DateTimeOriginal, 'YYYY-MM-DD HH:MM:SS'
            cam_make    TEXT,
            cam_model   TEXT,
            exif_serial TEXT,
            duration    REAL,      -- для видео, секунды
            status      TEXT,      -- ok | error | skipped
            note        TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_sigs_sha    ON sigs (sha256);
        CREATE INDEX IF NOT EXISTS ix_sigs_phash  ON sigs (phash);
        CREATE INDEX IF NOT EXISTS ix_sigs_exifdt ON sigs (exif_dt);

        -- Этап 3: найденные группы. Ничего не удаляет, только помечает.
        CREATE TABLE IF NOT EXISTS groups (
            id       INTEGER PRIMARY KEY,
            method   TEXT NOT NULL,   -- exact | exif | phash
            gkey     TEXT NOT NULL,
            n        INTEGER NOT NULL,
            bytes    INTEGER NOT NULL,
            waste    INTEGER NOT NULL -- сколько байт освободится, если оставить одного
        );
        CREATE TABLE IF NOT EXISTS group_members (
            group_id INTEGER NOT NULL REFERENCES groups(id),
            file_id  INTEGER NOT NULL REFERENCES files(id),
            keep     INTEGER NOT NULL DEFAULT 0,  -- 1 = рекомендуется оставить
            reason   TEXT,
            PRIMARY KEY (group_id, file_id)
        );
        CREATE INDEX IF NOT EXISTS ix_gm_file ON group_members (file_id);

        -- Превью для визуальной сверки. Хранятся в базе, а не файлами:
        -- отчёт тогда собирается без обращения к сетевым дискам, а превью
        -- переживают перегенерацию отчёта и переезд папки reports.
        --
        -- Делаются ТОЛЬКО для групп exif и phash: точные копии побайтово
        -- идентичны, смотреть там нечего.
        CREATE TABLE IF NOT EXISTS thumbs (
            file_id INTEGER PRIMARY KEY REFERENCES files(id),
            w       INTEGER,
            h       INTEGER,
            jpeg    BLOB,
            status  TEXT,          -- ok | error
            note    TEXT,
            made_at REAL
        );
        """
    )
    # мягкая миграция старых баз: добавляем колонку, не теряя данные
    cols = {r[1] for r in con.execute("PRAGMA table_info(files)")}
    if "seen" not in cols:
        con.execute("ALTER TABLE files ADD COLUMN seen REAL")
    cols = {r[1] for r in con.execute("PRAGMA table_info(scanned_dirs)")}
    if "dmtime" not in cols:
        # NULL означает «обойдено старой версией, mtime неизвестен» —
        # такой каталог будет перепроверен при следующем обходе
        con.execute("ALTER TABLE scanned_dirs ADD COLUMN dmtime REAL")

    con.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    # Какая версия последней писала в базу. Обход длится часами, а скрипты
    # за это время обновляются: без штампа причину странных данных
    # восстановить нечем.
    con.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('app_version', ?)",
        (VERSION,),
    )
    con.commit()


def get_meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(con, key, value):
    con.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value))
    )


# ------------------------------------------------- единая оценка оригинальности

# Папки и имена, выдающие производный файл, а не оригинал.
DERIVED_PATH_MARKERS = (
    "копия", "copy", "thumb", "preview", "resize", "web", "export",
    "whatsapp", "telegram", "viber", "screenshot", "скриншот", "downloads",
)
DERIVED_NAME_RE = r"(_\d{1,2}|\(\d+\)|[ _-]copy|[ _-]копия|[ _-]small|[ _-]web)$"


def originality_key(*, path, size, mtime, drive=None, target_drive=None,
                    width=None, height=None, exif_dt=None, dt_match=None,
                    depth=None):
    """
    Единый порядок «кто ближе к оригиналу». Меньше — вероятнее оригинал.

    Используется и поиском (00), и отчётом (03): раньше каждый скрипт судил
    по своим правилам, и они расходились в выводах на одних и тех же файлах.

    Порядок признаков осмысленный, а не случайный:
      1. сверка с известной датой съёмки, если она задана — сильнее всего;
         'противоречит' отбрасывает кандидата вниз, даже если он крупнее всех
      2. файл на целевом диске
      3. путь без признаков производной (WhatsApp, Screenshots, копия…)
      4. имя без суффиксов _2, (1), copy, small
      5. НАЛИЧИЕ EXIF-даты съёмки — оригинал от камеры её сохраняет,
         а пересохранение мессенджером или редактором обычно стирает
      6. больше пикселей
      7. больше байт
      8. более ранняя дата (EXIF, если есть; иначе mtime)
      9. короче путь
    """
    import re as _re

    low = str(path).lower()
    junky = any(s in low for s in DERIVED_PATH_MARKERS)
    stem = os.path.splitext(os.path.basename(low))[0]
    derived = bool(_re.search(DERIVED_NAME_RE, stem))

    when = mtime
    if exif_dt:
        try:
            import time as _t

            when = _t.mktime(_t.strptime(str(exif_dt).strip()[:19],
                                         "%Y:%m:%d %H:%M:%S"))
        except (ValueError, TypeError):
            pass

    return (
        {"совпадает": 0, "не задано": 1, "нет данных": 2,
         "противоречит": 9}.get(dt_match, 1),
        0 if (target_drive and drive == target_drive) else 1,
        1 if junky else 0,
        1 if derived else 0,
        0 if exif_dt else 1,
        -((width or 0) * (height or 0)),
        -size,
        when,
        depth if depth is not None else str(path).count(os.sep),
        str(path),
    )


# ------------------------------------------------------ безопасный вывод

def csv_safe(v):
    """
    Защита от formula injection в Excel/Numbers.

    Ячейка, начинающаяся с = + - @ (а также с табуляции или CR), трактуется
    табличным процессором как формула. Имя файла вида
        =cmd|' /C calc'!A1.jpg
    превращается в DDE-вызов при открытии отчёта. Имена файлов на дисках
    контролируются не нами, а отчёты README прямо предлагает открывать в Excel.

    Обезвреживаем апострофом: Excel показывает содержимое как текст,
    сама строка остаётся читаемой.
    """
    if v is None:
        return ""
    s = str(v)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def csv_row(values):
    return [csv_safe(v) for v in values]


_MD_ESCAPE = str.maketrans({
    "[": r"\[", "]": r"\]", "(": r"\(", ")": r"\)",
    "<": r"\<", ">": r"\>", "`": r"\`", "|": r"\|",
    "*": r"\*", "_": r"\_", "\r": " ", "\n": " ",
})


def md_text(v):
    """
    Экранирование текста, попадающего в markdown.

    Без него имя файла  brk](http://evil.tld) [x.jpg  разрывает ссылку
    и подставляет свою: в Obsidian это кликабельная ссылка наружу.
    Перевод строки в имени ломает разметку списка.
    """
    return str(v).translate(_MD_ESCAPE)


def dur(seconds: float) -> str:
    """98.3 минуты читается хуже, чем «1 ч 38 мин»."""
    s = int(seconds)
    h, m, s = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h} ч {m} мин"
    if m:
        return f"{m} мин {s} с"
    return f"{s} с"


def plural(n: int, one: str, few: str, many: str) -> str:
    """«2 файлов» режет глаз. plural(2,'файл','файла','файлов') -> 'файла'."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def nfiles(n: int) -> str:
    return f"{n:,} {plural(n, 'файл', 'файла', 'файлов')}"


def ngroups(n: int) -> str:
    return f"{n:,} {plural(n, 'группа', 'группы', 'групп')}"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n} B"
