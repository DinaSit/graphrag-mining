import { apiAction } from './auth.js';
import { showNotice } from './dialog.js';
import { $, apiJSON, clear, el, trunc, wrapQuote } from './dom.js';
import { docName, docsCache, loadDocs } from './state.js';
import { paintSourceDoc } from './view_sources.js';

// ==================================================================
// Экран Review: конвейер — один кандидат на весь экран
// ==================================================================
// Списка нет: кандидаты идут потоком, слева документ на странице цитаты,
// справа поля факта. Решение по кандидату сразу переводит к следующему.

// Поля кандидата, доступные эксперту. Список повторяет EDITABLE_FIELDS
// на стороне backend; цитаты и источника здесь нет намеренно — правка цитаты
// обесценила бы проверку «цитата есть в документе»
const FIELDS = [
  ['material', 'материал', 'text'],
  ['property', 'свойство', 'text'],
  ['process', 'процесс', 'text'],
  ['equipment', 'оборудование', 'text'],
  ['effect_direction', 'направление эффекта', 'direction'],
  ['effect_value', 'величина эффекта', 'number'],
  ['effect_unit', 'единица эффекта', 'text'],
  ['temperature_c', 'температура, °C', 'number'],
  ['duration_h', 'длительность, ч', 'number'],
  ['result_value', 'результат', 'number'],
  ['result_unit', 'единица результата', 'text'],
  ['sample', 'образец', 'text'],
  ['lab', 'лаборатория', 'text'],
  ['team', 'команда', 'text'],
];

const ISSUE_LABELS = {
  field_missing: 'не извлечено поле',
  quote_unconfirmed: 'цитата не подтверждена',
  number_unconfirmed: 'число не подтверждено',
  number_implausible: 'число неправдоподобно',
  unit_mismatch: 'единица не приводится',
  source_missing: 'нет источника',
};

const issuesOf = c => ((c.payload || {}).review_issues) || [];

export function viewReview(view){
  const wrap = el('div', 'belt-scr');
  const top = el('div', 'belt-top');
  const stage = el('div', 'belt');
  const docside = el('div', 'belt-doc');
  const work = el('div', 'belt-work');
  stage.append(docside, work);
  wrap.append(top, stage);
  view.append(wrap);

  const RV = { items: [], index: 0, docId: '', issue: '', busy: false, done: 0 };
  let inputs = new Map();

  // индекс отсчитывается по ОТФИЛЬТРОВАННОМУ списку: иначе выбор фильтра
  // меняет счётчик, но оставляет на экране кандидата из общей очереди
  const current = () => filteredItems()[RV.index] || null;

  function filteredItems(){
    let arr = RV.items;
    if (RV.docId) arr = arr.filter(c => c.source && c.source.document_id === RV.docId);
    if (RV.issue) arr = arr.filter(c => issuesOf(c).some(i => i.code === RV.issue));
    return arr;
  }

  // ---------- верхняя строка: имя документа заголовком + фильтры ----------

  function paintTop(){
    clear(top);
    const c = current();
    const full = c && c.source ? docName(c.source.document_id) : 'Проверка извлечённых фактов';
    const title = el('h3', 'belt-title', full);
    title.title = full;   // полное имя — подсказкой при наведении
    top.append(title);

    // Фильтры оформлены как в разделе «Документы» (эталон): подпись отдельным
    // приглушённым текстом, контрол — капсулой с собственной стрелкой
    const tools = el('div', 'belt-tools');
    const lbl = (text, ctrl) => { tools.append(el('span', 'dlbl', text)); tools.append(ctrl); };

    const docSel = document.createElement('select');
    docSel.className = 'dsel';
    const allDocs = el('option', null, 'все документы'); allDocs.value = '';
    docSel.append(allDocs);
    for (const d of (docsCache || [])){
      const o = el('option', null, trunc(d.filename || d.id, 44)); o.value = d.id; docSel.append(o);
    }
    docSel.value = RV.docId;
    docSel.addEventListener('change', () => { RV.docId = docSel.value; RV.index = 0; paintAll(); });
    lbl('документ:', docSel);

    const counts = {};
    for (const c2 of RV.items) for (const i of issuesOf(c2)) counts[i.code] = (counts[i.code] || 0) + 1;
    const issueSel = document.createElement('select');
    issueSel.className = 'dsel';
    const anyIssue = el('option', null, 'любое замечание'); anyIssue.value = '';
    issueSel.append(anyIssue);
    for (const [code, label] of Object.entries(ISSUE_LABELS)){
      if (!counts[code]) continue;
      const o = el('option', null, label + ' · ' + counts[code]); o.value = code; issueSel.append(o);
    }
    issueSel.value = RV.issue;
    issueSel.addEventListener('change', () => { RV.issue = issueSel.value; RV.index = 0; paintAll(); });
    lbl('замечание:', issueSel);
    top.append(tools);
  }

  // ---------- документ ----------

  function paintDoc(){
    clear(docside);
    const c = current();
    const box = el('div', 'belt-view');
    box.id = 'vdoc';                 // paintSourceDoc рисует просмотрщик сюда
    docside.append(box);
    // Нет кандидата (очередь пуста или фильтры ничего не отобрали) — левая
    // колонка остаётся пустой: причину называет правая, вторая надпись об этом
    // же только дублирует её. «Источник недоступен» относится к самому
    // кандидату и осмысленно, лишь когда кандидат есть, а ссылки у него нет
    if (c && c.source) paintSourceDoc(c.source);
    else if (c) box.append(el('div', 'vempty', 'Источник недоступен'));
  }

  // ---------- поля факта ----------

  function paintWork(){
    clear(work);
    inputs = new Map();
    const list = filteredItems();
    const c = current();
    if (!c){
      work.append(el('div', 'belt-empty',
        RV.items.length ? 'Под текущие фильтры ничего не попадает' : 'Всё проверено ✓'));
      return;
    }

    if (c.source && c.source.quote) work.append(el('div', 'belt-quote', wrapQuote(c.source.quote)));

    // поле с замечанием только помечено жёлтым: формулировка ушла в подсказку,
    // иначе строки причин растягивают форму вниз и она перестаёт помещаться
    const byField = {};
    for (const i of issuesOf(c)) (byField[i.field] = byField[i.field] || []).push(i);

    const form = el('div', 'belt-form');
    for (const [key, label, kind] of FIELDS){
      const bad = !!byField[key];
      const row = el('label', 'belt-f' + (bad ? ' bad' : ''));
      row.append(el('span', 'k', label));
      let input;
      if (kind === 'direction'){
        input = document.createElement('select');
        for (const [v, t] of [['', '—'], ['increase', 'рост'], ['decrease', 'снижение'], ['neutral', 'без изменений']]){
          const o = el('option', null, t); o.value = v; input.append(o);
        }
        input.value = c.payload[key] || '';
      } else {
        input = document.createElement('input');
        input.type = kind === 'number' ? 'number' : 'text';
        if (kind === 'number') input.step = 'any';
        input.value = c.payload[key] == null ? '' : String(c.payload[key]);
      }
      input.disabled = RV.busy;
      if (bad) row.title = byField[key].map(i => i.label).join('; ');
      inputs.set(key, [input, kind]);
      row.append(input);
      form.append(row);
    }
    work.append(form);

    // Вариант Б: три равные кнопки одним блоком — интерфейс не подсказывает,
    // какое решение «правильное»; под ними позиция в очереди и полоса прогресса
    const acts = el('div', 'belt-acts');
    const seg = el('div', 'belt-seg');
    const mk = (cls, text, fn) => {
      const b = el('button', cls, text);
      b.disabled = RV.busy;
      b.addEventListener('click', fn);
      seg.append(b);
      return b;
    };
    mk('ok', 'Подтвердить', () => decide('approve'));
    mk('no', 'Отклонить', () => decide('reject'));
    mk('sk', 'Пропустить', () => { RV.index += 1; paintAll(); });
    acts.append(seg);

    const pos = Math.min(RV.index + 1, list.length);
    acts.append(el('div', 'belt-count', pos + ' из ' + list.length));
    const bar = el('div', 'belt-bar');
    const fill = el('i');
    fill.style.width = (list.length ? (pos / list.length) * 100 : 0) + '%';
    bar.append(fill);
    acts.append(bar);
    work.append(acts);
  }

  function paintAll(){
    const list = filteredItems();
    if (RV.index >= list.length) RV.index = Math.max(0, list.length - 1);
    paintTop();
    paintDoc();
    paintWork();
  }

  function setBusy(b){
    RV.busy = b;
    for (const ctl of work.querySelectorAll('button, input, select')) ctl.disabled = b;
  }

  // Правки полей уходят вместе с решением: отдельной кнопки «сохранить» нет,
  // подтверждение означает «поля верны в том виде, в каком они на экране»
  function editedFields(c){
    const fields = {};
    let changed = false;
    for (const [key, [input, kind]] of inputs){
      const raw = input.value.trim();
      const value = kind === 'number' ? (raw === '' ? null : Number(raw)) : raw;
      const was = c.payload[key] == null ? (kind === 'number' ? null : '') : c.payload[key];
      if (String(value ?? '') !== String(was ?? '')) changed = true;
      fields[key] = value;
    }
    return changed ? fields : null;
  }

  async function decide(action){
    const c = current();
    if (!c || RV.busy) return;
    setBusy(true);
    try {
      if (action === 'approve'){
        const fields = editedFields(c);
        let state = c;
        if (fields){
          state = await apiAction('/review/facts/' + encodeURIComponent(c.id), {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ fields }),
          });
        }
        // правка могла сама провести кандидата через ворота — тогда подтверждать нечего
        if (state.status !== 'approved') await apiAction('/review/facts/' + encodeURIComponent(c.id) + '/approve', { method: 'POST' });
      } else {
        await apiAction('/review/facts/' + encodeURIComponent(c.id) + '/reject', { method: 'POST' });
      }
      RV.items = RV.items.filter(x => x.id !== c.id);
      RV.done += 1;
    } catch (e) {
      showNotice('Решение не сохранено', e.detail || e.message || 'ошибка');
    }
    setBusy(false);
    paintAll();
    pollReviewCount();
  }

  // Клавиши работают, хотя подсказок на экране нет: рука привыкает быстрее глаза
  const onKey = e => {
    if (!document.body.contains(wrap)) { document.removeEventListener('keydown', onKey); return; }
    const t = e.target;
    if (t && /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName) && e.key !== 'Enter') return;
    if (e.key === 'Enter'){ e.preventDefault(); decide('approve'); }
    else if (e.key === 'ArrowRight'){ RV.index += 1; paintAll(); }
    else if (e.key === 'ArrowLeft'){ RV.index = Math.max(0, RV.index - 1); paintAll(); }
  };
  document.addEventListener('keydown', onKey);

  work.append(el('div', 'belt-empty', 'Загрузка…'));
  Promise.all([apiJSON('/review/facts?status=pending_review'), loadDocs()])
    .then(([cands]) => {
      if (!document.body.contains(wrap)) return;
      RV.items = Array.isArray(cands) ? cands : [];
      paintAll();
    })
    .catch(e => {
      if (!document.body.contains(wrap)) return;
      clear(work);
      work.append(el('div', 'belt-empty', 'Не удалось загрузить очередь: ' + (e.message || 'ошибка сети')));
    });
}

// ==================================================================
// Красная точка Review
// ==================================================================
export async function pollReviewCount(){
  const dot = $('#review-dot');
  try {
    const data = await apiJSON('/review/count');
    dot.hidden = !(data && data.pending > 0);
  } catch (_) {
    dot.hidden = true; // эндпоинт недоступен — точку прячем
  }
}
setInterval(pollReviewCount, 30000);
