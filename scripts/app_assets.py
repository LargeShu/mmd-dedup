"""
Вёрстка и логика интерактивного отчёта.

Вынесено из 03_report.py отдельным модулем намеренно: CSS и JS полны фигурных
скобок, а в f-строках Python их пришлось бы удваивать — код стал бы нечитаемым
и правился бы с ошибками.

Здесь только статика. Данные подставляются одной подстановкой __DATA__.
"""

CSS = """
:root { color-scheme: light }
* { box-sizing: border-box }
body { font: 14px/1.5 -apple-system, "Segoe UI", sans-serif; margin: 0;
       color: #1a1a1a; background: #fbfbfc }
header { position: sticky; top: 0; z-index: 10; background: #fff;
         border-bottom: 1px solid #e2e2e5; padding: 10px 18px }
h1 { font-size: 17px; margin: 0 0 8px }
.bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap }
.bar input[type=search] { flex: 1 1 240px; min-width: 180px; padding: 6px 10px;
    border: 1px solid #ccc; border-radius: 6px; font: inherit }
button { font: 13px inherit; padding: 6px 12px; border: 1px solid #ccc;
         border-radius: 6px; background: #fff; cursor: pointer }
button:hover { background: #eef3fb }
button.primary { background: #2d6cdf; color: #fff; border-color: #2d6cdf }
button.primary:hover { background: #245ec5 }
label.chk { display: flex; align-items: center; gap: 6px; cursor: pointer;
            user-select: none; white-space: nowrap }
select { font: 13px inherit; padding: 5px 8px; border: 1px solid #ccc;
         border-radius: 6px; background: #fff }
#sortdir { min-width: 34px; font-size: 15px; line-height: 1 }
.stats { display: flex; gap: 18px; flex-wrap: wrap; margin-top: 8px;
         font-size: 13px; color: #555 }
.stats b { color: #1a1a1a; font-size: 15px }
main { padding: 14px 18px 60px }
.folder { border: 1px solid #e2e2e5; border-radius: 8px; margin-bottom: 8px;
          background: #fff }
.fhead { display: flex; align-items: center; gap: 10px; padding: 9px 12px;
         cursor: pointer; user-select: none }
.fhead:hover { background: #f4f7fc }
.fhead .name { font-family: ui-monospace, Menlo, monospace; font-weight: 600;
               flex: 1; word-break: break-all }
.fhead .meta { color: #666; font-size: 13px; white-space: nowrap }
.fhead .arrow { color: #999; transition: transform .12s }
.folder.open .arrow { transform: rotate(90deg) }
.fbody { display: none; padding: 4px 12px 12px; border-top: 1px solid #f0f0f2 }
.folder.open .fbody { display: block }
.row { display: flex; gap: 12px; align-items: flex-start; padding: 10px 0;
       border-bottom: 1px solid #f2f2f4 }
.row:last-child { border-bottom: 0 }
.row.done { opacity: .45 }
.row.kept { opacity: .62; background: #f6f7f9 }
.row.swap-on { box-shadow: inset 3px 0 0 #2d6cdf }
.row.kept .method { background: #eceef2; color: #555 }
.act { display: flex; flex-direction: column; align-items: center; gap: 6px;
       flex: 0 0 auto; padding-top: 40px }
.act button { font-size: 11px; padding: 2px 7px; border-radius: 12px }
.row.kept .act button { background: #e6e8ec; border-color: #c6c9d0;
                        font-weight: 500 }
.compact .act { padding-top: 0; flex-direction: row }
.act input[type=checkbox] { width: 18px; height: 18px; cursor: pointer }
.side { width: 300px; flex: 0 0 auto }
.side.keepside { border-left: 3px solid #8ec98e; padding-left: 10px }
.side.dupside { border-left: 3px solid #d9a3a3; padding-left: 10px }
.tag { font-size: 11px; font-weight: 700; letter-spacing: .04em;
       display: flex; align-items: center; gap: 8px }
.copy1 { font-size: 10px; padding: 1px 6px; font-weight: 400;
         margin-left: auto }
.keepside .tag { color: #2a7a2a }
.dupside .tag { color: #b34b4b }
.thumb { width: 100%; height: 150px; object-fit: contain; background: #f2f2f4;
         border-radius: 5px; display: block; margin: 4px 0 }
.noimg { width: 100%; height: 150px; background: #f2f2f4; border-radius: 5px;
         display: flex; align-items: center; justify-content: center;
         color: #aaa; font-size: 12px; margin: 4px 0 }
a.path { font-family: ui-monospace, Menlo, monospace; font-size: 11px;
         word-break: break-all; color: #2d6cdf; text-decoration: none }
a.path:hover { text-decoration: underline }
.ex { font-size: 11px; color: #777; margin-top: 3px }
.mid { flex: 1; min-width: 120px; font-size: 12px; color: #666; padding-top: 40px }
.method { display: inline-block; font-size: 10px; padding: 1px 7px;
          border-radius: 10px; margin-bottom: 4px; font-weight: 500 }
.row.m-exact { background: #f7fbf3 }
.row.m-exif  { background: #fdf9f0 }
.row.m-phash { background: #f7f6fd }
.row.m-exact .method { background: #EAF3DE; color: #3B6D11 }
.row.m-exif  .method { background: #FAEEDA; color: #854F0B }
.row.m-phash .method { background: #EEEDFE; color: #534AB7 }
.row.m-exact { border-left: 3px solid #639922 }
.row.m-exif  { border-left: 3px solid #BA7517 }
.row.m-phash { border-left: 3px solid #7F77DD }
.row { padding-left: 9px }
.tabs { display: flex; gap: 6px; flex-wrap: wrap }
.tab { font: 13px inherit; padding: 5px 12px; border: 1px solid #ccc;
       border-radius: 16px; background: #fff; cursor: pointer }
.tab.on { border-color: transparent; font-weight: 500 }
.tab[data-m=exact].on { background: #EAF3DE; color: #3B6D11 }
.tab[data-m=exif].on  { background: #FAEEDA; color: #854F0B }
.tab[data-m=phash].on { background: #EEEDFE; color: #534AB7 }
.tab[data-m=all].on   { background: #e9edf5; color: #33406b }
.hint { font-size: 12px; color: #666; margin-top: 6px }
.hint b { color: #1a1a1a; font-weight: 500 }
.compact .side { width: auto; flex: 1 }
.compact .thumb, .compact .noimg { display: none }

.compact .mid { padding-top: 0 }
.compact { padding: 6px 0 6px 9px }
.empty { color: #888; padding: 30px; text-align: center }
footer { position: fixed; bottom: 0; left: 0; right: 0; background: #fff;
         border-top: 1px solid #e2e2e5; padding: 8px 18px; font-size: 13px;
         display: flex; gap: 18px; align-items: center }
.saved { color: #2a7a2a }
"""

# Сырая строка: JS не должен проходить через экранирование Python.
# Обратная косая в коде («\\» в JS) иначе съедалась бы ещё до попадания
# в отчёт, и в браузер уходил бы синтаксически битый файл.
JS = r"""
const DATA = __DATA__;
const KEY = 'mmd2026:' + DATA.report_id;
let state = {};
let openFolders = new Set();
let method = 'exact';   // с чего начинать: механическая работа первой

const МЕТОДЫ = {
  all:   {имя: 'все',            подсказка: ''},
  exact: {имя: 'точные копии',   подсказка:
    'Файлы <b>побайтово одинаковы</b>. Превью не показываются — они идентичны. ' +
    'Решение механическое: можно отмечать папками.'},
  exif:  {имя: 'один кадр',      подсказка:
    'Совпали дата съёмки и камера: RAW+JPEG, экспорт, копия после редактора. ' +
    '<b>Проверьте выборочно</b> — RAW-пары иногда хранят намеренно.'},
  phash: {имя: 'похожие',        подсказка:
    '<b>Обязательно смотреть глазами.</b> Серии кадров подряд и разные снимки ' +
    'одной сцены закономерно попадают сюда, но дублями не являются.'},
};

function load() {
  try { state = JSON.parse(localStorage.getItem(KEY) || '{}'); }
  catch (e) { state = {}; }
}
function save() {
  try {
    localStorage.setItem(KEY, JSON.stringify(state));
    flash('сохранено');
  } catch (e) {
    // приватный режим или переполнение — предупреждаем честно, а не молча
    flash('НЕ СОХРАНЕНО: ' + e.name + ' — выгрузите решения в файл', true);
  }
}
let flashTimer = null;
function flash(msg, bad) {
  const el = document.getElementById('saved');
  el.textContent = msg;
  el.className = bad ? '' : 'saved';
  el.style.color = bad ? '#b00' : '';
  clearTimeout(flashTimer);
  if (!bad) flashTimer = setTimeout(() => { el.textContent = ''; }, 1500);
}

function human(n) {
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < 4) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + ' ' + u[i];
}

// Три состояния вместо двух. «Не отмечено» раньше значило одновременно
// «ещё не смотрел» и «смотрел, решил оставить обе» — из-за этого фильтр
// «только неразобранные» никогда не опустошался.
//   undefined — не разобрано
//   1         — удалить дубль, оставить предложенный оригинал
//   2         — оставить обе копии (осознанное решение, файлы связаны)
//   3         — НАОБОРОТ: удалить предложенный оригинал, оставить дубль
//
// Состояние 3 нужно потому, что автоматика судит по формальным признакам
// и не знает контекста: снимок в папке «Надежда Барабанова» осмысленнее
// того же файла в общей куче, хотя по размеру и дате они неразличимы.
const УДАЛИТЬ = 1, ОСТАВИТЬ_ОБЕ = 2, НАОБОРОТ = 3;

function decided(rid) { return state[rid] !== undefined; }
function toDelete(rid) { return state[rid] === УДАЛИТЬ; }
function keepBoth(rid) { return state[rid] === ОСТАВИТЬ_ОБЕ; }
function swapped(rid) { return state[rid] === НАОБОРОТ; }

// Сколько ещё строк опирается на этот файл как на оригинал. Если
// перевернуть такую пару, файл окажется и удаляемым, и оригиналом —
// 04_apply это отклонит, но предупредить лучше сразу.
function keeperUsedBy(path, exceptRid) {
  return DATA.rows.filter(r => r.keep.p === path && r.rid !== exceptRid &&
                               state[r.rid] !== ОСТАВИТЬ_ОБЕ).length;
}

// Кто исчезнет по этой строке. Одна функция на весь отчёт: подсказка,
// предупреждение и экспорт обязаны отвечать одинаково.
function жертва(r) {
  return swapped(r.rid) ? r.keep : r.dup;
}

// Предупреждение о потере последней копии.
//
// Файл, стоящий здесь как «ОСТАВИТЬ», в другой папке может быть строкой
// «УДАЛИТЬ» — и наоборот. Отметив обе строки, человек уносит в карантин
// все копии кадра. 04_apply такую пару отклонит («ОРИГИНАЛ ТОЖЕ ОТМЕЧЕН»),
// но узнать об этом на сотне решений постфактум — плохая замена
// предупреждению в тот момент, когда решение принимается.
function предупредитьОПоследнейКопии(rid) {
  const r = ПО_RID.get(rid);
  if (!r) return;
  const уйдёт = жертва(r);
  const сколько = keeperUsedBy(уйдёт.p, rid);
  if (сколько) {
    flash('осторожно: ' + base(уйдёт.p) + ' — оригинал ещё для ' + сколько +
          ' строк. Если отметить и их, кадр исчезнет целиком', true);
  }
}

function setState(rid, v) {
  if (v === undefined) delete state[rid]; else state[rid] = v;
}

function stats() {
  let n = 0, bytes = 0, kept = 0;
  for (const r of DATA.rows) {
    if (toDelete(r.rid)) { n++; bytes += r.size; }
    else if (swapped(r.rid)) { n++; bytes += r.keep.s; }
    else if (keepBoth(r.rid)) kept++;
  }
  document.getElementById('st_marked').textContent = n.toLocaleString('ru');
  document.getElementById('st_bytes').textContent = human(bytes);
  document.getElementById('st_kept').textContent = kept.toLocaleString('ru');
  document.getElementById('st_left').textContent =
      (DATA.rows.length - n - kept).toLocaleString('ru');
}

function matches(r, q) {
  if (!q) return true;
  q = q.toLowerCase();
  return r.folder.toLowerCase().includes(q) ||
         r.dup.p.toLowerCase().includes(q) ||
         r.keep.p.toLowerCase().includes(q);
}

function byMethod(r) { return method === 'all' || r.method === method; }

// Строки, разложенные по папкам один раз при загрузке.
//
// Без индекса visibleRows фильтровала все 56 000 строк на КАЖДУЮ из 807 папок:
// 45 миллионов сравнений на один пересчёт счётчиков. На тестовом стенде в два
// десятка файлов это незаметно, на реальном архиве — заморозка вкладки.
const ПО_ПАПКАМ = (() => {
  const m = new Map();
  for (const r of DATA.rows) {
    if (!m.has(r.folder)) m.set(r.folder, []);
    m.get(r.folder).push(r);
  }
  return m;
})();

function visibleRows(folder, q, onlyOpen) {
  return (ПО_ПАПКАМ.get(folder) || []).filter(
      r => byMethod(r) && matches(r, q) && (!onlyOpen || !decided(r.rid)));
}

// ---------------------------------------------------------------- сортировка
//
// Задача выбора «с чего начать» — планирование под ограниченным ресурсом:
// времени и внимания у человека конечное количество. Жадное правило, дающее
// наибольший результат к любому моменту, — сортировка по отношению
// «выгода / стоимость» (правило Смита в теории расписаний), а не по
// абсолютной выгоде.
//
// Стоимость решений здесь РАЗНАЯ, и это важнее, чем кажется. Папка точных
// копий снимается одним нажатием «удалить всю папку» — сколько бы в ней ни
// было строк. Папку с похожими надо смотреть глазами построчно. Поэтому
// 1012 точных копий на 9.3 ГБ стоят одного решения, а пять похожих
// на 200 МБ — пяти. По абсолютному размеру они встали бы в правильном
// порядке случайно, а не по существу.
function метрики(folder, q) {
  const rows = visibleRows(folder, q, false);
  let байтВсего = 0, байтОстаток = 0, нерешено = 0;
  let естьExact = false, прочих = 0;
  for (const r of rows) {
    байтВсего += r.size;
    if (!decided(r.rid)) {
      байтОстаток += r.size;
      нерешено++;
      if (r.method === 'exact') естьExact = true; else прочих++;
    }
  }
  // Стоимость в «решениях»: вся exact-часть папки — одно нажатие.
  const стоимость = (естьExact ? 1 : 0) + прочих;
  return {
    всего: rows.length, байтВсего, байтОстаток, нерешено, стоимость,
    выгода: стоимость ? байтОстаток / стоимость : 0,
  };
}

const СОРТИРОВКИ = {
  выгода:   {имя: 'выгода за решение', как: м => м.выгода},
  остаток:  {имя: 'неразобранный объём', как: м => м.байтОстаток},
  объём:    {имя: 'общий объём', как: м => м.байтВсего},
  нерешено: {имя: 'неразобранных строк', как: м => м.нерешено},
  дубли:    {имя: 'всего дублей', как: м => м.всего},
  путь:     {имя: 'путь', как: null},
};
let sortKey = 'выгода';
let sortDir = -1;                    // -1 — по убыванию, 1 — по возрастанию

// Порядок папок — СНИМОК, а не живая величина.
//
// Если пересчитывать его после каждого решения, список плывёт под руками:
// отметил строку — папки перескочили, место потеряно. Порядок обновляется
// только по явному действию: смена сортировки, поиск, вкладка метода,
// кнопка «пересортировать».
let порядок = new Map();

function пересортировать() {
  const q = document.getElementById('q').value.trim();
  const имена = DATA.folders.map(f => f.f);
  let список;
  if (sortKey === 'путь') {
    список = имена.slice().sort((a, b) => a.localeCompare(b, 'ru'));
    if (sortDir < 0) список.reverse();
  } else {
    const как = СОРТИРОВКИ[sortKey].как;
    const вес = new Map(имена.map(n => [n, как(метрики(n, q))]));
    // Ничья разрешается путём: иначе порядок равных папок скачет
    // между перерисовками без всякой причины.
    список = имена.slice().sort((a, b) =>
      (вес.get(b) - вес.get(a)) * (sortDir < 0 ? 1 : -1) ||
      a.localeCompare(b, 'ru'));
  }
  порядок = new Map(список.map((n, i) => [n, i]));
}

function сохранитьВид() {
  try {
    localStorage.setItem(KEY + ':вид', JSON.stringify({sortKey, sortDir}));
  } catch (e) { /* приватный режим — не беда, порядок не данные */ }
}

function загрузитьВид() {
  try {
    const v = JSON.parse(localStorage.getItem(KEY + ':вид') || '{}');
    if (СОРТИРОВКИ[v.sortKey]) sortKey = v.sortKey;
    if (v.sortDir === 1 || v.sortDir === -1) sortDir = v.sortDir;
  } catch (e) { /* оставляем умолчание */ }
}

// «Скрыть решённые» обязана скрывать решённые.
//
// Раньше она просто перерисовывала список, а прятал строки фильтр
// «только неразобранные». При снятом фильтре нажатие не делало ничего:
// кнопка обещала действие, которого не происходило. Теперь она сама
// включает фильтр — это и есть то, о чём её просят.
function скрытьРешённые() {
  const ф = document.getElementById('onlyopen');
  const было = ф.checked;
  ф.checked = true;
  renderFolders(true);
  flash(было ? 'список обновлён' : 'включён фильтр «только неразобранные»');
}

function renderSort() {
  const sel = document.getElementById('sort');
  sel.innerHTML = Object.keys(СОРТИРОВКИ).map(k =>
    '<option value="' + k + '"' + (k === sortKey ? ' selected' : '') + '>' +
    СОРТИРОВКИ[k].имя + '</option>').join('');
  document.getElementById('sortdir').textContent = sortDir < 0 ? '↓' : '↑';
  document.getElementById('sortdir').title =
      sortDir < 0 ? 'сначала большие' : 'сначала малые';
}

function renderTabs() {
  const счёт = {all: DATA.rows.length};
  for (const r of DATA.rows) счёт[r.method] = (счёт[r.method] || 0) + 1;
  const порядок = ['exact', 'exif', 'phash', 'all'];
  document.getElementById('tabs').innerHTML = порядок
    .filter(m => счёт[m])
    .map(m => '<button class="tab' + (m === method ? ' on' : '') +
              '" data-m="' + m + '">' + МЕТОДЫ[m].имя +
              ' <span style="opacity:.7">' + счёт[m].toLocaleString('ru') +
              '</span></button>').join('');
  document.getElementById('hint').innerHTML = МЕТОДЫ[method].подсказка;
  document.querySelectorAll('.tab').forEach(b => {
    b.addEventListener('click', () => {
      method = b.dataset.m;
      renderTabs();
      пересортировать();     // состав папок изменился — порядок тоже
      renderFolders(true);
    });
  });
}

function card(c, kind, tag) {
  const img = c.t
      ? '<img class="thumb" loading="lazy" src="thumbs/' + c.t + '" alt="">'
      : '<div class="noimg">нет превью</div>';
  // Ссылка file:// работает не во всех браузерах: переход на локальный файл
  // со страницы, открытой тоже как файл, Chrome и Firefox часто блокируют
  // молча. Поэтому рядом всегда есть кнопка копирования полного пути —
  // она работает везде.
  return '<div class="side ' + kind + '">' +
         '<div class="tag">' + tag +
         '<button class="copy1" data-p="' + esc(c.p) +
         '" title="скопировать полный путь">копировать путь</button></div>' +
         '<a class="path" href="' + c.u + '" title="открыть файл">' + img + '</a>' +
         '<a class="path" href="' + c.u + '">' + esc(c.p) + '</a>' +
         '<div class="ex">' + human(c.s) + ' · ' + esc(c.m) + '</div></div>';
}

function copyText(text) {
  const ok = () => flash('путь скопирован');
  const no = () => flash('скопировать не удалось', true);
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(ok, no);
    return;
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); ok(); } catch (e) { no(); }
  document.body.removeChild(ta);
}
function esc(s) {
  return String(s).replace(/[&<>"]/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function rowClasses(r) {
  return 'row m-' + r.method +
    (toDelete(r.rid) ? ' done' : '') +
    (keepBoth(r.rid) ? ' kept' : '') +
    (swapped(r.rid) ? ' swap-on' : '') +
    (r.method === 'exact' ? ' compact' : '');
}

// Имя файла из полного пути. Разделитель может быть любым: пути приходят
// и из macOS, и из Windows, а отчёт открывают не обязательно там, где собрали.
function base(p) {
  const s = String(p);
  return s.slice(Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\')) + 1);
}

// Подсказки называют КОНКРЕТНЫЙ файл, а не сторону.
//
// Раньше у флажка была неизменная подпись «отметить дубль к удалению».
// После переворота удаляется левый файл, а подсказка продолжала утверждать
// обратное — то есть подсказка расходилась с действием ровно в том случае,
// когда пользователь и так неуверен. Цена ошибки здесь — потерянный снимок,
// поэтому текст собирается из состояния строки и всегда содержит имя файла.
function hintDelete(r) {
  // Кто исчезнет: без переворота — r.dup (рисуется СЛЕВА как «удалить»),
  // после переворота — r.keep (уезжает ВПРАВО). Это же различие управляет
  // экспортом, и подсказка обязана следовать за ним, а не за интуицией.
  const жертва = swapped(r.rid) ? r.keep : r.dup;
  const сторона = swapped(r.rid) ? 'ПРАВЫЙ' : 'левый';
  if (decided(r.rid) && !keepBoth(r.rid)) {
    return 'снять решение (сейчас удаляется ' + сторона.toLowerCase() +
           ': ' + base(жертва.p) + ')';
  }
  return 'отметить к удалению ' + сторона + ' файл: ' + base(жертва.p);
}

function hintSwap(r) {
  return swapped(r.rid)
    ? 'вернуть как было: удалять ' + base(r.dup.p)
    : 'поменять местами: оставить ' + base(r.dup.p) +
      ', удалять ' + base(r.keep.p);
}

function rowInner(r) {
  return '<div class="act">' +
      // Переворот ТОЖЕ приводит к удалению — значит флажок обязан стоять.
      // Иначе строка молча удаляет файл при снятой галке: расхождение
      // между тем, что видно, и тем, что произойдёт.
      '<input type="checkbox" title="' + esc(hintDelete(r)) + '"' +
      (toDelete(r.rid) || swapped(r.rid) ? ' checked' : '') + '>' +
      '<button class="keepboth" title="осознанно оставить обе копии">' +
      (keepBoth(r.rid) ? 'обе ✓' : 'обе') + '</button>' +
      '<button class="swap" title="' + esc(hintSwap(r)) + '">' +
      (swapped(r.rid) ? '⇄ ✓' : '⇄') + '</button>' +
    '</div>' +
    // при перевороте стороны меняются ролями, а не только подписями
    card(r.dup,  swapped(r.rid) ? 'keepside' : 'dupside',
         swapped(r.rid) ? 'ОСТАВИТЬ' : 'УДАЛИТЬ') +
    '<div class="mid"><span class="method">' + r.method + '</span><br>' +
    (keepBoth(r.rid) ? '<b>оставлены обе</b>'
     : swapped(r.rid) ? '<b>← оставлен левый</b>' : 'дубль файла →') + '</div>' +
    card(r.keep, swapped(r.rid) ? 'dupside' : 'keepside',
         swapped(r.rid) ? 'УДАЛИТЬ' : 'ОСТАВИТЬ');
}

// Обновление ОДНОЙ строки без перерисовки списка.
//
// Раньше любое решение вызывало renderFolders(), строка исчезала под фильтром
// «только неразобранные», и убедиться глазами, что получилось задуманное,
// было нельзя — особенно при перевороте, где как раз и надо проверить,
// что стороны поменялись правильно.
//
// Теперь решённые строки остаются на месте до явного «скрыть решённые».
const ПО_RID = new Map(DATA.rows.map(r => [r.rid, r]));

// Перерисовка строки БЕЗ пересчёта счётчиков.
//
// Разделение не косметическое: updateCounters обходит все папки, и вызов
// его внутри цикла по тысяче строк даёт квадратичную работу — вкладка
// зависала на «удалить всю папку». Счётчики считаются один раз, после цикла.
function paintRow(rid) {
  const r = ПО_RID.get(rid);
  const el = document.querySelector('.row[data-rid="' + rid + '"]');
  if (!r || !el) return;
  el.className = rowClasses(r);
  el.innerHTML = rowInner(r);
}

function updateRow(rid) {
  paintRow(rid);
  updateCounters();
}

function updateCounters() {
  stats();
  document.querySelectorAll('.folder').forEach(div => {
    const rows = visibleRows(div.dataset.f,
                             document.getElementById('q').value.trim(), false);
    const left = rows.filter(r => !decided(r.rid)).length;
    const m = div.querySelector('.fhead .meta');
    if (m) {
      m.textContent = rows.length + ' дубл. · ' +
          human(rows.reduce((a, r) => a + r.size, 0)) + ' · не решено ' + left;
    }
  });
}

function дублей(n) {
  const d = n % 10, s = n % 100;
  if (d === 1 && s !== 11) return n + ' дубль';
  if (d >= 2 && d <= 4 && (s < 12 || s > 14)) return n + ' дубля';
  return n + ' дублей';
}

// Пустая папка обязана объяснить, почему она пустая.
//
// «Нечего показывать» после массовой отметки читается как «всё пропало»:
// пользователь только что нажал кнопку и не понимает, сработала она или
// сломала отчёт. Причин пустоты ровно две, и они разные по смыслу —
// разобрано и скрыто фильтром, либо не подошло под поиск.
function пустаяПапка(el, folder, q) {
  const всего = visibleRows(folder, q, false).length;
  if (!всего) {
    el.innerHTML = '<div class="empty">под текущий поиск в этой папке ' +
                   'ничего не подходит</div>';
    return;
  }
  el.innerHTML = '<div class="empty">' + дублей(всего) +
    ' в этой папке разобраны — строки скрыты фильтром ' +
    '«только неразобранные».<br><br>' +
    '<button class="shownow">показать решённые</button></div>';
  el.querySelector('.shownow').addEventListener('click', () => {
    document.getElementById('onlyopen').checked = false;
    renderFolders(true);
  });
}

function renderFolderBody(el, folder) {
  const q = document.getElementById('q').value.trim();
  const onlyOpen = document.getElementById('onlyopen').checked;
  const rows = visibleRows(folder, q, onlyOpen);
  if (!rows.length) { пустаяПапка(el, folder, q); return; }
  el.innerHTML = rows.map(r =>
      '<div class="' + rowClasses(r) + '" data-rid="' + r.rid + '">' +
      rowInner(r) + '</div>').join('');
  bindBody(el);
}

// Делегирование: обработчики висят на теле папки, а не на каждой кнопке.
// Иначе после обновления строки на месте они бы отвалились.
function bindBody(el) {
  if (el.dataset.bound) return;
  el.dataset.bound = '1';

  el.addEventListener('click', ev => {
    const b = ev.target.closest('button');
    if (!b) return;
    ev.preventDefault();
    ev.stopPropagation();
    if (b.classList.contains('copy1')) { copyText(b.dataset.p); return; }

    const row = b.closest('.row');
    if (!row) return;
    const rid = row.dataset.rid;

    if (b.classList.contains('keepboth')) {
      // повторное нажатие снимает решение — передумать можно всегда
      setState(rid, keepBoth(rid) ? undefined : ОСТАВИТЬ_ОБЕ);
    } else if (b.classList.contains('swap')) {
      setState(rid, swapped(rid) ? undefined : НАОБОРОТ);
      if (swapped(rid)) предупредитьОПоследнейКопии(rid);
    } else {
      return;
    }
    save();
    updateRow(rid);          // строка остаётся на месте: результат виден
  });

  el.addEventListener('change', ev => {
    if (ev.target.type !== 'checkbox') return;
    const row = ev.target.closest('.row');
    if (!row) return;
    const rid = row.dataset.rid;
    setState(rid, ev.target.checked ? УДАЛИТЬ : undefined);
    if (ev.target.checked) предупредитьОПоследнейКопии(rid);
    save();
    updateRow(rid);
  });
}

function renderFolders(keepOpen) {
  const q = document.getElementById('q').value.trim();
  const onlyOpen = document.getElementById('onlyopen').checked;
  const main = document.getElementById('list');
  if (!порядок.size) пересортировать();
  const items = DATA.folders.filter(f => {
    const rows = visibleRows(f.f, q, onlyOpen);
    return rows.length > 0;
  // Папки, которых нет в снимке (появились после смены вкладки), уходят
  // в конец, а не пропадают и не перемешивают остальных.
  }).sort((a, b) => (порядок.get(a.f) ?? 1e9) - (порядок.get(b.f) ?? 1e9));
  if (!items.length) {
    main.innerHTML = '<div class="empty">Ничего не найдено. ' +
        'Снимите фильтр или измените запрос.</div>';
    return;
  }
  main.innerHTML = items.map(f => {
    const rows = visibleRows(f.f, q, onlyOpen);
    const left = rows.filter(r => !decided(r.rid)).length;
    const bytes = rows.reduce((a, r) => a + r.size, 0);
    const open = keepOpen && openFolders.has(f.f);
    // Величину, по которой отсортировано, показываем рядом: иначе порядок
    // выглядит произвольным, а «выгоду за решение» не угадать глазами.
    const м = метрики(f.f, q);
    const подпись = sortKey === 'выгода' && м.выгода
        ? ' · ' + human(м.выгода) + '/решение' : '';
    return '<div class="folder' + (open ? ' open' : '') + '" data-f="' +
        esc(f.f) + '">' +
      '<div class="fhead"><span class="arrow">▶</span>' +
      '<span class="name">' + esc(f.f) + '</span>' +
      '<span class="meta">' + rows.length + ' дубл. · ' + human(bytes) +
      ' · не решено ' + left + подпись + '</span>' +
      '<button class="copypath" title="скопировать путь к папке">путь</button>' +
      '<button class="markall">удалить всю папку</button>' +
      '<button class="keepall" title="осознанно оставить обе копии для всей папки">' +
      'оставить обе</button></div>' +
      '<div class="fbody"></div></div>';
  }).join('');

  main.querySelectorAll('.folder').forEach(div => {
    const folder = div.dataset.f;
    const body = div.querySelector('.fbody');
    if (div.classList.contains('open')) renderFolderBody(body, folder);
    div.querySelector('.copypath').addEventListener('click', ev => {
      ev.stopPropagation();
      copyText(folder);
    });
    div.querySelector('.fhead').addEventListener('click', ev => {
      // любая кнопка в шапке — своё действие, а не сворачивание папки
      if (ev.target.tagName === 'BUTTON') return;
      div.classList.toggle('open');
      if (div.classList.contains('open')) {
        openFolders.add(folder);
        renderFolderBody(body, folder);      // рендерим только при раскрытии
      } else {
        openFolders.delete(folder);
        body.innerHTML = '';
      }
    });
    const массово = (значение) => (ev) => {
      ev.stopPropagation();
      const rows = visibleRows(folder, document.getElementById('q').value.trim(),
                               false);
      const надо = rows.some(r => state[r.rid] !== значение);
      rows.forEach(r => setState(r.rid, надо ? значение : undefined));
      if (надо && значение === УДАЛИТЬ) {
        // На папке предупреждаем ОДИН раз с числом: сто всплывающих
        // сообщений подряд не читают, их закрывают.
        const опасных = rows.filter(
            r => keeperUsedBy(жертва(r).p, r.rid)).length;
        if (опасных) {
          flash('осторожно: в ' + опасных + ' строках удаляемый файл — ' +
                'оригинал для других строк; проверьте их', true);
        }
      }
      save();
      // Строки обновляются НА МЕСТЕ, а не перерисовкой папки.
      //
      // Перерисовка при включённом «только неразобранные» опустошала папку
      // целиком: тысяча строк исчезала разом, без объяснения. Пользователь
      // не мог убедиться, что нажал именно то, что хотел, — а отменять
      // приходилось вслепую. Правило то же, что для одиночных решений:
      // решённое остаётся видимым до явного «скрыть решённые».
      // Красим только строки, которые сейчас в DOM: остальные не видны,
      // а обход тысячи отсутствующих узлов — та же лишняя работа.
      div.querySelectorAll('.row[data-rid]').forEach(el =>
          paintRow(el.dataset.rid));
      updateCounters();
      flash(надо
        ? (значение === УДАЛИТЬ ? 'отмечено к удалению: ' : 'оставлены обе: ') +
          rows.length + ' — нажмите кнопку ещё раз, чтобы снять'
        : 'решение снято с ' + rows.length + ' строк');
    };
    div.querySelector('.markall').addEventListener('click', массово(УДАЛИТЬ));
    div.querySelector('.keepall').addEventListener('click',
                                                   массово(ОСТАВИТЬ_ОБЕ));
  });
}

function exportJson() {
  const payload = {
    формат: 'mmd2026-решения',
    отчёт: DATA.report_id,
    сохранено: new Date().toISOString(),
    к_удалению: DATA.rows.filter(r => toDelete(r.rid) || swapped(r.rid))
      .map(r => swapped(r.rid)
        // перевёрнутая пара: удаляется то, что автоматика предлагала оставить
        ? {rid: r.rid, id: r.keep.id, путь: r.keep.p, байт: r.keep.s, метод: r.method,
           оставить: r.dup.p, переопределено: true}
        : {rid: r.rid, id: r.dup.id, путь: r.dup.p, байт: r.size,
           метод: r.method, оставить: r.keep.p}),
    // Решения «оставить обе» тоже сохраняются: иначе после перегенерации
    // отчёта разобранные пары снова считались бы неразобранными
    оставлены_обе: DATA.rows.filter(r => keepBoth(r.rid)).map(r => ({
      rid: r.rid, путь: r.dup.p, вторая: r.keep.p, метод: r.method,
    })),
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)],
                        { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'decisions_' + DATA.report_id + '.json';
  a.click();
  URL.revokeObjectURL(a.href);
}

function importJson(file) {
  const fr = new FileReader();
  fr.onload = () => {
    try {
      const d = JSON.parse(fr.result);
      const del = d.к_удалению || [];
      const keep = d.оставлены_обе || [];
      state = {};
      del.forEach(x => { if (x.rid) state[x.rid] = x.переопределено ? НАОБОРОТ : УДАЛИТЬ; });
      keep.forEach(x => { if (x.rid) state[x.rid] = ОСТАВИТЬ_ОБЕ; });
      save(); stats(); renderTabs(); renderFolders(true);
      flash('загружено: к удалению ' + del.length +
            ', оставлено обе ' + keep.length);
    } catch (e) { flash('файл не прочитан: ' + e.message, true); }
  };
  fr.readAsText(file);
}

document.addEventListener('DOMContentLoaded', () => {
  load();
  загрузитьВид();
  stats();
  renderSort();
  renderTabs();
  пересортировать();
  renderFolders(false);
  // Поиск меняет состав папок — порядок пересчитывается вместе с ним.
  document.getElementById('q').addEventListener('input', () => {
    пересортировать();
    renderFolders(true);
  });
  document.getElementById('sort').addEventListener('change', ev => {
    sortKey = ev.target.value;
    сохранитьВид();
    renderSort();
    пересортировать();
    renderFolders(true);
  });
  document.getElementById('sortdir').addEventListener('click', () => {
    sortDir = -sortDir;
    сохранитьВид();
    renderSort();
    пересортировать();
    renderFolders(true);
  });
  document.getElementById('resort').addEventListener('click', () => {
    пересортировать();
    renderFolders(true);
    flash('порядок пересчитан');
  });
  document.getElementById('onlyopen').addEventListener('change',
      () => renderFolders(true));
  document.getElementById('export').addEventListener('click', exportJson);
  document.getElementById('importfile').addEventListener('change', ev => {
    if (ev.target.files[0]) importJson(ev.target.files[0]);
  });
  document.getElementById('hidedone').addEventListener('click', скрытьРешённые);
  document.getElementById('expand').addEventListener('click', () => {
    DATA.folders.forEach(f => openFolders.add(f.f));
    renderFolders(true);
  });
  document.getElementById('collapse').addEventListener('click', () => {
    openFolders.clear();
    renderFolders(true);
  });
});
"""

BODY = """
<header>
  <h1>Решения по дубликатам — __TITLE__</h1>
  <div class="bar">
    <input type="search" id="q" placeholder="поиск по пути или имени файла">
    <label class="chk"><input type="checkbox" id="onlyopen" checked>
      только неразобранные</label>
    <label class="chk" title="чем сортировать папки">сначала:
      <select id="sort"></select></label>
    <button id="sortdir" title="направление">↓</button>
    <button id="resort" title="пересчитать порядок по текущим решениям">пересортировать</button>
    <button id="hidedone">скрыть решённые</button>
    <button id="expand">развернуть всё</button>
    <button id="collapse">свернуть всё</button>
    <button class="primary" id="export">Скачать решения (JSON)</button>
    <label class="chk"><button onclick="document.getElementById('importfile').click()">Загрузить</button></label>
    <input type="file" id="importfile" accept="application/json" hidden>
  </div>
  <div class="tabs" id="tabs"></div>
  <div class="hint" id="hint"></div>
  <div class="stats">
    <span>всего дублей <b>__TOTAL__</b></span>
    <span>отмечено к удалению <b id="st_marked">0</b></span>
    <span>освободится <b id="st_bytes">0 B</b></span>
    <span>оставлено обе <b id="st_kept">0</b></span>
    <span>не решено <b id="st_left">0</b></span>
  </div>
</header>
<main id="list"></main>
<footer>
  <span>Файлы не удаляются: отчёт только фиксирует решение.</span>
  <span style="color:#888">Решённые строки остаются видимыми — уберите их
  кнопкой «скрыть решённые», когда убедитесь, что всё верно.</span>
  <span id="saved"></span>
  <span style="margin-left:auto;color:#aaa;font-size:12px">__VERSION__</span>
</footer>
"""
