#!/usr/bin/env bash
# Quality gate: гонять после КАЖДОЙ правки скриптов.
#
#   ./check.sh          полный прогон
#   ./check.sh fast     без состязательного стенда (секунды вместо минуты)
#   PY=/путь/к/python ./check.sh    явно задать интерпретатор
#
# Ничего не трогает на сетевых дисках: весь стенд создаётся во временном
# каталоге и удаляется по выходу.
set -uo pipefail
cd "$(dirname "$0")"

# Интерпретатор: venv проекта, и никаких молчаливых подмен.
#
# Откат на системный python здесь недопустим: без Pillow и imagehash стадии
# exif и phash просто пропускаются, и гейт напечатал бы «ПРОЙДЕН», не проверив
# половину конвейера. Ложный зелёный хуже красного, поэтому все проблемы
# с окружением — это ошибка с диагнозом, а не деградация.
#
# Осознанный запуск на системном Python возможен только явно:
#   PY=python3 ./check.sh      (тогда проверка пакетов пропускается)
if [ -n "${PY:-}" ]; then
  echo "интерпретатор задан явно: $PY"
  EXPLICIT_PY=1
else
  EXPLICIT_PY=0
  VENV=""
  for cand in scripts/.venv .venv; do
    [ -d "$cand" ] && { VENV="$cand"; break; }
  done
  if [ -z "$VENV" ]; then
    echo "ОШИБКА: venv не найден."
    echo "  Ожидался каталог scripts/.venv или .venv"
    echo "  Создать:  python3 -m venv scripts/.venv"
    echo "            scripts/.venv/bin/pip install -r requirements.txt"
    exit 1
  fi
  PY="$VENV/bin/python"
  # -e не годится: bin/python это симлинк, и у нерабочего venv он висячий.
  # Различаем «venv нет» и «venv есть, но сломан» — диагнозы разные.
  if [ ! -e "$PY" ] && [ ! -L "$PY" ]; then
    echo "ОШИБКА: $VENV существует, но в нём нет bin/python."
    echo "  Каталог venv повреждён. Пересоздать:"
    echo "    rm -rf $VENV && python3 -m venv $VENV"
    echo "    $VENV/bin/pip install -r requirements.txt"
    exit 1
  fi
  if ! "$PY" -c "import sys" >/dev/null 2>&1; then
    echo "ОШИБКА: $PY есть, но не запускается."
    if [ -L "$PY" ]; then
      echo "  Это висячий симлинк: $(readlink "$PY" 2>/dev/null)"
    fi
    [ -f "$VENV/pyvenv.cfg" ] && \
      echo "  venv собран для: $(grep -m1 '^home' "$VENV/pyvenv.cfg" 2>/dev/null)"
    echo "  Обычные причины: venv создан на другой машине или ОС; переехал"
    echo "  каталог проекта; удалён базовый Python (venv хранит абсолютные пути)."
    echo "  Пересоздать:  rm -rf $VENV && python3 -m venv $VENV"
    echo "                $VENV/bin/pip install -r requirements.txt"
    exit 1
  fi
  echo "интерпретатор: $PY ($("$PY" -V 2>&1))"
fi

# Пакеты проверяются до тестов: иначе стадии exif/phash тихо пропустятся,
# а гейт отчитается об успехе.
if [ "$EXPLICIT_PY" = "0" ]; then
  MISSING=$("$PY" - <<'PY'
mods = {"PIL": "pillow", "imagehash": "imagehash", "pillow_heif": "pillow-heif"}
missing = []
for mod, pkg in mods.items():
    try:
        __import__(mod)
    except ImportError:
        missing.append(pkg)
print(" ".join(missing))
PY
)
  if [ -n "$MISSING" ]; then
    echo "ОШИБКА: в venv не хватает пакетов: $MISSING"
    echo "  Без них стадии exif и phash не выполняются, и гейт проверит"
    echo "  лишь часть конвейера."
    echo "  Установить:  $(dirname "$PY")/pip install -r requirements.txt"
    exit 1
  fi
  echo "пакеты: Pillow, imagehash, pillow-heif — на месте"
fi
T=$(mktemp -d)
FAIL=0
step() { printf "\n\033[1m== %s\033[0m\n" "$*"; }
ok()   { printf "  \033[32mOK\033[0m   %s\n" "$*"; }
bad()  { printf "  \033[31mFAIL\033[0m %s\n" "$*"; FAIL=$((FAIL+1)); }
trap 'rm -rf "$T"' EXIT

step "1. Компиляция"
for f in scripts/*.py; do
  "$PY" -m py_compile "$f" 2>/dev/null && ok "$(basename "$f")" || bad "$(basename "$f")"
done

step "2. Статический анализ"
# Никаких проверок внутри `if <конвейер>`: при pipefail ненулевой статус ruff
# делал всё условие ложным, тело не выполнялось, bad не вызывался — и гейт
# зеленел, показав при этом список ошибок. Статус берём явно.
if "$PY" -m ruff --version >/dev/null 2>&1; then
  "$PY" -m ruff check --select E,F,W,B,S,A,C4,SIM --ignore E501,S101 \
      --output-format concise scripts/ tests/ >"$T/ruff.txt" 2>&1
  RUFF_RC=$?
  tail -20 "$T/ruff.txt"
  if [ "$RUFF_RC" -eq 0 ] && grep -q "All checks passed" "$T/ruff.txt"; then
    ok "ruff чист"
  else
    bad "ruff нашёл замечания (код возврата $RUFF_RC)"
  fi
  # Самопроверка: убеждаемся, что путь обнаружения ошибок вообще работает.
  # Именно он однажды и отказал — ruff печатал замечания, а гейт зеленел.
  printf 'import os\nx=1\n' >"$T/broken_check.py"
  "$PY" -m ruff check --select F401 --output-format concise \
      "$T/broken_check.py" >/dev/null 2>&1
  [ $? -ne 0 ] && ok "самопроверка: замечания ruff действительно роняют шаг" \
                || bad "САМОПРОВЕРКА ПРОВАЛЕНА: гейт не увидит ошибок ruff"
else
  bad "ruff не установлен: $(dirname "$PY")/pip install ruff"
fi

step "3. Юнит-тесты (чистые функции)"
if "$PY" -m unittest -q tests.test_unit 2>"$T/unit.txt"; then
  ok "$(grep -oE 'Ran [0-9]+ tests' "$T/unit.txt" | tail -1) — все прошли"
else
  bad "юнит-тесты"; grep -E "FAIL|Error|AssertionError" "$T/unit.txt" | head -8
fi

step "4. Регрессионные тесты (по одному на пойманный дефект)"
if "$PY" -m unittest -q tests.test_regress 2>"$T/regress.txt"; then
  ok "$(grep -oE 'Ran [0-9]+ tests' "$T/regress.txt" | tail -1) — все прошли"
else
  bad "регрессионные тесты"; grep -E "^(FAIL|ERROR):|AssertionError" "$T/regress.txt" | head -10
fi

step "5. Журнал прогонов этапа 2"
"$PY" - <<'PY' && ok "журнал и сводка" || bad "журнал и сводка"
import sys, os, datetime as dt, importlib.util as u
sys.path.insert(0, "scripts")
sp = u.spec_from_file_location("m", "scripts/02_signatures.py")
m = u.module_from_spec(sp); sp.loader.exec_module(m)
import tempfile
d = tempfile.mkdtemp(); log = os.path.join(d, "l.csv")
st = {"label":"exact","done":118,"total":416,"bytes_done":5_900_000,
      "bytes_total":324_000_000,"errors":0,"interrupted":True,"seconds":2.0}
now = dt.datetime.now()
m.log_run(log, st, now, now, 2, "t.db")
m.print_summary(st, now, now, log)
import csv
with open(log, encoding="utf-8-sig") as fh:
    rows = list(csv.DictReader(fh, delimiter=";"))
assert rows[0]["завершено"] == "прервано", rows[0]
assert rows[0]["длительность"], rows[0]
PY

step "6. Сквозной конвейер"
mkdir -p "$T/photo/A/deep/deeper" "$T/300/B"
"$PY" - "$T" <<'PY'
import sys, os, shutil
from PIL import Image
T = sys.argv[1]
def mk(p, w=60, h=40, ex=None):
    im = Image.new("RGB", (w, h), (10, 20, 30))
    dst = os.path.join(T, p)
    if ex:
        e = im.getexif()
        for k, v in ex.items():
            e[k] = v
        im.save(dst, exif=e)
    else:
        im.save(dst)
mk("photo/A/orig.jpg", 600, 400,
   {36867: "2008:12:24 13:24:00", 272: "COOLPIX"})
mk("photo/A/deep/deeper/buried.jpg", 100, 80)
mk("300/B/other.jpg", 120, 90)
shutil.copy(f"{T}/photo/A/orig.jpg", f"{T}/300/B/copy.jpg")
Image.open(f"{T}/photo/A/orig.jpg").resize((300,200)).save(f"{T}/300/B/small.jpg")
# Ещё две независимые пары точных копий: сквозной сценарий должен уметь
# задействовать все три вида решения на РАЗНЫХ файлах, иначе проверка
# вырождается в один случай.
for i in (1, 2):
    mk(f"photo/A/pair{i}.jpg", 640, 480, {36867: f"2010:0{i}:01 10:00:00"})
    shutil.copy(f"{T}/photo/A/pair{i}.jpg", f"{T}/300/B/pair{i}.jpg")
PY
DB="$T/t.db"; R="$T/rep"
"$PY" scripts/01_inventory.py --db "$DB" --csv "$R/i.csv" --quiet \
  --root "photo=$T/photo" --root "300photos=$T/300" >"$T/s1.txt" 2>&1
grep -q "ИТОГО" "$T/s1.txt" && ok "этап 1" || { bad "этап 1"; tail -5 "$T/s1.txt"; }
"$PY" scripts/02_signatures.py --db "$DB" --stage all --threads 4 --min-size 1 >"$T/s2.txt" 2>&1
# Проверяется КАЖДАЯ стадия отдельно: «этап 2 не упал» ещё не значит,
# что exif и phash отработали — без библиотек они молча пропускаются.
for st in exact exif phash; do
  # маркер — блок-сводка стадии; строки «готово» больше нет
  if grep -q "ЭТАП 2 / $st" "$T/s2.txt"; then ok "этап 2 / $st"
  else bad "этап 2 / $st не отработала"; grep -E "$st|пропущена|нет Pillow|нет imagehash" "$T/s2.txt" | head -3; fi
done
"$PY" -c "
import sqlite3,sys
c=sqlite3.connect('$DB')
sha=c.execute('SELECT COUNT(*) FROM sigs WHERE sha256 IS NOT NULL').fetchone()[0]
ph =c.execute('SELECT COUNT(*) FROM sigs WHERE phash  IS NOT NULL').fetchone()[0]
ex =c.execute('SELECT COUNT(*) FROM sigs WHERE exif_dt IS NOT NULL').fetchone()[0]
sys.exit(0 if sha and ph and ex else 1)" \
  && ok "сигнатуры реально записаны (sha256, phash, exif)" \
  || bad "в базе нет части сигнатур — стадия отработала вхолостую"
"$PY" scripts/03_report.py --db "$DB" --out "$R" --md >"$T/s3.txt" 2>&1
grep -q "exact: групп" "$T/s3.txt" && ok "этап 3" || { bad "этап 3"; tail -5 "$T/s3.txt"; }
grep -q "инварианты соблюдены" "$T/s3.txt" \
  && ok "самопроверка отчёта: инварианты на данных" \
  || { bad "самопроверка отчёта нашла проблемы"; grep -A5 "САМОПРОВЕРКА" "$T/s3.txt"; }
# Логика интерфейса: до появления этого шага JS не проверялся ничем
if command -v node >/dev/null 2>&1; then
  if node tests/test_app.js "$R/decision.html" >"$T/js.txt" 2>&1; then
    ok "логика отчёта (JS): $(grep -c '^  OK' "$T/js.txt") проверок"
  else
    bad "логика отчёта (JS)"; grep "FAIL" "$T/js.txt" | head -5
  fi
else
  echo "  node не установлен — логика интерфейса не проверена"
fi
[ -f "$R/decision.html" ] && [ -f "$R/decision_list.md" ] \
  && ok "отчёты созданы" || bad "отчёты"
grep -q "остаётся \[.*/photo/A/orig.jpg\]" "$R/decision_list.md" \
  && ok "оригинал с EXIF выбран как ОСТАВИТЬ" \
  || bad "выбор оригинала"

step "7. Инкрементальность и целостность"
# --no-csv обязателен: без него сводка ушла бы в reports/ проекта.
# Гейт не должен оставлять следов за пределами своего временного каталога.
"$PY" scripts/01_inventory.py --db "$DB" --quiet --no-csv \
  --root "photo=$T/photo" --root "300photos=$T/300" >"$T/s1b.txt" 2>&1
grep -q "пропущено без изменений" "$T/s1b.txt" && ok "повтор пропускает неизменившееся" \
  || { bad "инкрементальность"; grep ГОТОВО "$T/s1b.txt"; }
sleep 1.1; cp "$T/photo/A/orig.jpg" "$T/photo/A/added.jpg"
"$PY" scripts/01_inventory.py --db "$DB" --quiet --no-csv \
  --root "photo=$T/photo" --root "300photos=$T/300" >"$T/s1c.txt" 2>&1
"$PY" -c "
import sqlite3,sys
n=sqlite3.connect('$DB').execute(\"SELECT COUNT(*) FROM files WHERE name='added.jpg'\").fetchone()[0]
sys.exit(0 if n==1 else 1)" && ok "новый файл в старой папке замечен" \
  || bad "новый файл в старой папке ПОТЕРЯН"
"$PY" -c "
import sqlite3,sys
c=sqlite3.connect('$DB')
orph=c.execute('SELECT COUNT(*) FROM sigs s LEFT JOIN files f ON f.id=s.file_id WHERE f.id IS NULL').fetchone()[0]
integ=c.execute('PRAGMA integrity_check').fetchone()[0]
sys.exit(0 if orph==0 and integ=='ok' else 1)" \
  && ok "sigs не осиротели, база целостна" || bad "целостность базы"

if [ "${1:-}" != "fast" ]; then
step "8. Состязательные имена файлов"
"$PY" - "$T" <<'PY'
import sys, os
from PIL import Image
T = os.path.join(sys.argv[1], "evil")
os.makedirs(T, exist_ok=True)
names = ["=cmd|' /C calc'.jpg", "+1+1.jpg", "@SUM(A1).jpg", "-2+3.jpg",
         "--output=pwn.jpg", "a;b.jpg", 'q"w.jpg', "brk](http://evil.tld) [x.jpg",
         "[!danger] f.jpg", "<script>alert(1)</script>.jpg", "O'Brien.jpg",
         "a'; DROP TABLE files;--.jpg", "L"*150 + ".jpg"]
for n in names:
    try: Image.new("RGB",(40,30)).save(os.path.join(T,n), format="JPEG")
    except OSError: pass
open(os.path.join(T,"broken.jpg"),"wb").write(b"\xff\xd8notajpeg")
open(os.path.join(T,"zero.jpg"),"wb").close()
PY
EDB="$T/e.db"; ER="$T/erep"
"$PY" scripts/01_inventory.py --db "$EDB" --drive photo --quiet --csv "$ER/i.csv" \
  --root "photo=$T/evil" >/dev/null 2>&1
"$PY" scripts/02_signatures.py --db "$EDB" --stage all --threads 4 --min-size 1 >/dev/null 2>&1
"$PY" scripts/03_report.py --db "$EDB" --out "$ER" --md >/dev/null 2>&1
"$PY" -c "
import sqlite3,sys
n=sqlite3.connect('$EDB').execute('SELECT COUNT(*) FROM files').fetchone()[0]
sys.exit(0 if n>0 else 1)" && ok "таблица files цела после SQL-инъекции в имени" \
  || bad "SQL-инъекция"
"$PY" - <<PY && ok "CSV без формул" || bad "CSV formula injection"
import csv, glob, sys
bad=[]
for f in glob.glob("$ER/*.csv"):
    with open(f, encoding="utf-8-sig") as fh:
        for row in csv.reader(fh, delimiter=";"):
            bad += [c for c in row if c[:1] in ("=","+","-","@","\t","\r")]
sys.exit(1 if bad else 0)
PY
# опасен только разрыв блока: </script> должен встречаться ровно раз
[ "$(grep -o "</script>" "$ER/decision.html" | wc -l | tr -d " ")" = "1" ] \
  && ok "блок script не разорвать именем файла" || bad "XSS в HTML"
grep -qE '^\- \[ \] удалить \[[^]]*\]\(http' "$ER"/decision_list*.md \
  && bad "markdown link injection" || ok "markdown экранирован"

step "9. Доказательство read-only"
manifest() { find "$T/photo" "$T/300" -type f -exec sha256sum {} \; 2>/dev/null | sort
             find "$T/photo" "$T/300" -printf "%p|%s|%T@|%m\n" 2>/dev/null | sort; }
manifest > "$T/before.txt"
"$PY" scripts/01_inventory.py --db "$T/ro.db" --quiet --no-csv \
  --root "photo=$T/photo" --root "300photos=$T/300" >/dev/null 2>&1
"$PY" scripts/02_signatures.py --db "$T/ro.db" --stage all --threads 4 --min-size 1 >/dev/null 2>&1
"$PY" scripts/00_find_original.py orig --date "24.12.2008 13:24" --deep --quiet \
  --csv "$T/f.csv" --root "$T/photo" --root "$T/300" >/dev/null 2>&1
manifest > "$T/after.txt"
diff -q "$T/before.txt" "$T/after.txt" >/dev/null \
  && ok "ни один файл не изменён ($(wc -l < "$T/before.txt") объектов)" \
  || { bad "ФАЙЛЫ ИЗМЕНЕНЫ"; diff "$T/before.txt" "$T/after.txt" | head; }
fi

step "10. Гейт не оставил следов в проекте"
# Регрессия: два вызова 01_inventory без --no-csv писали сводки в reports/
# проекта, и за день там скопилось четыре десятка лишних файлов.
STRAY=$(find reports -newermt "-5 minutes" -name "inventory_*.csv" 2>/dev/null | wc -l | tr -d ' ')
[ "$STRAY" = "0" ] && ok "reports/ проекта не тронут" \
  || bad "гейт создал $STRAY файлов в reports/ проекта"

step "11. Сквозная проверка: отчёт → решения → 04_apply"
# Шов между интерфейсом и исполнителем. Обе стороны по отдельности были
# зелёными, когда подсказка называла один файл, а удалялся другой.
if command -v node >/dev/null 2>&1; then
  if node tests/e2e_decisions.js "$R/decision.html" \
       "$T/decisions.json" "$T/expect.json" >"$T/e2e1.txt" 2>&1; then
    cat "$T/e2e1.txt"
    "$PY" scripts/04_apply.py --decisions "$T/decisions.json" --move \
      --root "photo=$T/photo" --root "300photos=$T/300" \
      --quiet >"$T/e2e2.txt" 2>&1
    if "$PY" tests/e2e_verify.py "$T/expect.json" "$T/decisions.json" \
         >"$T/e2e3.txt" 2>&1; then
      cat "$T/e2e3.txt"
      ok "экран и исполнитель согласованы"
    else
      bad "экран обещает одно, 04_apply делает другое"
      cat "$T/e2e3.txt"
      tail -20 "$T/e2e2.txt"
    fi
  else
    bad "не удалось выгрузить решения из отчёта"
    tail -10 "$T/e2e1.txt"
  fi
else
  echo "  node не установлен — сквозная проверка пропущена"
fi

step "12. Документация не ссылается на файлы вне репозитория"
# У проекта есть локальные файлы (CONTRIBUTING.md, GITHUB_SETUP.md, LOCAL.md
# и прочие) — они в .gitignore. Ссылка на них из отслеживаемого файла даёт
# битую ссылку на GitHub, причём видит её только читатель, а не автор.
if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
  DANGLING=0
  for tracked in $(git ls-files '*.md'); do
    # Только настоящие ссылки [текст](файл): именно они ломаются на GitHub.
    # Упоминание файла в тексте — не ссылка, ничего не ломает, и запрещать
    # его значило бы запретить объяснять, почему файл локальный.
    for ref in $(grep -oE '\]\([A-Za-z0-9_.-]+\.(md|sh|py|json)\)' \
                 "$tracked" 2>/dev/null | tr -d ']()'); do
      [ -e "$ref" ] || continue                 # файла нет — не наша забота
      git ls-files --error-unmatch "$ref" >/dev/null 2>&1 && continue
      echo "  $tracked → $ref (файл вне репозитория)"
      DANGLING=$((DANGLING+1))
    done
  done
  [ "$DANGLING" = "0" ] && ok "ссылок на файлы вне репозитория нет" \
    || bad "битых на GitHub ссылок: $DANGLING"
else
  echo "  не git-репозиторий — проверка ссылок пропущена"
fi

printf "\n"
if [ "$FAIL" -eq 0 ]; then
  printf "\033[32m== QUALITY GATE ПРОЙДЕН ==\033[0m\n"
else
  printf "\033[31m== ПРОВАЛЕНО ПРОВЕРОК: %s ==\033[0m\n" "$FAIL"
fi
exit "$FAIL"
