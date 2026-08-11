// Сквозная проверка: отчёт → JSON решений → 04_apply.
//
// Зачем отдельный файл. Два самых опасных дефекта проекта жили не внутри
// отчёта и не внутри 04_apply, а В ШВЕ между ними: интерфейс подписывал
// строку одним файлом, а исполнитель удалял другой. Каждая сторона по
// отдельности проверялась и была «зелёной».
//
// Этот скрипт нажимает кнопки так, как это делает человек, выгружает
// НАСТОЯЩИЙ JSON и рядом кладёт ожидания: какие файлы обязаны исчезнуть,
// а какие обязаны остаться. Дальше check.sh скармливает JSON в 04_apply
// и сверяет диск с ожиданиями.
//
// Запуск:
//   node tests/e2e_decisions.js отчёт.html решения.json ожидания.json

const fs = require('fs');

const [файлОтчёта, файлРешений, файлОжиданий] = process.argv.slice(2);
if (!файлОтчёта || !файлРешений || !файлОжиданий) {
  console.error('нужно: отчёт.html решения.json ожидания.json');
  process.exit(2);
}

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

const html = fs.readFileSync(файлОтчёта, 'utf8');
const js = html.split('<script>')[1].split('</script>')[0]
               .replace(/<\\\//g, '</');

const сценарий = `
// Три решения, по одному на каждый вид. Берём строки разных файлов:
// одна и та же пара в двух ролях сделала бы проверку бессмысленной.
const занято = new Set();
function свободная() {
  return DATA.rows.find(r => !занято.has(r.dup.p) && !занято.has(r.keep.p) &&
    (занято.add(r.dup.p), занято.add(r.keep.p), true));
}

const обычная = свободная();
const перевёрнутая = свободная();
const обе = свободная();

const исчезнут = [];
const останутся = [];

if (обычная) {
  setState(обычная.rid, УДАЛИТЬ);
  // Без переворота удаляется r.dup — тот, что нарисован СЛЕВА как «удалить»
  исчезнут.push(обычная.dup.p);
  останутся.push(обычная.keep.p);
}
if (перевёрнутая) {
  setState(перевёрнутая.rid, НАОБОРОТ);
  // После переворота удаляется бывший оригинал
  исчезнут.push(перевёрнутая.keep.p);
  останутся.push(перевёрнутая.dup.p);
}
if (обе) {
  setState(обе.rid, ОСТАВИТЬ_ОБЕ);
  останутся.push(обе.dup.p, обе.keep.p);
}

exportJson();

// Ожидания строим НЕ из выгруженного JSON, а из того, что показано
// в строке: подписи сторон и правило «переворот меняет роли». Иначе
// проверка сверяла бы экспорт сам с собой.
const ожидания = {
  исчезнут,
  останутся,
  подписи: [обычная, перевёрнутая].filter(Boolean).map(r => ({
    rid: r.rid,
    // что человек видит на карточке «УДАЛИТЬ»
    удалить_на_экране: swapped(r.rid) ? r.keep.p : r.dup.p,
    подсказка: hintDelete(r),
  })),
};
__ВЫВОД__(JSON.stringify(ожидания));
`;

let ожиданияJSON = null;
global.__ВЫВОД__ = (s) => { ожиданияJSON = s; };

eval(js + сценарий);

if (!выгружено) {
  console.error('экспорт не сработал: отчёт не отдал JSON');
  process.exit(1);
}
fs.writeFileSync(файлРешений, выгружено);
fs.writeFileSync(файлОжиданий, ожиданияJSON);

const d = JSON.parse(выгружено);
const о = JSON.parse(ожиданияJSON);
console.log(`  сценарий: к удалению ${d.к_удалению.length}, ` +
            `оставлено обе ${(d.оставлены_обе || []).length}, ` +
            `ожидаем исчезновения ${о.исчезнут.length}`);
