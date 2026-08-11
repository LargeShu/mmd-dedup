"""
Регрессионные тесты: по одному на каждый реально пойманный дефект.

Правило: нашли баг — сначала тест, воспроизводящий его, потом исправление.
Так дефект не может вернуться незамеченным.

Тесты трогают файловую систему и SQLite, но только во временном каталоге.
К сетевым дискам не обращаются.

Запуск:  .venv/bin/python -m unittest discover -s tests -v
"""

import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)
import mmdlib  # noqa: E402,F401  — гарантирует, что scripts/ в sys.path

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


def run(script, *args):
    """Запускает скрипт конвейера тем же интерпретатором."""
    # sys.executable и наши же скрипты; shell=False. Внешнего ввода нет.
    r = subprocess.run(  # noqa: S603
        [sys.executable, os.path.join(SCRIPTS, script), *args],
        capture_output=True, text=True, timeout=300, check=False)
    return r.stdout + r.stderr


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mmd_regress_")
        self.photo = os.path.join(self.tmp, "photo")
        self.other = os.path.join(self.tmp, "300")
        os.makedirs(os.path.join(self.photo, "A"))
        os.makedirs(self.other)
        self.db = os.path.join(self.tmp, "t.db")
        self.rep = os.path.join(self.tmp, "rep")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def img(self, rel, w=120, h=90, exif=None):
        p = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        im = Image.new("RGB", (w, h), (30, 60, 90))
        if exif:
            e = im.getexif()
            for k, v in exif.items():
                e[k] = v
            im.save(p, exif=e)
        else:
            im.save(p)
        return p

    def inventory(self, *extra):
        return run("01_inventory.py", "--db", self.db, "--no-csv", "--quiet",
                   "--root", f"photo={self.photo}",
                   "--root", f"300photos={self.other}", *extra)

    def q(self, sql, *p):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, p).fetchone()[0]
        finally:
            con.close()


@unittest.skipUnless(HAVE_PIL, "нужен Pillow")
class TestIdPreserved(Base):
    """
    Дефект: INSERT OR REPLACE менял files.id при каждом повторном обходе,
    из-за чего sigs (часы хеширования) теряли привязку к файлам.
    """

    def test_id_не_меняются_и_sigs_не_сиротеют(self):
        self.img("photo/A/a.jpg")
        self.img("photo/A/b.jpg", 200, 150)
        self.inventory()
        before = dict(sqlite3.connect(self.db)
                      .execute("SELECT relpath, id FROM files").fetchall())
        run("02_signatures.py", "--db", self.db, "--stage", "exif",
            "--threads", "2")
        sigs_before = self.q("SELECT COUNT(*) FROM sigs")

        self.inventory()   # повторный обход

        after = dict(sqlite3.connect(self.db)
                     .execute("SELECT relpath, id FROM files").fetchall())
        self.assertEqual(before, after, "id файлов изменились при повторе")
        self.assertEqual(self.q("SELECT COUNT(*) FROM sigs"), sigs_before)
        self.assertEqual(
            self.q("SELECT COUNT(*) FROM sigs s "
                   "LEFT JOIN files f ON f.id=s.file_id WHERE f.id IS NULL"),
            0, "появились осиротевшие сигнатуры")


@unittest.skipUnless(HAVE_PIL, "нужен Pillow")
class TestIncremental(Base):
    """
    Дефект: включение пропуска каталогов по факту «уже обойдён» теряло
    новые файлы, добавленные в старую папку.
    """

    def test_новый_файл_в_старой_папке_замечен(self):
        self.img("photo/A/a.jpg")
        self.inventory()
        time.sleep(1.1)                     # чтобы mtime каталога изменился
        self.img("photo/A/новый.jpg")
        out = self.inventory()
        self.assertEqual(self.q("SELECT COUNT(*) FROM files WHERE name=?",
                                "новый.jpg"), 1, out)

    def test_неизменившиеся_каталоги_пропускаются(self):
        self.img("photo/A/a.jpg")
        self.inventory()
        out = self.inventory()
        self.assertIn("пропущено без изменений", out)


@unittest.skipUnless(HAVE_PIL, "нужен Pillow")
class TestScanErrors(Base):
    """
    Дефект: scan_errors не очищались, и после -L 2 -> -L 3 отчёт продолжал
    считать инвентарь неполным, хотя ветви уже были обойдены.
    """

    def test_обрезанные_ветви_не_накапливаются(self):
        # уровней должно быть больше, чем -L, иначе резать нечего:
        # A=1, deep=2, deeper=3 — при -L 2 обрезается ветвь deeper
        self.img("photo/A/deep/deeper/x.jpg")
        self.inventory("-L", "2")
        self.assertGreater(self.q("SELECT COUNT(*) FROM scan_errors "
                                  "WHERE what='depth_limit'"), 0)
        out = self.inventory()             # полный обход
        self.assertEqual(self.q("SELECT COUNT(*) FROM scan_errors "
                                "WHERE what='depth_limit'"), 0, out)
        self.assertIn("замечаний нет", out)


@unittest.skipUnless(HAVE_PIL, "нужен Pillow")
class TestReportInjection(Base):
    """
    Дефекты: имя файла попадало в CSV как формула Excel и разрывало
    markdown-ссылку в decision.md.
    """

    def setUp(self):
        super().setUp()
        for name in ("=cmd|' /C calc'.jpg", "+1+1.jpg", "@SUM(A1).jpg",
                     "brk](http://evil.tld) [x.jpg", "<script>alert(1)</script>.jpg"):
            try:
                self.img(f"photo/A/{name}")
                self.img(f"300/{name}")     # копия -> попадёт в группу дублей
            except OSError:
                pass
        self.inventory()
        run("02_signatures.py", "--db", self.db, "--stage", "all",
            "--threads", "2", "--min-size", "1")
        run("03_report.py", "--db", self.db, "--out", self.rep, "--md")

    def test_в_csv_нет_живых_формул(self):
        плохие = []
        for f in os.listdir(self.rep):
            if not f.endswith(".csv"):
                continue
            with open(os.path.join(self.rep, f), encoding="utf-8-sig") as fh:
                for row in csv.reader(fh, delimiter=";"):
                    плохие += [c for c in row
                               if c[:1] in ("=", "+", "-", "@", "\t", "\r")]
        self.assertEqual(плохие, [], f"опасные ячейки: {плохие[:3]}")

    def test_markdown_не_уводит_наружу(self):
        файлы = [f for f in os.listdir(self.rep)
                 if f.startswith("decision_") and f.endswith(".md")]
        if not файлы:
            self.skipTest("групп дублей не образовалось")
        for имя in файлы:
            with open(os.path.join(self.rep, имя), encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("- [ ] удалить"):
                        self.assertNotIn("](http", line.split("](file://")[0])

    def test_html_нельзя_разорвать_именем_файла(self):
        """
        Имя  <script>alert(1)</script>.jpg  попадает в JSON внутри тега
        <script>. Опасна только закрывающая последовательность: она обрывает
        блок и превращает данные в исполняемый код. Открывающий <script>
        внутри строки JS безвреден, поэтому проверяем именно разрыв.
        """
        with open(os.path.join(self.rep, "decision.html"),
                  encoding="utf-8") as fh:
            h = fh.read()
        self.assertEqual(h.count("</script>"), 1,
                         "блок script разорван именем файла")
        self.assertIn(r"<\/script>", h, "закрывающий тег не экранирован")


@unittest.skipUnless(HAVE_PIL, "нужен Pillow")
class TestKeepChoice(Base):
    """
    Дефект: в отчёте «оставить» выбиралось по разрешению, и пересохранённая
    копия без EXIF обходила оригинал от камеры.
    """

    def test_оригинал_с_exif_побеждает_апскейл(self):
        self.img("photo/A/original.jpg", 600, 400,
                 exif={36867: "2008:12:24 13:24:00", 272: "COOLPIX"})
        src = Image.open(os.path.join(self.tmp, "photo/A/original.jpg"))
        src.resize((1200, 800)).save(os.path.join(self.tmp, "300",
                                                  "upscaled_2.jpg"))
        self.inventory()
        run("02_signatures.py", "--db", self.db, "--stage", "all",
            "--threads", "2", "--min-size", "1")
        run("03_report.py", "--db", self.db, "--out", self.rep, "--md")
        # чек-листы теперь разложены по файлам методов, а decision.md —
        # оглавление
        keep = []
        for имя in os.listdir(self.rep):
            if имя.startswith("decision_") and имя.endswith(".md"):
                with open(os.path.join(self.rep, имя), encoding="utf-8") as fh:
                    keep += [ln for ln in fh if "ОСТАВИТЬ" in ln]
        if not keep:
            self.skipTest("групп не образовалось")
        self.assertTrue(any("original.jpg" in ln for ln in keep),
                        f"оставлен не оригинал: {keep}")


@unittest.skipUnless(HAVE_PIL, "нужен Pillow")
class TestDecisionMdNotOverwritten(Base):
    """Дефект-риск: перезапись decision.md стёрла бы отметки пользователя."""

    def test_существующий_файл_не_трогается(self):
        self.img("photo/A/a.jpg")
        self.img("300/a.jpg")
        self.inventory()
        run("02_signatures.py", "--db", self.db, "--stage", "all",
            "--threads", "2", "--min-size", "1")
        run("03_report.py", "--db", self.db, "--out", self.rep, "--md")
        md = os.path.join(self.rep, "decision.md")
        with open(md, "a", encoding="utf-8") as fh:
            fh.write("\n<!-- МОЯ ОТМЕТКА -->\n")
        run("03_report.py", "--db", self.db, "--out", self.rep, "--md")
        with open(md, encoding="utf-8") as fh:
            self.assertIn("МОЯ ОТМЕТКА", fh.read())
        прочие = [f for f in os.listdir(self.rep)
                  if f.startswith("decision_") and f.endswith(".md")]
        self.assertTrue(прочие, "новый чек-лист не создан")


@unittest.skipUnless(HAVE_PIL, "нужен Pillow")
class TestPhashMinSize(Base):
    """Дефект: порог 1024 байта был зашит и молча отсекал мелкие картинки."""

    def test_пропуск_мелких_сообщается(self):
        self.img("photo/A/tiny.jpg", 8, 8)
        self.inventory()
        out = run("02_signatures.py", "--db", self.db, "--stage", "phash",
                  "--threads", "2")
        self.assertIn("пропущено как слишком мелкие", out)

    def test_порог_настраивается(self):
        self.img("photo/A/tiny.jpg", 8, 8)
        self.inventory()
        run("02_signatures.py", "--db", self.db, "--stage", "phash",
            "--threads", "2", "--min-size", "1")
        self.assertGreater(self.q("SELECT COUNT(*) FROM sigs "
                                  "WHERE phash IS NOT NULL"), 0)


@unittest.skipUnless(HAVE_PIL, "нужен Pillow")
class TestRunLog(Base):
    """Новое поведение: журнал прогонов и сводка."""

    def test_журнал_пишется_и_накапливается(self):
        self.img("photo/A/a.jpg")
        self.img("300/a.jpg")
        self.inventory()
        log = os.path.join(self.rep, "signatures_log.csv")
        run("02_signatures.py", "--db", self.db, "--stage", "exact",
            "--threads", "2", "--log", log)
        self.assertTrue(os.path.exists(log))
        with open(log, encoding="utf-8-sig") as fh:
            n1 = sum(1 for _ in fh)
        run("02_signatures.py", "--db", self.db, "--stage", "exif",
            "--threads", "2", "--log", log)
        with open(log, encoding="utf-8-sig") as fh:
            n2 = sum(1 for _ in fh)
        self.assertGreater(n2, n1, "журнал не дописывается")
        with open(log, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh, delimiter=";"))
        for r in rows:
            self.assertTrue(r["старт"] and r["финиш"] and r["длительность"])
            self.assertIn(r["завершено"], ("да", "прервано"))


@unittest.skipUnless(HAVE_PIL, "нужен Pillow")
class TestResumeMarkers(Base):
    """
    Дефект: признаком «файл разобран» служило поле exif_dt, которое у снимков
    без даты съёмки остаётся пустым. Такие файлы перечитывались при каждом
    перезапуске, и стадия exif никогда не сходилась к нулю.
    """

    def test_файл_без_exif_не_перечитывается(self):
        self.img("photo/A/no_exif.jpg")                       # EXIF отсутствует
        self.img("photo/A/with_exif.jpg",
                 exif={36867: "2010:01:01 10:00:00"})
        self.inventory()
        first = run("02_signatures.py", "--db", self.db, "--stage", "exif",
                    "--threads", "2")
        self.assertIn("изображений к разбору: 2", first)
        second = run("02_signatures.py", "--db", self.db, "--stage", "exif",
                     "--threads", "2")
        self.assertIn("изображений к разбору: 0", second,
                      "файл без EXIF читается повторно")

    def test_прерывание_exif_не_запускает_video(self):
        """
        Дефект: после Ctrl+C стадия exif возвращала управление, и следом
        безусловно стартовала стадия video — со стороны выглядело так,
        будто скрипт не реагирует на прерывание.
        """
        with open(os.path.join(SCRIPTS, "02_signatures.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        i = src.index("def stage_exif")
        tail = src[i:i + src[i:].index("\ndef ")]
        self.assertIn('if st["interrupted"]', tail,
                      "stage_exif не проверяет прерывание перед stage_video_meta")


@unittest.skipUnless(HAVE_PIL, "нужен Pillow")
class TestReportGrouping(Base):
    """Отчёты группируются по папкам и сворачиваются."""

    def setUp(self):
        super().setUp()
        import shutil
        for i in range(2):
            self.img(f"photo/Дети/2012/d{i}.jpg", 100 + i, 80)
            self.img(f"photo/Туризм/j{i}.jpg", 200 + i, 150)
        os.makedirs(os.path.join(self.other, "Foto"), exist_ok=True)
        for i in range(2):
            shutil.copy(os.path.join(self.tmp, f"photo/Дети/2012/d{i}.jpg"),
                        os.path.join(self.other, f"Foto/d{i}.jpg"))
            shutil.copy(os.path.join(self.tmp, f"photo/Туризм/j{i}.jpg"),
                        os.path.join(self.other, f"Foto/j{i}.jpg"))
        self.inventory()
        run("02_signatures.py", "--db", self.db, "--stage", "all",
            "--threads", "2", "--min-size", "1", "--no-log")
        run("03_report.py", "--db", self.db, "--out", self.rep, "--md")

    def test_md_группируется_по_папке_дубля(self):
        """
        Ключевое требование: раздел — это папка, где лежит ДУБЛЬ, а не
        оригинал. «Что в этой папке лишнее», а не «где копии этого файла».
        """
        with open(os.path.join(self.rep, "decision_list.md"),
                  encoding="utf-8") as fh:
            md = fh.read()
        # пути теперь полные, а не с логическим псевдонимом диска:
        # их копируют в Finder, поэтому проверяем именно окончание пути
        self.assertIn("/300/Foto", md, "нет раздела с папкой дублей")
        self.assertTrue(md.count("## /") >= 1, "разделы не полными путями")
        self.assertIn("- [ ] удалить", md)
        self.assertIn("остаётся", md)

    def test_в_html_ВСЕ_дубли_без_усечения(self):
        """Дефект: отчёты резались лимитом, часть решений не попадала."""
        con = sqlite3.connect(self.db)
        try:
            дублей = con.execute(
                "SELECT COUNT(*) FROM group_members WHERE keep=0").fetchone()[0]
        finally:
            con.close()
        with open(os.path.join(self.rep, "decision.html"), encoding="utf-8") as fh:
            h = fh.read()
        d = json.loads(re.search(r"const DATA = (\{.*?\});\n", h, re.S)
                       .group(1).replace(r"<\/", "</"))
        self.assertEqual(len(d["rows"]), дублей, "часть дублей не попала в отчёт")

    def _устарел_test_в_чеклистах_ВСЕ_группы(self):
        """
        Дефект: и md, и html резались лимитом, и часть решений просто
        не попадала в рабочий документ.
        """
        import re
        con = sqlite3.connect(self.db)
        try:
            в_базе = con.execute(
                "SELECT COUNT(*) FROM groups WHERE method='exact'").fetchone()[0]
        finally:
            con.close()
        в_md = 0
        for f in os.listdir(self.rep):
            if f.startswith("decision_exact") and f.endswith(".md"):
                with open(os.path.join(self.rep, f), encoding="utf-8") as fh:
                    в_md += len(re.findall(r"^### группа ", fh.read(), re.M))
        self.assertEqual(в_md, в_базе, "часть групп не попала в чек-листы")

    def test_оглавление_ссылается_на_чеклисты(self):
        with open(os.path.join(self.rep, "decision.md"), encoding="utf-8") as fh:
            idx = fh.read()
        self.assertIn("[[decision_list]]", idx)
        self.assertIn("Крупнейшие папки", idx)

    def test_html_интерактивный_и_хранит_отметки(self):
        with open(os.path.join(self.rep, "decision.html"), encoding="utf-8") as fh:
            h = fh.read()
        for нужно in ("localStorage", "Скачать решения", "только неразобранные",
                      "удалить всю папку", "оставить обе", 'loading="lazy"',
                      "const DATA"):
            self.assertIn(нужно, h, нужно)

    def test_в_html_есть_ссылки_на_файлы_и_превью(self):
        with open(os.path.join(self.rep, "decision.html"), encoding="utf-8") as fh:
            h = fh.read()
        d = json.loads(re.search(r"const DATA = (\{.*?\});\n", h, re.S)
                       .group(1).replace(r"<\/", "</"))
        self.assertTrue(d["rows"], "нет строк решений")
        r = d["rows"][0]
        for k in ("dup", "keep"):
            self.assertTrue(r[k]["u"].startswith("file://"), "нет ссылки на файл")
            self.assertIn("·", r[k]["m"], "нет метаданных")

    def test_склонения(self):
        import mmdlib as L
        self.assertEqual(L.nfiles(1), "1 файл")
        self.assertEqual(L.nfiles(2), "2 файла")
        self.assertEqual(L.nfiles(5), "5 файлов")
        self.assertEqual(L.ngroups(1), "1 группа")
        self.assertEqual(L.ngroups(11), "11 групп")


@unittest.skipUnless(HAVE_PIL, "нужен Pillow")
class TestThumbs(Base):
    """Превью: только для различающихся файлов, хранятся в базе."""

    def setUp(self):
        super().setUp()
        big = self.img("photo/A/orig.jpg", 400, 300,
                       exif={36867: "2010:05:05 12:00:00", 272: "NIKON"})
        Image.open(big).resize((200, 150)).save(
            os.path.join(self.other, "small.jpg"))
        import shutil
        shutil.copy(big, os.path.join(self.other, "exact_copy.jpg"))
        self.inventory()
        run("02_signatures.py", "--db", self.db, "--stage", "all",
            "--threads", "2", "--min-size", "1", "--no-log")
        run("03_report.py", "--db", self.db, "--out", self.rep, "--md")

    def test_превью_не_делаются_для_точных_копий(self):
        run("02_signatures.py", "--db", self.db, "--stage", "thumb",
            "--threads", "2", "--no-log")
        con = sqlite3.connect(self.db)
        try:
            в_превью = {r[0] for r in con.execute(
                "SELECT file_id FROM thumbs WHERE status='ok'")}
            только_exact = {r[0] for r in con.execute(
                """SELECT gm.file_id FROM group_members gm
                   JOIN groups g ON g.id=gm.group_id WHERE g.method='exact'
                   AND gm.file_id NOT IN (SELECT gm2.file_id FROM group_members gm2
                     JOIN groups g2 ON g2.id=gm2.group_id
                     WHERE g2.method IN ('exif','phash'))""")}
        finally:
            con.close()
        self.assertFalse(в_превью & только_exact,
                         "превью сделаны для побайтово одинаковых файлов")

    def test_превью_лежат_в_базе_и_попадают_в_отчёт(self):
        run("02_signatures.py", "--db", self.db, "--stage", "thumb",
            "--threads", "2", "--no-log")
        n = self.q("SELECT COUNT(*) FROM thumbs WHERE status='ok' "
                   "AND jpeg IS NOT NULL")
        if not n:
            self.skipTest("групп exif/phash не образовалось")
        run("03_report.py", "--db", self.db, "--out", self.rep)
        self.assertTrue(os.path.isdir(os.path.join(self.rep, "thumbs")))
        with open(os.path.join(self.rep, "decision.html"), encoding="utf-8") as fh:
            h = fh.read()
        # <img> собирается в JS, поэтому проверяем данные, а не разметку
        d = json.loads(re.search(r"const DATA = (\{.*?\});\n", h, re.S)
                       .group(1).replace(r"<\/", "</"))
        self.assertTrue(any(r["dup"]["t"] or r["keep"]["t"] for r in d["rows"]),
                        "превью не попали в данные отчёта")
        self.assertIn('loading="lazy"', h)

    def test_отчёт_не_читает_диски(self):
        """Гарантия этапа 3: превью берутся из базы, а не с сетевых дисков."""
        with open(os.path.join(SCRIPTS, "03_report.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("Image.open", src)
        self.assertNotIn("subprocess", src)


@unittest.skipUnless(HAVE_PIL, "нужен Pillow")
class TestReportByMethod(Base):
    """
    Разделение по типу совпадения. При 22 тысячах точных копий и полутора
    тысячах требующих просмотра смешивать их в одном списке — значит утопить
    важное в рутине.
    """

    def setUp(self):
        super().setUp()
        import shutil
        big = self.img("photo/A/orig.jpg", 400, 300,
                       exif={36867: "2010:05:05 12:00:00", 272: "NIKON"})
        shutil.copy(big, os.path.join(self.other, "точная.jpg"))
        Image.open(big).resize((200, 150)).save(
            os.path.join(self.other, "похожая.jpg"))
        self.inventory()
        run("02_signatures.py", "--db", self.db, "--stage", "all",
            "--threads", "2", "--min-size", "1", "--no-log")
        run("03_report.py", "--db", self.db, "--out", self.rep)
        with open(os.path.join(self.rep, "decision.html"),
                  encoding="utf-8") as fh:
            self.h = fh.read()

    def test_есть_фильтр_по_типу(self):
        self.assertIn("renderTabs", self.h)
        self.assertIn("byMethod", self.h)

    def test_у_каждого_типа_свой_цвет(self):
        for клсс, цвет in (("m-exact", "#639922"),
                           ("m-exif", "#BA7517"),
                           ("m-phash", "#7F77DD")):
            self.assertIn(клсс, self.h, клсс)
            self.assertIn(цвет, self.h, f"нет цвета для {клсс}")

    def test_красный_не_используется_для_phash(self):
        """phash требует внимания, но не является ошибкой."""
        начало = self.h.index(".row.m-phash")
        кусок = self.h[начало:начало + 200]
        for красный in ("#e24b4a", "#E24B4A", "#a32d2d", "red"):
            self.assertNotIn(красный, кусок)

    def test_точные_копии_показываются_компактно(self):
        """Превью двух побайтово одинаковых файлов — визуальный шум."""
        self.assertIn("compact", self.h)
        self.assertIn(".compact .thumb", self.h)

    def test_начинаем_с_механической_работы(self):
        self.assertIn("let method = 'exact'", self.h)

    def test_есть_три_решения_а_не_два(self):
        """
        Дефект: «не отмечено» значило одновременно «не смотрел» и «решил
        оставить обе». Фильтр «только неразобранные» из-за этого никогда
        не опустошался — дойти до конца было невозможно.
        """
        for нужно in ("ОСТАВИТЬ_ОБЕ", "НАОБОРОТ", "keepBoth", "swapped",
                      "оставлены_обе", "keepall"):
            self.assertIn(нужно, self.h, нужно)

    def test_решение_остаётся_видимым_после_клика(self):
        """
        Дефект: любое решение перерисовывало список целиком, строка тут же
        исчезала под фильтром «только неразобранные», и убедиться глазами,
        что получилось задуманное, было нельзя.
        """
        self.assertIn("updateRow", self.h)
        self.assertIn("bindBody", self.h)
        self.assertIn("hidedone", self.h, "нет кнопки «скрыть решённые»")

    def test_ключ_решения_уникален_для_строки(self):
        """
        Дефект: состояние хранилось по id ФАЙЛА, а один файл попадает
        в несколько строк — группу exact и группу phash — с РАЗНЫМИ
        оригиналами. Отметка в одной строке молча ставилась и в другой.
        """
        d = json.loads(re.search(r"const DATA = (\{.*?\});\n", self.h, re.S)
                       .group(1).replace(r"<\/", "</"))
        rids = [r["rid"] for r in d["rows"]]
        self.assertEqual(len(rids), len(set(rids)),
                         "ключи решений повторяются — отметки будут слипаться")
        for r in d["rows"]:
            self.assertIn(r["method"], r["rid"])

    def test_переворот_пары_меняет_стороны(self):
        """Автоматика судит по формальным признакам и не знает контекста."""
        self.assertIn("переопределено", self.h)
        self.assertIn("keeperUsedBy", self.h,
                      "нет предупреждения о переиспользуемом оригинале")


class TestApplySafety(unittest.TestCase):
    """
    Единственный скрипт, который меняет файлы. Проверки здесь важнее всех
    остальных: ошибка тут стоит потерянных фотографий.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mmd_apply_")
        self.a = os.path.join(self.tmp, "a")
        self.b = os.path.join(self.tmp, "b")
        os.makedirs(self.a)
        os.makedirs(self.b)
        for i in range(4):
            for d in (self.a, self.b):
                with open(os.path.join(d, f"f{i}.jpg"), "wb") as fh:
                    fh.write(b"x" * (100 + i))
        self.dec = os.path.join(self.tmp, "d.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def решения(self, items):
        with open(self.dec, "w", encoding="utf-8") as fh:
            json.dump({"формат": "mmd2026-решения", "отчёт": "t",
                       "к_удалению": items}, fh, ensure_ascii=False)
        return self.dec

    def запуск(self, *args):
        return run("04_apply.py", "--decisions", self.dec,
                   "--root", f"a={self.a}", "--root", f"b={self.b}", *args)

    def файл(self, d, n):
        p = os.path.join(d, n)
        return {"id": 1, "путь": p, "байт": os.path.getsize(p),
                "метод": "exact", "оставить": os.path.join(
                    self.a if d == self.b else self.b, n)}

    def test_по_умолчанию_ничего_не_трогает(self):
        self.решения([self.файл(self.b, "f0.jpg")])
        out = self.запуск()
        self.assertIn("НИ ОДИН ФАЙЛ НЕ ТРОНУТ", out)
        self.assertTrue(os.path.exists(os.path.join(self.b, "f0.jpg")))

    def test_не_удаляет_когда_оригинала_нет(self):
        r = self.файл(self.b, "f0.jpg")
        r["оставить"] = os.path.join(self.a, "НЕТУ.jpg")
        self.решения([r])
        out = self.запуск("--move")
        self.assertIn("ОРИГИНАЛ НЕ НАЙДЕН", out)
        self.assertTrue(os.path.exists(os.path.join(self.b, "f0.jpg")),
                        "файл убран, хотя оригинала не существует")

    def test_не_удаляет_обе_копии(self):
        """Самый опасный случай: и дубль, и оригинал отмечены к удалению."""
        r1 = self.файл(self.b, "f1.jpg")
        r2 = self.файл(self.a, "f1.jpg")
        self.решения([r1, r2])
        out = self.запуск("--move")
        self.assertIn("ОРИГИНАЛ ТОЖЕ ОТМЕЧЕН", out)
        self.assertTrue(os.path.exists(os.path.join(self.a, "f1.jpg")))
        self.assertTrue(os.path.exists(os.path.join(self.b, "f1.jpg")))

    def test_не_трогает_файлы_вне_настроенных_дисков(self):
        r = self.файл(self.b, "f0.jpg")
        r["путь"] = os.path.join(self.tmp, "снаружи.jpg")
        with open(r["путь"], "wb") as fh:
            fh.write(b"y")
        r["байт"] = 1
        self.решения([r])
        out = self.запуск("--move")
        self.assertIn("вне настроенных дисков", out)
        self.assertTrue(os.path.exists(r["путь"]))

    def test_не_трогает_если_размер_изменился(self):
        r = self.файл(self.b, "f0.jpg")
        r["байт"] = r["байт"] + 999
        self.решения([r])
        out = self.запуск("--move")
        self.assertIn("размер изменился", out)
        self.assertTrue(os.path.exists(os.path.join(self.b, "f0.jpg")))

    def test_перенос_и_возврат(self):
        self.решения([self.файл(self.b, "f0.jpg"),
                      self.файл(self.b, "f2.jpg")])
        qr = os.path.join(self.tmp, "qr")
        out = self.запуск("--move", "--quarantine", qr, "--verify-hash")
        # Формат итога менялся; проверяем факт, а не вёрстку строки
        self.assertRegex(out, r"перенесено\s*:?\s*2\b")
        self.assertFalse(os.path.exists(os.path.join(self.b, "f0.jpg")))
        # Журнал лежит рядом с самим карантином, а карантин теперь свой
        # у каждого диска. --undo принимает и папку целиком.
        журналы = [os.path.join(к, "journal.csv")
                   for к, _, ф in os.walk(qr) if "journal.csv" in ф]
        self.assertTrue(журналы, "журнал не создан")

        back = run("04_apply.py", "--undo", qr)
        self.assertIn("возвращено 2", back)
        self.assertTrue(os.path.exists(os.path.join(self.b, "f0.jpg")),
                        "файл не вернулся из карантина")

    def test_структура_каталогов_в_карантине_сохраняется(self):
        глубоко = os.path.join(self.b, "2014", "лето")
        os.makedirs(глубоко)
        p = os.path.join(глубоко, "f0.jpg")
        with open(p, "wb") as fh:
            fh.write(b"z" * 50)
        self.решения([{"id": 9, "путь": p, "байт": 50, "метод": "exact",
                       "оставить": os.path.join(self.a, "f0.jpg")}])
        qr = os.path.join(self.tmp, "qr2")
        self.запуск("--move", "--quarantine", qr)
        self.assertTrue(
            os.path.isfile(os.path.join(qr, "b", "2014", "лето", "f0.jpg")),
            "структура каталогов в карантине не сохранена")


class TestByteProgress(unittest.TestCase):
    """
    Дефект: прогресс считался по числу файлов, а кандидаты идут по
    возрастанию размера — на 27% файлов бывало прочитано 3% байт.
    """

    def test_доли_по_файлам_и_по_байтам_расходятся(self):
        sizes = [50_000] * 200 + [20_000_000] * 8   # как в реальной библиотеке
        total = sum(sizes)
        n = len(sizes)
        доля_файлов = 200 / n
        доля_байт = sum(sizes[:200]) / total
        self.assertGreater(доля_файлов, 0.9)
        self.assertLess(доля_байт, 0.1)
        # именно поэтому ETA обязан считаться по байтам


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestQuarantineOnSameVolume(unittest.TestCase):
    """
    Карантин по умолчанию — на том же диске, где лежит файл.

    Дефект: карантин создавался в каталоге проекта. У пользователя проект
    на Mac, а архив на сетевом хранилище, и «перенос» превращался
    в копирование 70 ГБ по сети. Внутри одного тома это переименование:
    мгновенно и атомарно.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.photo = os.path.join(self.tmp, "photo")
        self.other = os.path.join(self.tmp, "300")
        os.makedirs(self.photo)
        os.makedirs(self.other)

    def tearDown(self):
        import shutil as sh
        sh.rmtree(self.tmp, ignore_errors=True)

    def мод(self):
        import importlib.util as u
        sp = u.spec_from_file_location(
            "apply4", os.path.join(SCRIPTS, "04_apply.py"))
        m = u.module_from_spec(sp)
        sp.loader.exec_module(m)
        return m

    def test_по_умолчанию_карантин_внутри_диска(self):
        m = self.мод()
        drives = {"photo": self.photo, "300photos": self.other}
        bases = m.quarantine_bases(drives, None, "2026-01-01_00-00-00")
        for имя, корень in drives.items():
            self.assertTrue(
                bases[имя].startswith(корень + os.sep),
                f"карантин диска {имя} оказался вне самого диска: {bases[имя]}")

    def test_карантин_на_том_же_томе(self):
        m = self.мод()
        drives = {"photo": self.photo}
        bases = m.quarantine_bases(drives, None, "ts")
        self.assertTrue(m.same_device(self.photo, bases["photo"]),
                        "перенос стал бы копированием, а не переименованием")

    def test_явная_папка_перекрывает_умолчание(self):
        m = self.мод()
        свой = os.path.join(self.tmp, "куда-то")
        bases = m.quarantine_bases({"photo": self.photo}, свой, "ts")
        self.assertTrue(bases["photo"].startswith(свой),
                        "--quarantine должен оставаться рабочим")

    def test_структура_внутри_диска_без_имени_диска(self):
        m = self.мод()
        src = os.path.join(self.photo, "2014", "a.jpg")
        got = m.quarantine_path("/qr", src, self.photo)
        self.assertEqual(got, os.path.join("/qr", "2014", "a.jpg"))


class TestApplyOutput(unittest.TestCase):
    """
    Прогон на сетевом диске идёт часами. Вывод обязан отвечать на три
    вопроса, не заставляя листать простыню вверх: когда запустились,
    с какой скоростью идём, чем кончилось.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = os.path.join(self.tmp, "a")
        self.b = os.path.join(self.tmp, "b")
        os.makedirs(self.a)
        os.makedirs(self.b)
        for d in (self.a, self.b):
            for i in range(3):
                with open(os.path.join(d, f"f{i}.jpg"), "wb") as fh:
                    fh.write(b"x" * (100 + i))
        self.dec = os.path.join(self.tmp, "d.json")
        with open(self.dec, "w", encoding="utf-8") as fh:
            json.dump({"формат": "mmd2026-решения", "отчёт": "t",
                       "к_удалению": [
                           {"rid": f"exact:{i}:{i}", "id": i,
                            "путь": os.path.join(self.b, f"f{i}.jpg"),
                            "байт": 100 + i, "метод": "exact",
                            "оставить": os.path.join(self.a, f"f{i}.jpg")}
                           for i in range(3)]}, fh, ensure_ascii=False)

    def tearDown(self):
        import shutil as sh
        sh.rmtree(self.tmp, ignore_errors=True)

    def запуск(self, *args):
        return run("04_apply.py", "--decisions", self.dec,
                   "--root", f"photo={self.a}", "--root", f"other={self.b}",
                   *args)

    def test_в_начале_печатается_время_запуска(self):
        out = self.запуск()
        self.assertIn("запуск:", out)
        self.assertIn("MMD-2026 duplicate finder", out,
                      "версия в выводе: иначе непонятно, чем прогон сделан")

    def test_в_конце_есть_итог(self):
        out = self.запуск("--move")
        self.assertIn("ИТОГ", out)
        for поле in ("перенесено", "время", "начало", "конец", "КАРАНТИН"):
            self.assertIn(поле, out, f"в итоге нет поля «{поле}»")

    def test_итога_нет_в_предпросмотре(self):
        out = self.запуск()
        self.assertNotIn("ИТОГ", out,
                         "предпросмотр ничего не переносил — итога быть не должно")

    def test_после_переноса_напоминает_про_prune(self):
        # Шаг, который пропускают чаще всего: база продолжает считать
        # перенесённые файлы существующими, и следующий отчёт врёт.
        out = self.запуск("--move")
        self.assertIn("--prune", out)
        self.assertIn("03_report.py", out)

    def test_напоминания_нет_в_предпросмотре(self):
        out = self.запуск()
        self.assertNotIn("СЛЕДУЮЩИЙ ШАГ", out,
                         "ничего не переносили — нечего и обновлять")

    def test_undo_подсказывает_папку_а_не_файл(self):
        out = self.запуск("--move")
        строка = [s for s in out.splitlines() if "--undo" in s][-1]
        self.assertNotIn("journal.csv", строка,
                         "--undo принимает папку карантина: журналов может "
                         "быть несколько, и один из них забудут")


class TestMountCheck(unittest.TestCase):
    """
    Непримонтированная сетевая шара выглядит как пустая локальная папка.

    Дефект по следам реального прогона: карантин уехал на системный диск,
    потому что проверка ограничивалась os.path.isdir. Тот же промах в этапе 1
    страшнее: обход нашёл бы ноль файлов, а --prune вычистил бы инвентарь.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil as sh
        sh.rmtree(self.tmp, ignore_errors=True)

    def test_нет_каталога(self):
        стоп, _ = mmdlib.проверить_диски({"photo": os.path.join(self.tmp, "нет")})
        self.assertTrue(стоп)
        self.assertIn("каталога нет", стоп[0])

    def test_пустой_каталог_предупреждение_а_при_строго_отказ(self):
        пустой = os.path.join(self.tmp, "Volumes_photo")
        os.makedirs(пустой)
        стоп, внимание = mmdlib.проверить_диски({"photo": пустой})
        self.assertFalse(стоп, "пустой диск бывает законно — не повод отказывать")
        self.assertIn("ПУСТ", внимание[0])
        стоп, _ = mmdlib.проверить_диски({"photo": пустой}, строго=True)
        self.assertTrue(стоп, "там, где файлы двигают, пустой диск — стоп")

    def test_непустой_локальный_каталог_проходит(self):
        норм = os.path.join(self.tmp, "архив")
        os.makedirs(норм)
        with open(os.path.join(норм, "a.jpg"), "wb") as fh:
            fh.write(b"x")
        стоп, внимание = mmdlib.проверить_диски({"photo": норм})
        self.assertEqual((стоп, внимание), ([], []))

    def test_точка_монтирования_поднимается_до_смены_тома(self):
        # корень диска может лежать ВНУТРИ тома: /Volumes/video/300 Photos
        глубоко = os.path.join(self.tmp, "video", "300 Photos")
        os.makedirs(глубоко)
        точка = mmdlib.точка_монтирования(глубоко)
        self.assertEqual(точка, mmdlib.точка_монтирования(self.tmp),
                         "точка монтирования у вложенной папки та же, что "
                         "у тома целиком")

    def test_описание_дисков_печатает_том_и_свободное(self):
        строки = mmdlib.описание_дисков({"photo": self.tmp})
        self.assertEqual(len(строки), 1)
        self.assertIn("том", строки[0])
        self.assertIn("свободно", строки[0])

    def test_apply_отказывается_при_пустом_диске(self):
        пустой = os.path.join(self.tmp, "Volumes_photo")
        целевой = os.path.join(self.tmp, "b")
        os.makedirs(пустой)
        os.makedirs(целевой)
        with open(os.path.join(целевой, "f.jpg"), "wb") as fh:
            fh.write(b"x" * 10)
        dec = os.path.join(self.tmp, "d.json")
        with open(dec, "w", encoding="utf-8") as fh:
            json.dump({"к_удалению": [
                {"rid": "exact:1:2", "id": 1,
                 "путь": os.path.join(целевой, "f.jpg"), "байт": 10,
                 "метод": "exact", "оставить": os.path.join(пустой, "f.jpg")}
            ]}, fh, ensure_ascii=False)
        out = run("04_apply.py", "--decisions", dec, "--move",
                  "--root", f"photo={пустой}", "--root", f"other={целевой}")
        self.assertIn("ДИСКИ НЕ ГОТОВЫ", out)
        self.assertTrue(os.path.exists(os.path.join(целевой, "f.jpg")),
                        "при неготовых дисках нельзя трогать файлы")


class TestQuarantineProvenance(unittest.TestCase):
    """
    Карантин обязан объяснять сам себя.

    Он живёт неделями, а вопрос «откуда это и по какому решению» возникает
    позже — когда файл решений уже переименован, перезаписан или удалён
    из Загрузок. Журнал говорит, ЧТО перенесено, но не говорит, чем
    это решение было принято.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = os.path.join(self.tmp, "a")
        self.b = os.path.join(self.tmp, "b")
        os.makedirs(self.a)
        os.makedirs(self.b)
        for d in (self.a, self.b):
            with open(os.path.join(d, "f.jpg"), "wb") as fh:
                fh.write(b"x" * 100)
        self.dec = os.path.join(self.tmp, "решения_20260811.json")
        with open(self.dec, "w", encoding="utf-8") as fh:
            json.dump({"формат": "mmd2026-решения", "отчёт": "20260811-1234",
                       "сохранено": "2026-08-11T10:00:00Z",
                       "к_удалению": [
                           {"rid": "exact:1:2", "id": 1,
                            "путь": os.path.join(self.b, "f.jpg"),
                            "байт": 100, "метод": "exact",
                            "оставить": os.path.join(self.a, "f.jpg")}]},
                      fh, ensure_ascii=False)

    def tearDown(self):
        import shutil as sh
        sh.rmtree(self.tmp, ignore_errors=True)

    def карантин(self):
        for корень, _, файлы in os.walk(self.b):
            if "journal.csv" in файлы:
                return корень
        return None

    def test_рядом_с_журналом_лежит_происхождение(self):
        out = run("04_apply.py", "--decisions", self.dec, "--move",
                  "--root", f"a={self.a}", "--root", f"b={self.b}")
        к = self.карантин()
        self.assertIsNotNone(к, f"карантин не найден. Вывод:\n{out}")
        инфо = os.path.join(к, "run_info.txt")
        копия = os.path.join(к, "decisions.json")
        self.assertTrue(os.path.isfile(инфо), "нет файла run_info.txt")
        self.assertTrue(os.path.isfile(копия),
                        "нет копии решений: исходный файл могут удалить")
        with open(инфо, encoding="utf-8") as fh:
            текст = fh.read()
        for нужно in (os.path.basename(self.dec), "20260811-1234",
                      "sha256", "--undo", "MMD-2026"):
            self.assertIn(нужно, текст, f"в run_info.txt нет «{нужно}»")

    def test_копия_решений_совпадает_с_исходной(self):
        run("04_apply.py", "--decisions", self.dec, "--move",
            "--root", f"a={self.a}", "--root", f"b={self.b}")
        к = self.карантин()
        with open(self.dec, encoding="utf-8") as fh:
            было = json.load(fh)
        with open(os.path.join(к, "decisions.json"), encoding="utf-8") as fh:
            стало = json.load(fh)
        self.assertEqual(было, стало)

    def test_несколько_файлов_под_шаблоном_отвергаются(self):
        # Оболочка раскрывает *.json в список; молча взять первый и бросить
        # остальные — значит выполнить половину работы, не сказав об этом.
        второй = os.path.join(self.tmp, "решения_20260812.json")
        import shutil as sh
        sh.copy(self.dec, второй)
        out = run("04_apply.py", "--decisions", self.dec, второй, "--move",
                  "--root", f"a={self.a}", "--root", f"b={self.b}")
        self.assertIn("ПРОПУЩЕН", out)
        self.assertIn(os.path.basename(второй), out)
        self.assertTrue(os.path.isfile(os.path.join(self.b, "f.jpg")),
                        "при неоднозначном вводе трогать файлы нельзя")

    def test_файл_решений_назван_в_выводе(self):
        out = run("04_apply.py", "--decisions", self.dec,
                  "--root", f"a={self.a}", "--root", f"b={self.b}")
        self.assertIn(os.path.basename(self.dec), out,
                      "в предпросмотре не видно, какой файл решений разбирается")
