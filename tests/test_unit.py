"""
Юнит-тесты чистых функций. Без файловой системы, без базы, без сети.

Запуск:  .venv/bin/python -m unittest discover -s tests -v
Быстрые: должны отрабатывать за доли секунды, поэтому гоняются первыми.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scripts"))
import mmdlib as L  # noqa: E402


class TestClassify(unittest.TestCase):
    def test_основные_классы(self):
        cases = {
            "a.JPG": "image", "b.jpeg": "image", "c.heic": "image",
            "d.cr2": "raw", "e.dng": "raw",
            "f.mov": "video", "g.mp4": "video",
            "h.wav": "audio", "i.mp3": "audio",
            "j.rar": "archive", "k.zip": "archive", "l.iso": "archive",
            "m.xmp": "sidecar", "n.aae": "sidecar",
            ".DS_Store": "junk", "Thumbs.db": "junk", "._resource": "junk",
            "o.txt": "other", "no_extension": "other",
        }
        for name, want in cases.items():
            with self.subTest(name=name):
                self.assertEqual(L.classify(name)[0], want)

    def test_регистр_расширения_не_важен(self):
        self.assertEqual(L.classify("X.JpEg")[0], "image")

    def test_tga_это_изображение(self):
        """Регрессия: .tga попадал в 'other' и выпадал из дедупликации."""
        self.assertEqual(L.classify("art.tga")[0], "image")

    def test_служебные_каталоги(self):
        for d in ("@eaDir", "#recycle", ".Spotlight-V100", "#snapshot"):
            self.assertTrue(L.is_junk_dir(d), d)
        self.assertFalse(L.is_junk_dir("Дети"))


class TestCsvSafe(unittest.TestCase):
    """Регрессия: formula injection при открытии отчёта в Excel."""

    def test_опасные_префиксы_обезвреживаются(self):
        for s in ("=cmd|' /C calc'!A1", "+1+1", "-2+3", "@SUM(A1)",
                  "\tтаб", "\rвозврат"):
            with self.subTest(s=s):
                self.assertTrue(L.csv_safe(s).startswith("'"))

    def test_обычные_значения_не_меняются(self):
        for s in ("IMG_1238.JPG", "Дети/2012", "123", ""):
            self.assertEqual(L.csv_safe(s), s)

    def test_none_становится_пустой_строкой(self):
        self.assertEqual(L.csv_safe(None), "")

    def test_строка_целиком_сохраняется(self):
        s = "=опасно.jpg"
        self.assertIn("опасно.jpg", L.csv_safe(s))


class TestMdText(unittest.TestCase):
    """Регрессия: имя файла разрывало markdown-ссылку и вело наружу."""

    def test_разметка_экранируется(self):
        for ch in "[]()<>`|*_":
            with self.subTest(ch=ch):
                self.assertIn("\\" + ch, L.md_text(f"a{ch}b"))

    def test_перевод_строки_схлопывается(self):
        self.assertNotIn("\n", L.md_text("a\nb"))
        self.assertNotIn("\r", L.md_text("a\rb"))

    def test_ссылка_наружу_не_собирается(self):
        out = L.md_text("brk](http://evil.tld) [x.jpg")
        self.assertNotIn("](http", out)


class TestOriginalityKey(unittest.TestCase):
    """Общий порядок «кто ближе к оригиналу» для поиска и отчёта."""

    def k(self, **kw):
        kw.setdefault("size", 1000)
        kw.setdefault("mtime", 0)
        return L.originality_key(**kw)

    def test_дата_съёмки_важнее_всего(self):
        """Регрессия: кандидат с чужой датой возглавлял список из-за размера."""
        верный = self.k(path="a.jpg", dt_match="совпадает")
        чужой = self.k(path="b.jpg", width=9000, height=9000,
                       size=10**9, dt_match="противоречит")
        self.assertLess(верный, чужой)

    def test_противоречит_хуже_чем_нет_данных(self):
        нет = self.k(path="a.jpg", dt_match="нет данных")
        против = self.k(path="b.jpg", dt_match="противоречит")
        self.assertLess(нет, против)

    def test_exif_важнее_разрешения(self):
        """Регрессия: апскейл без EXIF обходил оригинал от камеры."""
        оригинал = self.k(path="a.jpg", exif_dt="2008:12:24 13:24:00",
                          width=600, height=400)
        апскейл = self.k(path="b.jpg", width=9000, height=9000)
        self.assertLess(оригинал, апскейл)

    def test_целевой_диск_приоритетнее(self):
        свой = self.k(path="a.jpg", drive="photo", target_drive="photo")
        чужой = self.k(path="a.jpg", drive="300photos", target_drive="photo")
        self.assertLess(свой, чужой)

    def test_производные_опускаются(self):
        for плохой in ("orig_2.jpg", "orig (1).jpg", "orig copy.jpg",
                       "WhatsApp/orig.jpg", "Screenshots/orig.jpg"):
            with self.subTest(p=плохой):
                self.assertLess(self.k(path="orig.jpg"), self.k(path=плохой))

    def test_при_прочих_равных_больше_пикселей(self):
        больше = self.k(path="a.jpg", width=4000, height=3000)
        меньше = self.k(path="b.jpg", width=800, height=600)
        self.assertLess(больше, меньше)


class TestPhashBanding(unittest.TestCase):
    """
    Дефект: полос было ровно 4 при пороге по умолчанию 6, и около 41% пар
    на расстоянии 6 не встречались в общей корзине — поиск молча возвращал
    случайную выборку похожих вместо всех.

    Правило (принцип Дирихле): чтобы гарантировать нахождение всех пар с
    расстоянием не больше T, полос должно быть не меньше T+1.
    """

    def setUp(self):
        import importlib.util as u
        sp = u.spec_from_file_location(
            "rep", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scripts", "03_report.py"))
        self.rep = u.module_from_spec(sp)
        sp.loader.exec_module(self.rep)

    def test_полосы_покрывают_все_64_бита(self):
        for n in range(1, 17):
            b = self.rep.band_bounds(n)
            self.assertEqual(len(b), n)
            self.assertEqual(sum(w for _, w in b), 64, f"полос {n}")
            # без дыр и нахлёстов
            pos = 0
            for сдвиг, w in b:
                self.assertEqual(сдвиг, pos)
                pos += w

    def test_полнота_гарантирована_до_порога(self):
        """Пара на расстоянии ровно T обязана попасть в общую корзину."""
        import random
        random.seed(7)
        for T in (1, 2, 3, 4, 6):
            bounds = self.rep.band_bounds(T + 1)
            промахи = 0
            for _ in range(3000):
                a = random.getrandbits(64)
                b = a
                for i in random.sample(range(64), T):
                    b ^= 1 << i
                общая = any(((a >> p) & ((1 << w) - 1)) ==
                            ((b >> p) & ((1 << w) - 1)) for p, w in bounds)
                промахи += not общая
            self.assertEqual(промахи, 0,
                             f"порог {T}: {промахи} пар не найдено")

    def test_четырёх_полос_не_хватает_для_порога_6(self):
        """
        Тест-свидетель: фиксирует, почему число полос нельзя задавать
        константой. При 4 полосах и расстоянии 6 теряется около 40% пар.
        """
        import random
        random.seed(7)
        bounds = self.rep.band_bounds(4)
        всего = 2000
        промахи = 0
        for _ in range(всего):
            a = random.getrandbits(64)
            b = a
            for i in random.sample(range(64), 6):
                b ^= 1 << i
            общая = any(((a >> p) & ((1 << w) - 1)) ==
                        ((b >> p) & ((1 << w) - 1)) for p, w in bounds)
            промахи += not общая
        доля = промахи / всего
        self.assertGreater(доля, 0.3,
                           f"ожидалась заметная потеря пар, получено {доля:.0%}")


class TestFormatters(unittest.TestCase):
    def test_human(self):
        self.assertEqual(L.human(0), "0 B")
        self.assertIn("KB", L.human(2048))
        self.assertIn("GB", L.human(5 * 1024**3))

    def test_dur(self):
        self.assertEqual(L.dur(45), "45 с")
        self.assertEqual(L.dur(90), "1 мин 30 с")
        self.assertEqual(L.dur(3600 + 38 * 60), "1 ч 38 мин")


if __name__ == "__main__":
    unittest.main(verbosity=2)
