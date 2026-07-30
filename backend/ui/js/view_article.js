import { displayGraph, respOf, usedDocsCount } from './article_stream.js';
import { ask } from './ask.js';
import { appendRich, noteForSource, richParagraphs, splitPending, supFor } from './citations.js';
import { $, REDUCED, clear, el, extLink, pct, trunc, wrapQuote } from './dom.js';
import { graphSVG, openGraphOverlay } from './graph.js';
import { routeName } from './router.js';
import { POPULAR, S, docName, docYear, fragLoc, sciLabel } from './state.js';
import { _SVG_TRASH, dockBtn } from './view_docs.js';
import { openSources } from './view_sources.js';
import { foundExperiments, hostOf, isWebSrcFormOpen, paintToc, sectionCounts, sectionTitle, setWebSrcFormOpen, snippetUrl, webSourcesRoster, webSrcOverrides, webSrcSave, webSrcUsed, webUsedKeys } from './web_mode.js';

// ---------- скролл-spy оглавления ----------
// Активная секция = последний заголовок, чей верх поднялся выше «линии чтения»
// (верх скролл-области + SPY_LINE px); до первой секции активен «Ответ»; упор в самый
// низ — последняя секция (иначе короткие секции у дна недостижимы). Позиции секций
// кэшируются дешёвым offsetTop (offsetParent секций — сама <article>, ей выставлен
// position:relative) и пересчитываются на resize и после каждой перерисовки,
// а НЕ на каждый scroll-тик. Подсветка меняется только при реальной смене ключа.
export const SPY_LINE = 100;   // «линия чтения»: отступ от верха скролл-области
export const SPY_ANCHOR = 14;  // зазор цели клика = scroll-margin-top якорей
export const SPY = {
  keys: [], tops: [],   // кэш секций в координатах скролл-области
  narrow: false,        // режим на момент пересчёта (узкий = скроллит страница)
  activeKey: null,      // текущий подсвеченный пункт оглавления
  navKey: null,         // клик-навигация в процессе: spy не перебивает подсветку
  navTarget: 0,         // scrollTop — цель плавной прокрутки
  navTimer: 0,          // страховочный таймаут снятия флага навигации
  navDist: 0,           // остаток пути до цели (для продления страховки при прогрессе)
};
export const SPY_NAV_TIMEOUT = 700; // мс без прогресса к цели — снимаем флаг навигации

export function spyIsNarrow(){ return matchMedia('(max-width:980px)').matches; }
// скролл-контейнер статьи на широком экране — обёртка «текст + инфобокс»
export function artScroller(){ return document.getElementById('artscroll'); }

export function recalcSpy(){
  const art = document.getElementById('artbody');
  if (!art) return;
  SPY.narrow = spyIsNarrow();
  SPY.keys = []; SPY.tops = [];
  // на узком экране скроллит страница — переводим offsetTop в координаты документа;
  // на широком — в координаты контента скролл-обёртки (top статьи внутри неё)
  let base;
  if (SPY.narrow) base = art.getBoundingClientRect().top + window.scrollY;
  else {
    const sc = artScroller();
    base = sc ? art.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop : 0;
  }
  for (const s of art.querySelectorAll('#sec-rest > div[id^="sec-"]')){
    SPY.keys.push(s.id.slice(4));
    SPY.tops.push(base + s.offsetTop);
  }
}

export let spyRAF = 0;
export function scheduleSpy(){
  if (spyRAF) return;
  spyRAF = requestAnimationFrame(() => { spyRAF = 0; updateTocSpy(); });
}

// текущее положение скролл-области: [scrollTop, высота вьюпорта, высота контента, линия чтения]
export function spyScrollPos(art){
  if (SPY.narrow){
    const hdrH = ($('#hdr') || {}).offsetHeight || 0; // верх области — под липкой шапкой
    return [window.scrollY, window.innerHeight, document.documentElement.scrollHeight,
      window.scrollY + hdrH + SPY_LINE];
  }
  const sc = artScroller() || art;
  return [sc.scrollTop, sc.clientHeight, sc.scrollHeight, sc.scrollTop + SPY_LINE];
}

export function updateTocSpy(){
  if (routeName() !== 'article') return;
  if (S.article && S.article.webMode) return; // веб-режим: секции базы скрыты, TOC приглушён
  const art = document.getElementById('artbody');
  if (!art || !$('#toc')) return;
  if (SPY.narrow !== spyIsNarrow()) recalcSpy(); // сменился режим — кэш в других координатах
  const [pos, viewH, contentH, line] = spyScrollPos(art);
  if (SPY.navKey !== null){
    // идёт навигация по клику: подсветка уже установлена на целевой пункт, spy ожидает
    // завершения прокрутки; пока она приближается к цели — продлеваем страховочный таймаут
    const dist = Math.abs(pos - SPY.navTarget);
    if (dist <= 4) endTocNav();
    else if (dist < SPY.navDist){
      SPY.navDist = dist;
      clearTimeout(SPY.navTimer);
      SPY.navTimer = setTimeout(endTocNav, SPY_NAV_TIMEOUT);
    }
    return;
  }
  let active = 'ans';
  if (contentH > viewH && pos + viewH >= contentH - 4 && SPY.keys.length){
    active = SPY.keys[SPY.keys.length - 1]; // упор в самый низ — последняя секция
  } else {
    for (let i = 0; i < SPY.tops.length; i++) if (SPY.tops[i] <= line) active = SPY.keys[i];
  }
  setTocActive(active);
}

export function setTocActive(key){
  if (key === SPY.activeKey) return; // без лишних перерисовок: classList изменяется только при смене ключа
  SPY.activeKey = key;
  applyTocActive();
}
// безусловная простановка маркера (нужна после полной перестройки оглавления)
export function applyTocActive(){
  const toc = $('#toc');
  if (!toc) return;
  for (const b of toc.querySelectorAll('button'))
    b.classList.toggle('on', b.dataset.key === SPY.activeKey && !b.classList.contains('off'));
}

// Клик по пункту: подсветка ставится мгновенно и не перебивается spy'ем, пока идёт
// плавный скролл; флаг снимается по прибытии к цели или страховочным таймаутом ~700мс.
export function tocNavigate(key){
  const art = document.getElementById('artbody');
  if (!art) return;
  if (S.article && S.article.webMode) return; // в веб-режиме содержание некликабельно
  recalcSpy(); // позиции могли устареть (стрим дорисовывает контент)
  const [, viewH, contentH] = spyScrollPos(art);
  const hdrH = SPY.narrow ? (($('#hdr') || {}).offsetHeight || 0) : 0;
  const i = SPY.keys.indexOf(key);
  let top;
  if (i === -1) // «Ответ» (или секция ещё не отрисована) — к началу статьи
    top = SPY.narrow ? art.getBoundingClientRect().top + window.scrollY - hdrH - SPY_ANCHOR : 0;
  else
    top = SPY.tops[i] - hdrH - SPY_ANCHOR;
  top = Math.max(0, Math.min(Math.round(top), Math.max(0, contentH - viewH)));
  setTocActive(key);
  clearTimeout(SPY.navTimer);
  SPY.navKey = key;
  SPY.navTarget = top;
  SPY.navDist = Infinity;
  SPY.navTimer = setTimeout(endTocNav, SPY_NAV_TIMEOUT);
  const opts = { top, behavior: REDUCED ? 'auto' : 'smooth' };
  if (SPY.narrow) window.scrollTo(opts);
  else (artScroller() || art).scrollTo(opts);
}
export function endTocNav(){
  clearTimeout(SPY.navTimer);
  SPY.navKey = null;
  // подсветка остаётся на выбранном пункте; следующее событие прокрутки вернёт управление скролл-spy
}

// Скролл узкого экрана (page-скролл): один постоянный слушатель на window с guard'ом
// маршрута внутри updateTocSpy — снимать его не нужно, утечек нет. Скролл широкого
// экрана слушает обёртка #artscroll (artwrap) — слушатель удаляется вместе с ней.
window.addEventListener('scroll', scheduleSpy, { passive: true });
// resize меняет и позиции секций, и режим (узкий/широкий) — пересчёт кэша
window.addEventListener('resize', () => {
  if (routeName() !== 'article') return;
  recalcSpy();
  scheduleSpy();
});


/** Значение поля или прочерк: заглушки извлечения читателю не показываются. */
export function plainOr(value){
  const text = String(value ?? '').trim();
  return (!text || text === 'не указано' || text === 'Unknown Lab') ? '—' : text;
}

// Чипы метрик для узкого экрана (≤980px): те же значения, что в карточке
// «О статье», размещённые одной строкой под заголовком. Видимость управляется CSS —
// рисуем всегда, показывается только когда карточка и мета-строка скрыты
export function paintChips(st){
  const wrap = $('#art-chips');
  if (!wrap) return;
  clear(wrap);
  // веб-режим: метрики базового ответа здесь не соответствовали бы содержимому — вместо них один чип
  // с числом источников реестра (то же, что мини-таблица в карточке «О статье»)
  if (st.webMode){
    const c = el('span', 'chip');
    const n = String(webSourcesRoster().length);
    c.append('источников ');
    c.append(el('b', null, n));
    wrap.append(c);
    return;
  }
  const r = respOf(st);
  const has = !!(st.evidence || st.final);
  const chip = (label, value) => {
    const c = el('span', 'chip');
    c.append(label + ' ');
    c.append(el('b', null, value));
    wrap.append(c);
    return c;
  };
  const activate = (c, open, role) => {
    c.classList.add('act');
    c.setAttribute('role', role);
    c.tabIndex = 0;
    c.addEventListener('click', open);
    c.addEventListener('keydown', e => { if (e.key === 'Enter') open(); });
  };
  chip('уверенность', has && r.confidence != null ? pct(r.confidence) : '—');
  chip('научность', has && r.scientific_share != null ? pct(r.scientific_share) : '—');
  chip('документы', has ? String(usedDocsCount(st, r)) : '—');
  const g = displayGraph(st);
  const gChip = chip('граф',
    has ? (g.nodes || []).length + '/' + (g.edges || []).length : '—');
  if ((g.nodes || []).length){
    activate(gChip, () => openGraphOverlay(st), 'button');
    gChip.append(' →'); // стрелка — подсказка кликабельности
  }
}

// Строка реестра: кликабельное название + корзина. used=false — площадка не
// дала источников текущему ответу: строка приглушается, но остаётся интерактивной
export function webSrcRow(st, s, used){
  const tr = el('tr', used ? null : 'dim');
  const name = el('td', 'name');
  const a = extLink(s.url, trunc(s.title, 60));
  a.title = s.url;
  name.append(a);
  tr.append(name);

  const act = el('td', 'act');
  const del = dockBtn('trash', 'убрать из списка', _SVG_TRASH);
  del.addEventListener('click', () => {
    const o = webSrcOverrides();
    o.removed = [...o.removed.filter(u => u !== s.url), s.url];
    o.added = o.added.filter(x => x.url !== s.url); // пользовательский источник удаляется полностью
    webSrcSave(o);
    paintInfobox(st);
    paintChips(st);
  });
  act.append(del);
  tr.append(act);
  return tr;
}

// Форма «добавить источник»: ссылка + название, Enter добавляет. Источник
// попадает в реестр (localStorage), сохраняется между ответами и передаётся в поиск
export function webSrcForm(st){
  const form = el('div', 'srcform');
  const urlIn = el('input');
  urlIn.placeholder = 'ссылка (https://…)';
  const nameIn = el('input');
  nameIn.placeholder = 'название источника';
  const frow = el('div', 'frow');
  const err = el('div', 'ferr');
  const add = el('button', 'fadd', 'Добавить');
  const cancel = el('button', 'fcancel', 'отмена');
  const submit = () => {
    let u = urlIn.value.trim();
    if (u && !/^https?:\/\//i.test(u)) u = 'https://' + u; // домен без схемы дополняем схемой
    const host = hostOf(u);
    if (!host || !host.includes('.')){ err.textContent = 'нужна корректная ссылка'; return; }
    const o = webSrcOverrides();
    o.added = [...o.added.filter(x => x.url !== u),
               { title: nameIn.value.trim() || host, url: u }];
    o.removed = o.removed.filter(x => x !== u); // повторное добавление убранного возвращает строку
    webSrcSave(o);
    setWebSrcFormOpen(false);
    paintInfobox(st);
    paintChips(st);
  };
  add.addEventListener('click', submit);
  const onEnter = e => { if (e.key === 'Enter') submit(); };
  urlIn.addEventListener('keydown', onEnter);
  nameIn.addEventListener('keydown', onEnter);
  cancel.addEventListener('click', () => { setWebSrcFormOpen(false); paintInfobox(st); });
  frow.append(err, add, cancel);
  form.append(urlIn, nameIn, frow);
  return form;
}

export function paintInfobox(st){
  const box = $('#infobox');
  if (!box) return;
  clear(box);
  // заголовок зависит от режима: метрики статьи ↔ реестр веб-площадок
  const ih = el('div', 'ih', st.webMode ? 'Ресурсы' : 'О статье');
  box.append(ih);
  // веб-режим: вместо метрик базы — РЕЕСТР веб-источников системы (весь список
  // площадок поиска, не источники конкретного ответа): кликабельные названия
  // и корзина; использованные в текущем ответе площадки отображаются обычной яркостью,
  // прочие приглушены. Вместо заголовка колонки — строка «Добавить» со значком «+»,
  // она же открывает форму. Реестр не зависит от фазы веб-ответа
  if (st.webMode){
    const toggleForm = () => {
      setWebSrcFormOpen(!isWebSrcFormOpen());
      paintInfobox(st);
      const inp = box.querySelector('.srcform input');
      if (inp) inp.focus();
    };
    const t = el('table', 'dlist');
    const head = el('tr', 'addrow');
    const thAdd = el('th');
    const addBtn = el('button', 'addlbl', 'Добавить');
    addBtn.addEventListener('click', toggleForm);
    thAdd.append(addBtn);
    const thPlus = el('th', 'act');
    const plus = el('button', 'ic-btn');
    plus.title = 'добавить источник';
    plus.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 5v14M5 12h14"/></svg>';
    plus.addEventListener('click', toggleForm);
    thPlus.append(plus);
    head.append(thAdd, thPlus);
    t.append(head);
    if (isWebSrcFormOpen()){
      const ftr = el('tr');
      const ftd = el('td');
      ftd.colSpan = 2;
      ftd.style.padding = '0';
      ftd.append(webSrcForm(st));
      ftr.append(ftd);
      t.append(ftr);
    }
    const list = webSourcesRoster();
    if (!list.length){
      const etr = el('tr');
      const etd = el('td');
      etd.colSpan = 2;
      etd.append(el('span', 'k', 'источников нет — добавьте первый'));
      etr.append(etd);
      t.append(etr);
    }
    // подсветка: выделяются площадки, давшие источники текущему ответу,
    // остальные приглушены (но кликабельны и редактируемы); до ответа — все обычные
    const used = webUsedKeys(st);
    for (const s of list) t.append(webSrcRow(st, s, webSrcUsed(s, used)));
    box.append(t);
    return;
  }
  const r = respOf(st);
  // состав плашки фиксирован с первого мгновения: до события evidence все значения —
  // приглушённые «—», дальше только подставляем данные, не перестраивая структуру
  const has = !!(st.evidence || st.final);
  const addRow = (k, v, vcls) => {
    const row = el('div', 'row');
    row.append(el('span', 'k', k), el('span', 'v' + (vcls ? ' ' + vcls : ''), v));
    box.append(row);
    return row;
  };
  // все строки видимы всегда: нераспознанный показатель (null) — приглушённое «—»,
  // фактический ноль остаётся нулём (0 %, 0, 0 / 0).
  // Инфобокс показывает ПОЛНУЮ информацию без фильтров и заполняется уже во время
  // генерации: значения появляются с события evidence. Финал может лишь УВЕЛИЧИТЬ
  // «Источники»/«Научность» (добавляет процитированное сверх топ-12, чтобы сноски
  // резолвились) — значения только увеличиваются, поэтому активное заполнение оставляем.
  const confKnown = has && r.confidence != null;
  addRow('Уверенность', confKnown ? pct(r.confidence) : '—', confKnown ? 'conf' : 'na');
  const sciKnown = has && r.scientific_share != null;
  addRow('Научность', sciKnown ? pct(r.scientific_share) : '—', sciKnown ? 'sci' : 'na');
  // «Документы» — сколько РАЗНЫХ документов дали материал для ответа; в отличие от
  // числа сносок внизу статьи это счёт по документам, а не по фрагментам
  const docCount = usedDocsCount(st, r);
  addRow('Документы', has ? String(docCount) : '—', has ? null : 'na');
  const g = displayGraph(st);
  addRow('Узлы / связи',
    has ? ((g.nodes || []).length + ' / ' + (g.edges || []).length) : '—',
    has ? null : 'na');

  // зона мини-графа присутствует всегда; до данных — статичный «—» без анимаций
  const mg = el('div', 'minigraph');
  const cap = el('div', 'cap');
  cap.append(el('span', null, 'Граф'));
  if ((g.nodes || []).length){
    const expand = el('button', null, 'развернуть →');
    expand.addEventListener('click', () => openGraphOverlay(st));
    cap.append(expand);
  }
  mg.append(cap);
  if ((g.nodes || []).length){
    const svg = graphSVG(g, 10, 220, true);
    svg.addEventListener('click', () => openGraphOverlay(st));
    mg.append(svg);
  } else {
    const ph = el('div', null, '—');
    ph.style.cssText = 'height:72px;display:flex;align-items:center;justify-content:center;color:var(--w-dim)';
    mg.append(ph);
  }
  box.append(mg);
}

export function paintAnswer(st){
  const body = $('#ans-body');
  if (!body) return;
  clear(body);
  if (st.phase === 'done'){
    const r = st.final || {};
    if (r.llm_error) body.append(el('div', 'llmerr', r.llm_error));
    body.append(richParagraphs(st, r.summary || ''));
    return;
  }
  const [shown] = splitPending(st.answerRaw);
  const frag = richParagraphs(st, shown);
  const lastP = frag.lastChild;
  body.append(frag);
  (lastP || body).append(el('span', 'cursor'));
}

// Секции ниже ответа. Рисуются с события evidence, а не с финала: факты,
// источники и граф приходят в предпросмотре за доли секунды, тогда как текст
// модель пишет десятки секунд. Состав секций берётся из sectionCounts — того
// же счётчика, по которому строится оглавление, иначе содержание обещает
// секции, которых в документе нет, и переход по ним уводит в начало статьи
export function paintSections(st){
  const r = respOf(st);
  const counts = sectionCounts(st);
  // сноски строим заново с чистой нумерацией: перерисовка — источник истины
  st.noteMap = new Map();
  st.notes = [];
  paintAnswer(st);

  const rest = $('#sec-rest');
  if (!rest) return;
  // полосы ненаписанного текста снимаются только по готовности ответа —
  // во время генерации они и есть признак того, что текст ещё пишется
  const skeleton = $('#skels');
  if (skeleton && st.phase === 'done') skeleton.remove();
  clear(rest);
  // заголовок берётся из общего списка секций — тот же, что в оглавлении
  const sec = (key) => {
    const w = el('div'); w.id = 'sec-' + key;
    w.append(el('div', 'sec-h', sectionTitle(key)));
    rest.append(w);
    return w;
  };

  if (counts.conf){
    const w = sec('conf');
    for (const c of r.contradictions){
      const d = el('div', 'conflict');
      d.append(el('span', 'flag', '⚑'));
      appendRich(st, d, String(c));
      w.append(d);
    }
  }
  if (counts.gaps){
    const w = sec('gaps');
    for (const gp of r.gaps){
      const d = el('div', 'gap');
      appendRich(st, d, String(gp));
      w.append(d);
    }
  }
  if (counts.hyp){
    const w = sec('hyp');
    for (const h of r.hypotheses){
      const p = el('p', 'hyp');
      p.append(el('span', 'mark', 'Косвенно'));
      appendRich(st, p, String(h));
      w.append(p);
    }
  }
  if (counts.facts){
    const w = sec('facts');
    w.append(expTable(st, foundExperiments(r)));
  }
  // секции «Внешние источники» в базовом ответе нет: веб-ответ доступен
  // через переключатель-глобус (final.web_answer в контракте отсутствует)
  if (st.notes.length){
    const w = sec('notes');
    const ol = el('ol', 'refs');
    for (const note of st.notes) ol.append(noteLi(st, note));
    w.append(ol);
  }
  paintToc(st);
  paintInfobox(st);
  paintChips(st);
}

export function snippetEl(sn, primary){
  const d = el('div', 'sn');
  if (typeof sn === 'string'){ d.textContent = sn; return d; }
  const url = snippetUrl(sn);
  const title = sn.title || url || 'источник';
  const body = sn.snippet || sn.body || sn.text || '';
  // бейдж основного источника открывает строку — читается раньше названия
  if (primary) d.append(el('span', 'primetag', 'основной'));
  if (url){
    d.append(extLink(url, trunc(title, 90)));
  } else d.append(el('b', null, trunc(title, 90)));
  // год публикации из научных API (контракт «год статьи»): year или date рядом с заголовком
  const yr = sn.year != null ? sn.year : sn.date;
  if (yr != null && String(yr) !== '') d.append(el('span', 'yr', ' · ' + yr));
  if (body){ d.append(document.createElement('br')); d.append(document.createTextNode(trunc(body, 220))); }
  // Маршрут к полному тексту для источников с DOI: официальная страница —
  // ссылка заголовка (шаг 1, там нередко open access); платный доступ → ResearchGate
  // (авторские копии, шаг 2); Sci-Hub — запасной, в основном для статей
  // до ~2021 (коллекция заморожена). Переход — действие пользователя
  const doi = url && (url.match(/10\.\d{4,9}\/[^\s?#]+/) || [])[0];
  if (doi){
    const ft = el('div', 'ft');
    ft.append('полный текст: ');
    const rg = extLink('https://www.researchgate.net/search?q=' + encodeURIComponent(sn.title || doi), 'ResearchGate');
    rg.title = 'поиск авторской копии по названию';
    const sh = extLink('https://sci-hub.ru/' + doi, 'Sci-Hub');
    sh.title = 'запасной вариант: надёжен в основном для статей до ~2021';
    ft.append(rg, ' · ', sh);
    d.append(ft);
  }
  return d;
}

export function noteLi(st, note){
  const li = el('li');
  if (note.ref && note.ref.document_id){
    const doc = el('span', 'doc', docName(note.ref.document_id));
    doc.tabIndex = 0;
    doc.setAttribute('role', 'link');
    const open = () => openSources(note.id);
    doc.addEventListener('click', open);
    doc.addEventListener('keydown', e => { if (e.key === 'Enter') open(); });
    li.append(doc);
    const yr = docYear(note.ref.document_id); // год издания рядом с именем файла (контракт «год статьи»)
    if (yr != null) li.append(' (' + yr + ')');
    li.append(', ' + fragLoc(note.ref.document_id, note.ref.page));
    const sci = sciLabel(note.ref.document_id);
    if (sci) li.append(' · ' + sci);
  } else if (/^https?:\/\//i.test(note.id || '')){
    // страховка: цитата не резолвится, но это URL — рендерим кликабельную ссылку
    li.append(extLink(note.id, trunc(note.id, 90)));
  } else {
    li.append('фрагмент вне списка источников ответа');
  }
  if (note.quote){
    li.append(' — ');
    li.append(el('q', null, wrapQuote(note.quote, 220)));
  }
  return li;
}

export function expTable(st, rows){
  const wrap = el('div', 'tblwrap');
  const t = el('table', 'exp');
  const head = el('tr');
  // «Значение» — то, ради чего факт извлекался: без него измерения одного и
  // того же свойства выглядят одинаковыми строками. «Образец» их различает
  // (год, номер опыта). Колонка «Лаб.» убрана: заполнена у 3 % фактов, а место
  // занимала наравне с остальными; лабораторию видно в форме проверки
  for (const h of ['Материал', 'Процесс', 'Свойство', 'Значение', 'Образец', 'T, °C', 'Эффект', 'Ист.'])
    head.append(el('th', null, h));
  t.append(head);
  for (const row of rows){
    const tr = el('tr');
    tr.append(el('td', null, row.material || '—'));
    tr.append(el('td', null, row.process || '—'));
    tr.append(el('td', null, row.property || '—'));
    tr.append(el('td', 'num', row.result_value != null
      ? `${row.result_value}${row.result_unit ? ' ' + row.result_unit : ''}` : '—'));
    tr.append(el('td', null, plainOr(row.sample)));
    tr.append(el('td', 'num', row.temperature_c != null ? String(row.temperature_c) : '—'));
    const eff = String(row.effect || '—');
    const low = eff.toLowerCase();
    const up = low.startsWith('рост') || low.startsWith('increase') || low.startsWith('увелич');
    const down = low.startsWith('сниж') || low.startsWith('decrease') || low.startsWith('умень') || low.startsWith('паден');
    const td = el('td', up ? 'up' : (down ? 'down' : null));
    td.textContent = (up ? '▲ ' : down ? '▼ ' : '') + eff;
    tr.append(td);
    const tdRef = el('td');
    const note = noteForSource(st, row.source);
    if (note) tdRef.append(supFor(st, note));
    tr.append(tdRef);
    t.append(tr);
  }
  wrap.append(t);
  return wrap;
}

// ---------- вопрос вне предметной области и ошибка — единое оформление ----------
export function paintOfftopic(view){
  const o = el('div', 'oops');
  o.append(el('div', 'glyph', '∅'));
  o.append(el('h3', null, 'Такой статьи в энциклопедии нет'));
  o.append(el('p', null,
    'Вопрос не похож на запрос к базе знаний R&D, поэтому полный поиск не запускался. ' +
    'Здесь отвечают про материалы, процессы, параметры, эксперименты и источники.'));
  const tries = el('div', 'try');
  for (const q of POPULAR.slice(0, 3)){
    const b = el('button', null, q + ' →');
    b.addEventListener('click', () => ask(q));
    tries.append(b);
  }
  o.append(tries);
  const center = el('div', 'oops-center'); // вертикальное центрирование в видимой области
  center.append(o);
  view.append(center);
}

export function paintAskError(view, st){
  const o = el('div', 'oops');
  o.append(el('div', 'glyph', '⚠'));
  o.append(el('h3', null, 'Ответ не собрался'));
  o.append(el('p', null, 'Причина: ' + (st.error || 'неизвестная ошибка') + '. База знаний на месте — можно попробовать ещё раз.'));
  const retry = el('button', 'retry', 'Повторить запрос');
  retry.addEventListener('click', () => ask(st.question));
  o.append(retry);
  view.append(o);
}
