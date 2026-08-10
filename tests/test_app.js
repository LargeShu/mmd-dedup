// Тесты логики интерактивного отчёта.
//
// До этого файла весь JS — состояния решений, экспорт, переворот пары —
// не проверялся ничем. Именно там нашлась коллизия ключей: состояние
// хранилось по id файла, а один файл попадает в несколько строк.
//
// Запуск:  node tests/test_app.js путь/к/decision.html
// В гейте вызывается автоматически, если node установлен.

const fs = require('fs');

const файл = process.argv[2];
if (!файл) { console.error('нужен путь к decision.html'); process.exit(2); }

// --- минимальный DOM: ровно столько, сколько трогает наш код
function узел() {
  return {
    textContent: '', innerHTML: '', className: '', value: '', checked: false,
    style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {}, click() {},
    querySelector: () => узел(), querySelectorAll: () => [],
    closest: () => null, appendChild() {}, removeChild() {}, select() {},
  };
}
const элементы = {};
global.document = {
  getElementById: id => элементы[id] || (элементы[id] = узел()),
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, createElement: () => узел(),
  body: { appendChild() {}, removeChild() {} },
};
global.localStorage = {
  _d: {},
  getItem(k) { return this._d[k] ?? null; },
  setItem(k, v) { this._d[k] = v; },
};
global.window = { isSecureContext: false };
global.navigator = {};
let выгружено = null;
global.Blob = function (parts) { выгружено = parts[0]; };
global.URL = { createObjectURL: () => 'blob:x', revokeObjectURL() {} };

// --- достаём код и данные из готового отчёта
const html = fs.readFileSync(файл, 'utf8');
const js = html.split('<script>')[1].split('</script>')[0]
               .replace(/<\\\//g, '</');

let провалов = 0;
function проверить(имя, условие, пояснение) {
  if (условие) { console.log('  OK   ' + имя); }
  else { console.log('  FAIL ' + имя + (пояснение ? ' — ' + пояснение : ''));
         провалов++; }
}

const тесты = `
// ---------- ключи решений
проверить('ключи решений уникальны',
  new Set(DATA.rows.map(r => r.rid)).size === DATA.rows.length,
  'один файл попадает в несколько строк с разными оригиналами');

проверить('в ключе есть метод',
  DATA.rows.every(r => r.rid.startsWith(r.method + ':')));

проверить('у каждой строки есть оригинал',
  DATA.rows.every(r => r.keep && r.keep.p && r.dup && r.dup.p));

проверить('дубль и оригинал — разные файлы',
  DATA.rows.every(r => r.dup.p !== r.keep.p));

// ---------- три состояния
const rid = DATA.rows[0].rid;
setState(rid, УДАЛИТЬ);
проверить('удаление считается решением', decided(rid) && toDelete(rid));
setState(rid, ОСТАВИТЬ_ОБЕ);
проверить('«оставить обе» — тоже решение',
  decided(rid) && !toDelete(rid) && keepBoth(rid),
  'иначе фильтр «неразобранные» никогда не опустеет');
setState(rid, НАОБОРОТ);
проверить('переворот не путается с удалением',
  swapped(rid) && !toDelete(rid) && !keepBoth(rid));
setState(rid, undefined);
проверить('решение снимается', !decided(rid));

// ---------- отрисовка строки
setState(rid, НАОБОРОТ);
const r0 = DATA.rows[0];
const html0 = rowInner(r0);
проверить('при перевороте левая сторона становится «оставить»',
  html0.indexOf('keepside') < html0.indexOf('dupside'));
проверить('переворот подписан в строке', html0.includes('оставлен левый'));
проверить('класс строки помечает переворот',
  rowClasses(r0).includes('swap-on'));

// Подсказка обязана называть тот файл, который реально исчезнет:
// расхождение здесь стоит снимка.
проверить('подсказка флажка следует за переворотом',
  hintDelete(r0).includes(base(r0.dup.p)) &&
  !hintDelete(r0).includes('правый файл'),
  'после переворота удаляется левый, а подсказка обещала правый');
проверить('подсказка переворота предлагает вернуть как было',
  hintSwap(r0).includes('вернуть'));
setState(rid, undefined);
проверить('без переворота подсказка называет правый файл',
  hintDelete(r0).includes(base(r0.keep.p)) &&
  hintDelete(r0).includes('правый'));
проверить('подсказка попадает в разметку строки',
  rowInner(r0).includes(esc(hintDelete(r0))));
проверить('имя файла вырезается из пути обоих ОС',
  base('C:\\\\a\\\\b\\\\x.jpg') === 'x.jpg' &&
  base('/a/b/y.jpg') === 'y.jpg');

// ---------- цена массовой операции
//
// Заморозка вкладки на «удалить всю папку» случилась из-за того, что
// перерисовка строки тянула за собой пересчёт счётчиков: тысяча строк —
// тысяча обходов всех папок. Тест закрепляет разделение.
Object.keys(state).forEach(k => delete state[k]);
setState(DATA.rows[0].rid, УДАЛИТЬ);
stats();
const счётчикДо = document.getElementById('st_marked').textContent;
setState(DATA.rows[1].rid, УДАЛИТЬ);
paintRow(DATA.rows[1].rid);
проверить('перерисовка строки не пересчитывает счётчики',
  document.getElementById('st_marked').textContent === счётчикДо,
  'иначе массовая операция даёт квадратичную работу и вешает вкладку');
updateRow(DATA.rows[1].rid);
проверить('updateRow счётчики всё же обновляет',
  document.getElementById('st_marked').textContent !== счётчикДо);
Object.keys(state).forEach(k => delete state[k]);

проверить('индекс по папкам совпадает с прямым перебором',
  DATA.folders.every(f => visibleRows(f.f, '', false).length ===
    DATA.rows.filter(r => r.folder === f.f && byMethod(r)).length),
  'индекс вводился ради скорости и не должен менять результат');

// ---------- пустая папка объясняет причину
const папка = DATA.folders[0].f;
const короб = { innerHTML: '', querySelector: () => ({ addEventListener() {} }) };
пустаяПапка(короб, папка, '');
проверить('пустая папка объясняет, что строки скрыты фильтром',
  короб.innerHTML.includes('скрыты фильтром'),
  '«нечего показывать» после массовой отметки читается как «всё пропало»');
проверить('пустая папка предлагает показать решённые',
  короб.innerHTML.includes('показать решённые'));
пустаяПапка(короб, папка, 'заведомо-ненайдётся-zzz');
проверить('пустой поиск объясняется иначе, чем разобранная папка',
  короб.innerHTML.includes('поиск') &&
  !короб.innerHTML.includes('скрыты фильтром'));

проверить('склонение «дубль» по-русски',
  дублей(1).endsWith('дубль') && дублей(2).endsWith('дубля') &&
  дублей(5).endsWith('дублей') && дублей(11).endsWith('дублей') &&
  дублей(21).endsWith('дубль') && дублей(112).endsWith('дублей'));

// ---------- экспорт
const дубль = DATA.rows.find(r => !DATA.rows.some(
  x => x !== r && x.dup.p === r.keep.p));
setState(DATA.rows[0].rid, УДАЛИТЬ);
setState(DATA.rows[1].rid, НАОБОРОТ);
setState(DATA.rows[2].rid, ОСТАВИТЬ_ОБЕ);
exportJson();
const d = JSON.parse(выгружено);
проверить('к удалению попали обычное и перевёрнутое',
  d.к_удалению.length === 2);
проверить('«оставить обе» вынесено отдельно',
  d.оставлены_обе.length === 1,
  'иначе после перегенерации решение потеряется');
const пере = d.к_удалению.find(x => x.переопределено);
проверить('в перевёрнутой паре удаляется бывший оригинал',
  пере && пере.путь === DATA.rows[1].keep.p &&
  пере.оставить === DATA.rows[1].dup.p);
проверить('каждая запись экспорта несёт ключ строки',
  d.к_удалению.every(x => x.rid) && d.оставлены_обе.every(x => x.rid));

// ---------- «оставить обе» никогда не идёт в удаление
проверить('«оставить обе» не попадает к удалению',
  !d.к_удалению.some(x => x.rid === DATA.rows[2].rid),
  'это привело бы к потере файла, который решили сохранить');

// ---------- счётчик места
Object.keys(state).forEach(k => delete state[k]);
setState(DATA.rows[0].rid, УДАЛИТЬ);
stats();
проверить('счётчик учитывает только удаляемое',
  document.getElementById('st_marked').textContent === '1');
`;

eval(js + тесты);

console.log();
if (провалов) { console.log('ПРОВАЛЕНО: ' + провалов); process.exit(1); }
console.log('логика отчёта: все проверки пройдены');
