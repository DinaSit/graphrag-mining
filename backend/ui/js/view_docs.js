import { apiJSON, clear, el } from './dom.js';
import { gotoRoute, render, routeName } from './router.js';
import { docFragCache, loadDocs, sciLabel } from './state.js';
import { plural } from './view_home.js';
import { docPreviewSrc, openDocView } from './view_sources.js';

// ==================================================================
// Экран «Документы»
// ==================================================================
// Раздел «Документы» — список + постоянное досье: слева компактные строки,
// справа ВСЕГДА открыта панель выбранного документа (превью, обоснование LLM,
// счётчики графа, действия). При входе выбран первый документ.
export let dockDocId = null; // id документа в досье; сохраняется между перерисовками раздела

// Пресет фильтра Review по документу — кликабельные счётчики досье ведут в
// Review, уже отфильтрованный по этому документу; потребляется одноразово
let reviewDocPreset = '';
export function openReviewForDoc(docId){
  reviewDocPreset = docId;
  gotoRoute('review');
}
// Пресет забирается ровно один раз: экран Review читает значение и сбрасывает
// его, чтобы следующий заход не унаследовал старый фильтр
export function takeReviewDocPreset(){
  const preset = reviewDocPreset;
  reviewDocPreset = '';
  return preset;
}

export function viewDocs(view){
  const wrap = el('div', 'docs');
  // заголовок раздела размещён в ЛЕВОЙ колонке: досье справа занимает раздел
  // от самого верха
  const dt = el('div', 'dt');
  dt.append(el('h3', null, 'База знаний'));
  const hint = el('span', 'hint', '');
  dt.append(hint);
  const split = el('div', 'dsplit');
  const left = el('div', 'dcol');
  const lst = el('div', 'dscroll');
  left.append(dt, lst);
  const dock = el('aside', 'ddock');
  split.append(left, dock);
  wrap.append(split);
  view.append(wrap);

  loadDocs(true).then(list => {
    if (!document.body.contains(lst)) return;
    if (!list.length){
      hint.textContent = '0 документов';
      clear(lst);
      lst.append(el('div', 'vempty', 'Документов пока нет — загрузите первые через плашку в шапке.'));
      dock.style.display = 'none'; // при пустом корпусе досье не отображается
      return;
    }
    const hiddenCount = list.filter(d => d.hidden).length;

    const renderRows = () => {
      const visible = docsFiltered(list);
      hint.textContent = (visible.length < list.length
        ? visible.length + ' из ' + list.length + ' ' + plural(list.length, 'документа', 'документов', 'документов')
        : list.length + ' ' + plural(list.length, 'документ', 'документа', 'документов'))
        + (hiddenCount ? ' · ' + hiddenCount + ' ' + plural(hiddenCount, 'скрыт', 'скрыто', 'скрыто') + ' из графа' : '');
      clear(lst);
      if (!visible.length){
        lst.append(el('div', 'vempty', 'Под выбранные фильтры ничего не попало.'));
        dock.style.display = 'none';
        return;
      }
      dock.style.display = '';
      for (const d of visible) lst.append(docListRow(d, lst, dock));
      // досье открыто всегда: выбранный ранее документ, иначе — первый видимый
      const opened = (dockDocId && visible.find(x => x.id === dockDocId)) || visible[0];
      openDock(opened, lst, dock);
    };

    dt.after(docControls(list, renderRows));
    renderRows();
  });
}

// --- фильтры и сортировка списка документов (сохраняются между перерисовками) ---
export let docSciOnly = false;          // галочка «научность»: только научные документы
export let docTypesSel = new Set();     // выбранные типы публикаций; пусто — не выбрано (все)
export let docSort = '';                // '' (не выбрано) | 'year-asc' (ранние) | 'year-desc' (поздние)
export let docQuery = '';               // поиск по названию (ё-толерантный)

export const foldE = s => String(s).toLowerCase().replace(/ё/g, 'е');

export function docsFiltered(list){
  const q = foldE(docQuery.trim());
  const out = list.filter(d => {
    if (docSciOnly && d.is_scientific !== true) return false;
    if (docTypesSel.size && !docTypesSel.has(d.doc_type)) return false;
    if (q && !foldE(d.filename || d.id).includes(q)) return false;
    return true;
  });
  // «не выбрано» — порядок как пришёл с сервера; документы без года — в конце
  if (docSort){
    out.sort((a, b) => docSort === 'year-asc'
      ? (a.year ?? Infinity) - (b.year ?? Infinity)
      : (b.year ?? -Infinity) - (a.year ?? -Infinity));
  }
  return out;
}

export const _SVG_CHEVRON = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg>';

// Выпадающий мультивыбор типов публикаций: пока в корпусе нет типов — в
// списке только «не выбрано»; типы пополняются из классифицированных
// документов, выбрать можно несколько
export function docTypeDropdown(list, onChange){
  const types = [...new Set(list.map(d => d.doc_type).filter(Boolean))].sort();
  docTypesSel = new Set([...docTypesSel].filter(t => types.includes(t))); // типы, отсутствующие в корпусе, удаляются из выбора
  const dd = el('div', 'dd');
  const btn = el('button', 'dsel ddbtn');
  const txt = el('span', null, '');
  btn.append(txt);
  btn.insertAdjacentHTML('beforeend', _SVG_CHEVRON);
  const setLabel = () => {
    const sel = [...docTypesSel];
    txt.textContent = sel.length === 0 ? 'не выбрано'
      : sel.length <= 2 ? sel.join(', ') : 'выбрано: ' + sel.length;
  };
  setLabel();

  const pop = el('div', 'ddpop');
  if (!types.length){
    pop.append(el('div', 'ddempty', 'не выбрано'));
  } else {
    for (const t of types){
      const lab = el('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = docTypesSel.has(t);
      cb.addEventListener('change', () => {
        if (cb.checked) docTypesSel.add(t); else docTypesSel.delete(t);
        setLabel();
        onChange(); // строки перерисовываются, попап остаётся открытым
      });
      lab.append(cb, ' ' + t);
      pop.append(lab);
    }
  }
  const close = e => {
    if (!dd.contains(e.target)){
      dd.classList.remove('open');
      document.removeEventListener('click', close);
    }
  };
  btn.addEventListener('click', () => {
    const opening = !dd.classList.contains('open');
    dd.classList.toggle('open', opening);
    if (opening) setTimeout(() => document.addEventListener('click', close), 0);
    else document.removeEventListener('click', close);
  });
  dd.append(btn, pop);
  return dd;
}

export function docControls(list, onChange){
  const bar = el('div', 'dctrl');

  // слева: галочка «научность» — показывает только научные документы
  const lab = el('label', 'dchk');
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = docSciOnly;
  cb.addEventListener('change', () => { docSciOnly = cb.checked; onChange(); });
  lab.append(cb, ' научность');
  bar.append(lab);

  // поиск по названию: сужает список на лету, совпадение подсвечивается в строках
  const srch = document.createElement('input');
  srch.type = 'search';
  srch.className = 'dsrch';
  srch.placeholder = 'поиск по названию…';
  srch.value = docQuery;
  srch.addEventListener('input', () => { docQuery = srch.value; onChange(); });
  bar.append(srch);

  // справа, слева направо: тип публикации, затем сортировка по новизне
  const right = el('div', 'dright');
  right.append(el('span', 'dlbl', 'тип публикации:'));
  right.append(docTypeDropdown(list, onChange));
  right.append(el('span', 'dlbl', 'по новизне:'));
  const sort = document.createElement('select');
  sort.className = 'dsel';
  for (const [value, label] of [['', 'не выбрано'], ['year-asc', 'сначала ранние'], ['year-desc', 'сначала поздние']]){
    const o = document.createElement('option');
    o.value = value; o.textContent = label;
    sort.append(o);
  }
  sort.value = docSort;
  sort.addEventListener('change', () => { docSort = sort.value; onChange(); });
  right.append(sort);
  bar.append(right);
  return bar;
}

// Словесная форма научности — для подписей источников (sciLabel)
export function sciWord(v){ return v === true ? 'научный' : v === false ? 'ненаучный' : '—'; }

// Заголовок строки: совпадение с поисковой строкой подсвечено (ё-толерантно)
export function docTitleEl(name){
  const t = el('div', 't');
  const q = foldE(docQuery.trim());
  if (!q){ t.textContent = name; return t; }
  const folded = foldE(name);
  let pos = 0;
  let i = folded.indexOf(q);
  if (i < 0){ t.textContent = name; return t; }
  while (i >= 0){
    if (i > pos) t.append(name.slice(pos, i));
    t.append(el('span', 'hl', name.slice(i, i + q.length)));
    pos = i + q.length;
    i = folded.indexOf(q, pos);
  }
  if (pos < name.length) t.append(name.slice(pos));
  return t;
}

export function docListRow(d, lst, dock){
  const row = el('div', 'docrow' + (d.hidden ? ' dim' : ''));
  row.dataset.id = d.id;
  row.append(docTitleEl(d.filename || d.id));
  // мета-строка без прочерков: показываем только известные признаки —
  // тип и научность бейджами, остальное текстом; вердикты LLM дополнят строку
  const m = el('div', 'm');
  if (d.doc_type) m.append(badge(d.doc_type, 'typ'));
  if (d.is_scientific === true) m.append(badge('научный', 'sci'));
  else if (d.is_scientific === false) m.append(badge('ненаучный', 'rep'));
  const parts = [];
  if (d.year != null) parts.push(String(d.year));
  if (d.origin === 'ru') parts.push('РФ');
  else if (d.origin === 'foreign') parts.push('зарубежн.');
  if (d.element_count != null) parts.push(d.element_count + ' фрагм.');
  if (d.status === 'processing') parts.push('обрабатывается…');
  if (d.status === 'failed') parts.push('обработка не удалась');
  if (d.hidden) parts.push('скрыт из графа');
  m.append(parts.join(' · '));
  row.append(m);
  row.addEventListener('click', () => openDock(d, lst, dock));
  return row;
}

// Иконка-кнопка досье: только SVG, без видимых подписей; title даёт
// подсказку при наведении, aria-label — доступность
export function dockBtn(cls, label, svg){
  const b = el('button', 'ic-btn' + (cls ? ' ' + cls : ''));
  b.setAttribute('aria-label', label);
  b.title = label;
  b.innerHTML = svg;
  return b;
}
export const _SVG_EYE = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6z"/><circle cx="12" cy="12" r="2.6"/></svg>';
export const _SVG_EYE_OFF = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6z"/><circle cx="12" cy="12" r="2.6"/><line x1="4" y1="20" x2="20" y2="4"/></svg>';
export const _SVG_TRASH = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16M9 7V5h6v2m-8 0 1 13h8l1-13"/></svg>';
export const _SVG_DOWNLOAD = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 4v11m0 0 4-4m-4 4-4-4M5 20h14"/></svg>';
export const _SVG_REFRESH = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6"/></svg>';

export function openDock(d, lst, dock){
  dockDocId = d.id;
  for (const r of lst.querySelectorAll('.docrow')) r.classList.toggle('sel', r.dataset.id === d.id);
  clear(dock);
  const dk = el('div', 'dk');

  // Структура досье — по согласованному макету: превью → «почему научный» →
  // «в графе знаний» → действия. Имя и признаки не дублируются — они в
  // выбранной строке списка слева
  // Превью: единые правила с полностраничным просмотром (docPreviewSrc);
  // ни оригинала-PDF, ни конвертированного превью — статичная заглушка
  const pvUrl = docPreviewSrc(d);
  const pv = el('div', 'pv' + (pvUrl ? '' : ' eh'));
  if (pvUrl){
    const fr = document.createElement('iframe');
    fr.src = pvUrl;
    fr.loading = 'lazy';
    pv.append(fr);
  } else pv.textContent = 'превью недоступно';
  // разворот в полностраничный просмотр (вкладка источников) — кнопка
  // в углу превью, появляется при наведении; iframe перехватывает клики
  const go = dockBtn('pvgo', 'Развернуть',
    '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M15 3h6v6M9 21H3v-6M21 3l-8 8M3 21l8-8"/></svg>');
  go.addEventListener('click', () => openDocView(d.id));
  pv.append(go);
  dk.append(pv);

  if (d.trait_reason){
    dk.append(el('div', 'lbl', d.is_scientific === false ? 'почему «ненаучный»' : 'почему «научный»'));
    dk.append(el('div', 'why', d.trait_reason));
  }

  dk.append(el('div', 'lbl', 'в графе знаний'));
  // счётчики кликабельны: фрагменты разворачивают документ, факты и
  // эксперименты открывают Review с фильтром по этому документу
  const stats = el('div', 'stats');
  const statBtn = (text, tip, fn) => {
    const b = el('button', 'stbtn', text);
    b.title = tip;
    b.addEventListener('click', fn);
    return b;
  };
  stats.append(statBtn(
    (d.element_count != null ? d.element_count : '—') + ' '
      + plural(d.element_count || 0, 'фрагмент', 'фрагмента', 'фрагментов'),
    'Развернуть', () => openDocView(d.id)));
  if (d.facts_count != null){
    stats.append(' · ', statBtn(
      d.facts_count + ' ' + plural(d.facts_count, 'факт', 'факта', 'фактов'),
      'Открыть в Review', () => openReviewForDoc(d.id)));
  }
  if (d.experiments_count != null){
    stats.append(' · ', statBtn(
      d.experiments_count + ' ' + plural(d.experiments_count, 'эксперимент', 'эксперимента', 'экспериментов'),
      'Открыть в Review', () => openReviewForDoc(d.id)));
  }
  if (d.status === 'processing') stats.append(' · обрабатывается…');
  if (d.status === 'failed') stats.append(' · обработка не удалась');
  dk.append(stats);

  const acts = el('div', 'acts');
  // на маршруте docs render() принудительно перезагружает список (viewDocs →
  // loadDocs(true)) — результат отдельного fetch перед ним был бы отброшен
  const rerender = async () => { if (routeName() === 'docs') render(); else await loadDocs(true); };

  const eye = dockBtn(d.hidden ? 'eye-off' : null, d.hidden ? 'Вернуть в Граф' : 'Скрыть из Графа',
    d.hidden ? _SVG_EYE_OFF : _SVG_EYE);
  eye.addEventListener('click', async () => {
    try {
      await apiJSON('/documents/' + encodeURIComponent(d.id) + '/visibility', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hidden: !d.hidden }),
      });
      await rerender();
    } catch (e) {
      alert('Не удалось изменить видимость: ' + (e.detail || e.message));
    }
  });
  acts.append(eye);

  const dl = el('a', 'ic-btn');
  dl.setAttribute('aria-label', 'Скачать');
  dl.title = 'Скачать';
  dl.href = '/documents/' + encodeURIComponent(d.id) + '/original';
  dl.target = '_blank';
  dl.rel = 'noopener noreferrer';
  dl.innerHTML = _SVG_DOWNLOAD;
  acts.append(dl);

  const rf = dockBtn(null, 'Перезагрузить документ в систему', _SVG_REFRESH);
  rf.addEventListener('click', async () => {
    if (d.status === 'processing') return; // уже идёт
    try {
      await apiJSON('/documents/' + encodeURIComponent(d.id) + '/reprocess', { method: 'POST' });
      await rerender(); // строка и досье покажут «обрабатывается…»
    } catch (e) {
      alert('Перезагрузка не запустилась: ' + (e.detail || e.message));
    }
  });
  acts.append(rf);

  const del = dockBtn('trash', 'Удалить', _SVG_TRASH);
  del.addEventListener('click', async () => {
    if (!confirm('Удалить «' + (d.filename || d.id) + '» и всё извлечённое из него? Это необратимо.')) return;
    try {
      await apiJSON('/documents/' + encodeURIComponent(d.id), { method: 'DELETE' });
      docFragCache.delete(d.id);
      dockDocId = null;
      await rerender();
    } catch (e) {
      alert('Удаление не завершено: ' + (e.detail || e.message));
    }
  });
  acts.append(del);
  dk.append(acts);

  dock.append(dk);
}

export function badge(text, cls){ return el('span', 'badge ' + cls, text); }
