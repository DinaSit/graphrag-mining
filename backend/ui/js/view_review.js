import { $, apiJSON, clear, el, trunc, wrapQuote } from './dom.js';
import { docName, docsCache, fragLoc, loadDocs } from './state.js';
import { takeReviewDocPreset } from './view_docs.js';
import { plural } from './view_home.js';

// ==================================================================
// Экран Review: фильтры, сортировка, массовые действия, порции по 50
// ==================================================================
export const REVIEW_PAGE = 50;

export function viewReview(view){
  const wrap = el('div', 'review');
  const rhead = el('div', 'rhead'); // липкая шапка раздела: заголовок + фильтры + массовые действия
  const h = el('h3', null, 'Проверка извлечённых фактов');
  const tools = el('div', 'rtools'); tools.hidden = true;
  const bulk = el('div', 'rbulk'); bulk.hidden = true;
  const summary = el('div', 'rsummary'); summary.hidden = true;
  const list = el('div');
  const moreWrap = el('div');
  rhead.append(h, tools, bulk);
  wrap.append(rhead, summary, list, moreWrap);
  view.append(wrap);
  list.append(el('div', 'rempty', 'Загрузка…'));

  // docId может прийти пресетом из досье документа (кликабельные счётчики)
  const RV = { items: [], selected: new Set(), sortDir: 'desc', docId: takeReviewDocPreset(), minConf: 0, shown: REVIEW_PAGE, busy: false };
  let selAllCb, cntEl, okAllBtn, noAllBtn;

  // фильтры и сортировка — по ВСЕМУ загруженному набору
  function filtered(){
    let arr = RV.items;
    if (RV.docId) arr = arr.filter(c => c.source && c.source.document_id === RV.docId);
    if (RV.minConf > 0) arr = arr.filter(c => (Number(c.confidence) || 0) * 100 >= RV.minConf);
    return arr.slice().sort((a, b) => {
      const d = (Number(a.confidence) || 0) - (Number(b.confidence) || 0);
      return RV.sortDir === 'asc' ? d : -d;
    });
  }

  function buildTools(){
    clear(tools);
    const lbl = (text, ctrl) => { const l = el('label'); l.append(text + ' ', ctrl); return l; };

    const sort = document.createElement('select');
    for (const [v, t] of [['desc', 'по убыванию'], ['asc', 'по возрастанию']]){
      const o = el('option', null, t); o.value = v; sort.append(o);
    }
    sort.value = RV.sortDir;
    sort.addEventListener('change', () => { RV.sortDir = sort.value; RV.shown = REVIEW_PAGE; repaint(); });
    tools.append(lbl('уверенность:', sort));

    const docSel = document.createElement('select');
    const all = el('option', null, 'все документы'); all.value = '';
    docSel.append(all);
    for (const d of (docsCache || [])){
      const o = el('option', null, trunc(d.filename || d.id, 48)); o.value = d.id; docSel.append(o);
    }
    docSel.value = RV.docId; // пресет из досье отражается в селекте
    docSel.addEventListener('change', () => { RV.docId = docSel.value; RV.shown = REVIEW_PAGE; repaint(); });
    tools.append(lbl('документ:', docSel));

    const conf = document.createElement('input');
    conf.type = 'number'; conf.min = '0'; conf.max = '100'; conf.step = '5'; conf.placeholder = '0';
    conf.addEventListener('input', () => {
      RV.minConf = Math.max(0, Math.min(100, Number(conf.value) || 0));
      RV.shown = REVIEW_PAGE;
      repaint();
    });
    tools.append(lbl('уверенность от, %:', conf));
  }

  function buildBulk(){
    clear(bulk);
    const selall = el('label', 'selall');
    selAllCb = document.createElement('input');
    selAllCb.type = 'checkbox';
    selAllCb.addEventListener('change', () => {
      if (selAllCb.checked) for (const c of filtered()) RV.selected.add(c.id);
      else RV.selected.clear();
      repaint();
    });
    selall.append(selAllCb, document.createTextNode('Выделить все'));
    cntEl = el('span', 'cnt', '');
    okAllBtn = el('button', 'ok', 'Подтвердить');
    noAllBtn = el('button', 'no', 'Отклонить');
    okAllBtn.setAttribute('aria-label', 'Подтвердить выбранные');
    noAllBtn.setAttribute('aria-label', 'Отклонить выбранные');
    okAllBtn.addEventListener('click', () => bulkAct('approve'));
    noAllBtn.addEventListener('click', () => bulkAct('reject'));
    const grp = el('div', 'grp');
    grp.append(okAllBtn, noAllBtn);
    bulk.append(selall, cntEl, grp);
  }

  function updateBulkState(){
    if (!selAllCb) return;
    const f = filtered();
    const n = RV.selected.size;
    cntEl.textContent = 'выбрано: ' + n;
    selAllCb.checked = f.length > 0 && n === f.length;
    selAllCb.indeterminate = n > 0 && n < f.length;
    selAllCb.disabled = RV.busy || !f.length;
    okAllBtn.disabled = noAllBtn.disabled = RV.busy || !n;
  }

  // на время любой операции (bulk или одиночной) блокируем ВСЕ управляющие элементы,
  // чтобы кандидата нельзя было обработать дважды параллельно
  function setBusy(b){
    RV.busy = b;
    updateBulkState();
    for (const ctl of list.querySelectorAll('.racts button, .rc-chk')) ctl.disabled = b;
  }

  function repaint(){
    h.textContent = 'Проверка извлечённых фактов · ' + RV.items.length;
    tools.hidden = bulk.hidden = !RV.items.length;
    const f = filtered();
    // выделение всегда подмножество отфильтрованного набора
    const fIds = new Set(f.map(c => c.id));
    for (const id of Array.from(RV.selected)) if (!fIds.has(id)) RV.selected.delete(id);
    clear(list);
    clear(moreWrap);
    if (!RV.items.length){
      list.append(el('div', 'rempty', 'Всё проверено ✓'));
    } else if (!f.length){
      list.append(el('div', 'rempty', 'Под текущие фильтры ничего не попадает'));
    } else {
      for (const c of f.slice(0, RV.shown)) list.append(reviewCard(c));
      if (f.length > RV.shown){
        const more = el('button', 'rmore',
          'Показать ещё ' + Math.min(REVIEW_PAGE, f.length - RV.shown) + ' (скрыто ' + (f.length - RV.shown) + ')');
        more.addEventListener('click', () => { RV.shown += REVIEW_PAGE; repaint(); });
        moreWrap.append(more);
      }
    }
    updateBulkState();
  }

  function removeItems(ids){
    const gone = ids instanceof Set ? ids : new Set(ids);
    RV.items = RV.items.filter(c => !gone.has(c.id));
    for (const id of gone) RV.selected.delete(id);
  }

  function showProgress(text){
    summary.hidden = false;
    clear(summary);
    summary.append(document.createTextNode(text));
  }

  function showResult(action, okCount, failed){
    summary.hidden = false;
    clear(summary);
    summary.append(el('div', null,
      (action === 'approve' ? 'Подтверждено ' : 'Отклонено ') + okCount
      + (failed.length ? ', ошибок ' + failed.length : '')));
    if (failed.length){
      const ul = el('ul');
      for (const f of failed) ul.append(el('li', null, (f && f.id ? f.id + ': ' : '') + ((f && f.error) || 'ошибка')));
      summary.append(ul);
    }
  }

  // Одиночные вердикты по кандидату — общие для кнопок карточки и поштучной
  // деградации bulk; note передаётся телом запроса только непустым
  const approveOne = id =>
    apiJSON('/review/facts/' + encodeURIComponent(id) + '/approve', { method: 'POST' });
  const rejectOne = (id, note) => {
    const opts = { method: 'POST' };
    if (note){
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify({ note: note });
    }
    return apiJSON('/review/facts/' + encodeURIComponent(id) + '/reject', opts);
  };

  async function bulkAct(action){
    const ids = Array.from(RV.selected);
    if (!ids.length || RV.busy) return;
    let note = null;
    if (action === 'reject'){
      const v = prompt('Причина отклонения для всех выбранных (необязательно):', '');
      if (v === null) return;
      note = v.trim() || null;
    }
    setBusy(true);
    showProgress('Обработка: ' + ids.length + ' ' + plural(ids.length, 'кандидат', 'кандидата', 'кандидатов') + '…');
    let processed = [], failed = [];
    try {
      const data = await apiJSON('/review/facts/bulk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ candidate_ids: ids, action: action, note: note }),
      });
      failed = Array.isArray(data.failed) ? data.failed : [];
      const bad = new Set(failed.map(x => x && x.id));
      processed = ids.filter(id => !bad.has(id));
    } catch (e) {
      if (e.status !== 404){
        setBusy(false);
        showResult(action, 0, [{ id: null, error: e.detail || e.message || 'ошибка сети' }]);
        repaint();
        return;
      }
      // старый backend без bulk-эндпоинта — деградация: поштучно с прогрессом
      for (let i = 0; i < ids.length; i++){
        showProgress('Обработка ' + (i + 1) + ' / ' + ids.length + '…');
        try {
          if (action === 'approve') await approveOne(ids[i]);
          else await rejectOne(ids[i], note);
          processed.push(ids[i]);
        } catch (err) {
          failed.push({ id: ids[i], error: err.detail || err.message || 'ошибка' });
        }
      }
    }
    removeItems(processed);
    setBusy(false);
    showResult(action, processed.length, failed);
    repaint();
    pollReviewCount();
  }

  function reviewCard(c){
    const card = el('div', 'rcard');
    const chk = document.createElement('input');
    chk.type = 'checkbox';
    chk.className = 'rc-chk';
    chk.checked = RV.selected.has(c.id);
    chk.disabled = RV.busy; // repaint может случиться во время операции
    chk.setAttribute('aria-label', 'выбрать кандидата');
    chk.addEventListener('change', () => {
      if (chk.checked) RV.selected.add(c.id);
      else RV.selected.delete(c.id);
      updateBulkState();
    });
    const body = el('div', 'rc-body');
    card.append(chk, body);

    const p = c.payload || {};
    if (c.source && c.source.quote) body.append(el('div', 'rq', wrapQuote(c.source.quote)));

    const rx = el('div', 'rx');
    const bits = [];
    for (const [label, key] of [['материал', 'material'], ['процесс', 'process'], ['свойство', 'property']]){
      if (p[key]) bits.push([label, String(p[key])]);
    }
    bits.forEach(([label, value], i) => {
      if (i) rx.append(' · ');
      rx.append(label + ': ');
      rx.append(el('b', null, value));
    });
    body.append(rx);

    const nums = [];
    if (p.effect_direction) nums.push('эффект: ' + p.effect_direction + (p.effect_value != null ? ' на ' + p.effect_value + (p.effect_unit || '') : ''));
    if (p.temperature_c != null) nums.push('T = ' + p.temperature_c + ' °C');
    if (p.duration_h != null) nums.push('t = ' + p.duration_h + ' ч');
    if (p.result_value != null) nums.push('результат: ' + p.result_value + (p.result_unit ? ' ' + p.result_unit : ''));
    if (nums.length) body.append(el('div', 'rx', nums.join(' · ')));

    const meta = [];
    meta.push('confidence ' + Number(c.confidence || 0).toFixed(2));
    if (c.source && c.source.document_id)
      meta.push(docName(c.source.document_id) + ' · ' + fragLoc(c.source.document_id, c.source.page));
    body.append(el('div', 'rmeta', meta.join(' · ')));

    // замечания валидации: контрактный payload.validation.issues либо фактический number_validation.issues
    const validation = p.validation || p.number_validation || {};
    const issues = Array.isArray(validation.issues) ? validation.issues : [];
    for (const issue of issues) body.append(el('div', 'rwarn', String(issue)));
    if (c.review_note && !issues.length) body.append(el('div', 'rwarn', c.review_note));

    const acts = el('div', 'racts');
    const okBtn = el('button', 'ok', 'Подтвердить');
    const noBtn = el('button', 'no', 'Отклонить');
    okBtn.disabled = noBtn.disabled = RV.busy; // repaint может случиться во время операции
    const finish = () => {
      removeItems([c.id]);
      repaint();
      pollReviewCount();
    };
    okBtn.addEventListener('click', async () => {
      if (RV.busy) return;
      setBusy(true);
      try {
        await approveOne(c.id);
        setBusy(false);
        finish();
      } catch (e) {
        setBusy(false);
        alert('Не удалось подтвердить: ' + (e.detail || e.message));
      }
    });
    noBtn.addEventListener('click', async () => {
      if (RV.busy) return;
      const note = prompt('Причина отклонения (необязательно):', '');
      if (note === null) return;
      setBusy(true);
      try {
        await rejectOne(c.id, note.trim() || null);
        setBusy(false);
        finish();
      } catch (e) {
        setBusy(false);
        alert('Не удалось отклонить: ' + (e.detail || e.message));
      }
    });
    acts.append(okBtn, noBtn);
    body.append(acts);
    return card;
  }

  Promise.all([apiJSON('/review/facts?status=pending_review'), loadDocs()])
    .then(([cands]) => {
      if (!document.body.contains(list)) return;
      RV.items = Array.isArray(cands) ? cands : [];
      buildTools();
      buildBulk();
      repaint();
    })
    .catch(e => {
      if (!document.body.contains(list)) return;
      clear(list);
      list.append(el('div', 'rempty', 'Не удалось загрузить очередь: ' + (e.message || 'ошибка сети')));
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
setInterval(pollReviewCount, 60000);
