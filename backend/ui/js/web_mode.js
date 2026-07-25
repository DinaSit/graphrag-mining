import { respOf } from './article_stream.js';
import { TOC_SECTIONS, viewArticle } from './article_toc.js';
import { $, apiJSON, clear, el, extLink, trunc } from './dom.js';
import { hideFnpop } from './footnote_popup.js';
import { S } from './state.js';
import { SPY, applyTocActive, paintChips, paintInfobox, recalcSpy, scheduleSpy, snippetEl, tocNavigate, updateTocSpy } from './view_article.js';

// ---------- веб-режим: глобус и центральный контент из /web/answer ----------
export function globeBtn(st){
  const b = el('span', 'gicon' + (st.webMode ? ' on' : ''));
  b.id = 'gicon';
  b.setAttribute('role', 'button');
  b.setAttribute('aria-label', 'Ответ из веб-источников');
  b.setAttribute('aria-pressed', st.webMode ? 'true' : 'false');
  b.title = 'Ответ из веб-источников';
  b.tabIndex = 0;
  b.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c2.9 2.5 4.6 5.6 4.6 9s-1.7 6.5-4.6 9c-2.9-2.5-4.6-5.6-4.6-9s1.7-6.5 4.6-9z"/></svg>';
  const run = () => toggleWebMode(st);
  b.addEventListener('click', run);
  b.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' '){ e.preventDefault(); run(); } });
  return b;
}

export function toggleWebMode(st){
  st.webMode = !st.webMode;
  applyWebMode(st);
}

// показывает/прячет центральный контент по st.webMode; TOC приглушается целиком,
// инфобокс и чипы перерисовываются под режим. Вызывается и из перерисовок (viewArticle).
export function applyWebMode(st){
  const on = st.webMode;
  const btn = $('#gicon');
  if (btn){
    btn.classList.toggle('on', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  }
  for (const id of ['sec-ans', 'sec-rest', 'skels']){
    const n = document.getElementById(id);
    if (n) n.hidden = on;
  }
  const toc = $('#toc');
  if (toc) toc.classList.toggle('webdim', on);
  const web = $('#webmode');
  if (web){
    web.hidden = !on;
    if (on) paintWebMode(st);
  }
  // карточка «О статье» и чипы меняют содержимое по режиму: метрики базы ↔ веб-источники
  paintInfobox(st);
  paintChips(st);
  hideFnpop();
  const sc = document.getElementById('artscroll');
  if (on && sc) sc.scrollTop = 0; // веб-ответ читается с начала
  if (!on){ recalcSpy(); scheduleSpy(); } // возврат к базовому ответу — управление подсветкой возвращается скролл-spy
}

export function paintWebMode(st){
  const box = $('#webmode');
  if (!box) return;
  clear(box);
  const w = st.web || {};
  if (w.phase === 'loading'){ // ответ ещё не получен — статичный скелет загрузки
    for (const width of ['88%', '72%', '55%']){
      const s = el('div', 'skel');
      s.style.width = width;
      box.append(s);
    }
    return;
  }
  if (w.phase === 'error'){
    box.append(el('div', 'llmerr', 'Веб-ответ не получен: ' + (w.error || 'неизвестная ошибка')));
    return;
  }
  // порядок фиксирован: жёлтая строка ошибки → текст ответа →
  // раздел «Источники» (основной — первым с бейджем); при ошибке LLM
  // структура та же, но без текста
  const d = w.data || {};
  if (d.llm_error) box.append(el('div', 'llmerr', String(d.llm_error)));
  if (d.answer) box.append(linkifiedParagraphs(d.answer));
  else if (!d.llm_error) box.append(el('p', 'p', 'Ответ из веб-источников не найден.'));
  const list = webSourceList(st);
  if (list.length){
    box.append(el('div', 'sec-h', 'Источники'));
    const sb = el('div', 'websnips');
    for (const s of list) sb.append(snippetEl(s.data, s.primary));
    box.append(sb);
  }
}

// URL веб-сниппета по любому из принятых ключей контракта
export function snippetUrl(sn){
  return (sn && typeof sn === 'object') ? (sn.url || sn.href || sn.link || null) : null;
}

// Упорядоченный список веб-источников для карточек и инфобокса: основной
// (d.url) — первым с пометкой primary; если он не встретился среди сниппетов,
// добавляется синтетической карточкой, чтобы не потерять ссылку
export function webSourceList(st){
  const d = (st.web && st.web.data) || {};
  const list = (Array.isArray(d.snippets) ? d.snippets : []).slice(0, 8)
    .map(sn => ({ data: sn, url: snippetUrl(sn), primary: false }));
  if (d.url){
    const i = list.findIndex(s => s.url === d.url);
    if (i >= 0){
      const [p] = list.splice(i, 1);
      p.primary = true;
      list.unshift(p);
    } else {
      list.unshift({ data: { title: d.url, url: d.url }, url: d.url, primary: true });
    }
  }
  return list;
}

// Реестр веб-источников системы — ВЕСЬ список площадок веб-контура (домены
// ddgs-поиска + научные API), НЕ источники конкретного ответа. Значения по умолчанию
// предоставляет СЕРВЕР (GET /web/sources ← ml /web_sources, единственный источник правды);
// встроенный список ниже — только резервный вариант до/без ответа сервера. Правки
// (корзина/«Добавить») хранятся в localStorage поверх значений по умолчанию. Реестр передаётся
// на сервер с каждым /web/answer: домены ищутся ddgs и русским вопросом, и
// английским переводом; строки с полем api включают-выключают научные API
export const WEB_SOURCES_FALLBACK = [
  { title: 'ResearchGate', url: 'https://researchgate.net' },
  { title: 'eLibrary.ru', url: 'https://elibrary.ru' },
  { title: 'Springer Link', url: 'https://link.springer.com' },
  { title: 'Google Patents', url: 'https://patents.google.com' },
  { title: 'MDPI', url: 'https://mdpi.com' },
  { title: 'КиберЛенинка', url: 'https://cyberleninka.ru' },
  { title: 'Wiley Online Library', url: 'https://onlinelibrary.wiley.com' },
  { title: 'ScienceDirect', url: 'https://sciencedirect.com' },
  { title: 'arXiv', url: 'https://arxiv.org', api: 'arxiv' },
  { title: 'Crossref', url: 'https://search.crossref.org', api: 'crossref' },
  { title: 'Semantic Scholar', url: 'https://semanticscholar.org', api: 'semanticscholar' },
];
export let WEB_SOURCES_SERVER = null; // реестр с сервера; null — ещё не получен/недоступен
export const WEBSRC_LS = 'graphrag.webSources';
// Раскрыта ли форма «добавить источник». Флаг принадлежит этому модулю, читает
// и переключает его экран статьи — через функции: импортированную переменную
// присвоить нельзя
let webSrcFormOpen = false;
export function isWebSrcFormOpen(){ return webSrcFormOpen; }
export function setWebSrcFormOpen(open){ webSrcFormOpen = open; }

// Загружает реестр по умолчанию с сервера (один раз при загрузке приложения);
// при недоступном сервере без сообщения используется встроенный резервный список
export async function loadWebSources(){
  try {
    const data = await apiJSON('/web/sources');
    const list = Array.isArray(data.sources) ? data.sources : [];
    if (!list.length) return;
    WEB_SOURCES_SERVER = list.map(s => ({
      title: s.title || s.host,
      url: s.url || ('https://' + s.host),
      host: s.host || hostOf(s.url),
      api: s.api || null,
    }));
    // веб-карточка могла уже отрисоваться с резервным списком — обновляем
    if (S.article && S.article.webMode){ paintInfobox(S.article); paintChips(S.article); }
  } catch {}
}

// Правки реестра из localStorage: added — свои источники {title, url},
// removed — убранные корзиной URL
export function webSrcOverrides(){
  try {
    const o = JSON.parse(localStorage.getItem(WEBSRC_LS)) || {};
    return { removed: o.removed || [], added: o.added || [] };
  } catch { return { removed: [], added: [] }; }
}

export function webSrcSave(o){
  try { localStorage.setItem(WEBSRC_LS, JSON.stringify(o)); } catch {} // приватный режим — правки сохраняются до перезагрузки
}

// Единственная точка нормализации хоста: без www, пустая строка при некорректном URL
export function hostOf(url){
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return ''; }
}

// Итоговый реестр: значения по умолчанию (серверные либо резервные) + добавленные, минус
// удалённые; дедупликация по URL. host/api передаются для отправки на сервер и подсветки
export function webSourcesRoster(){
  const o = webSrcOverrides();
  const removed = new Set(o.removed);
  const out = [];
  const seen = new Set();
  for (const s of [...(WEB_SOURCES_SERVER || WEB_SOURCES_FALLBACK), ...o.added]){
    if (!s.url || seen.has(s.url) || removed.has(s.url)) continue;
    seen.add(s.url);
    out.push({ title: s.title || s.url, url: s.url,
               host: s.host || hostOf(s.url), api: s.api || null });
  }
  return out;
}

// Реестр для сервера: [{host}] всех строк карточки — передаётся с каждым
// вопросом в /web/answer, чтобы поиск шёл ровно по списку пользователя
export function webActiveSources(){
  const out = [];
  for (const s of webSourcesRoster()){
    if (s.host) out.push({ host: s.host });
  }
  return out;
}

// Что реально сработало в текущем веб-ответе: хосты сниппетов + ветки научных
// API (по метке source). null — ответа ещё нет (не приглушать реестр)
export function webUsedKeys(st){
  const w = st.web || {};
  if (w.phase !== 'done') return null;
  const d = w.data || {};
  const hosts = new Set();
  const apis = new Set();
  const push = u => { const h = hostOf(u); if (h) hosts.add(h); };
  for (const sn of (Array.isArray(d.snippets) ? d.snippets : [])){
    if (sn && sn.source && sn.source !== 'ddgs') apis.add(sn.source);
    const u = snippetUrl(sn);
    if (u) push(u);
  }
  if (d.url) push(d.url);
  return { hosts, apis };
}

// Использована ли площадка реестра в текущем ответе: совпадение ветки API
// (поле api строки — приходит с реестром от сервера) или хоста (с учётом
// поддоменов — сниппет www.researchgate.net ≙ researchgate.net)
export function webSrcUsed(entry, used){
  if (!used) return true; // данных об использовании нет — все строки обычные
  if (entry.api && used.apis.has(entry.api)) return true;
  const host = entry.host || hostOf(entry.url);
  if (!host) return false;
  for (const h of used.hosts){
    if (h === host || h.endsWith('.' + host) || host.endsWith('.' + h)) return true;
  }
  return false;
}

// текст веб-ответа: http(s)-ссылки кликабельны, всё остальное — textContent
export const URL_RE = /https?:\/\/[^\s<>"'«»()\[\]]+/g;
export function linkifiedParagraphs(text){
  const frag = document.createDocumentFragment();
  const parts = String(text || '').split(/\n{2,}/).filter(p => p.trim());
  for (const part of parts.length ? parts : ['']){
    const p = el('p', 'p');
    URL_RE.lastIndex = 0;
    let last = 0, m;
    while ((m = URL_RE.exec(part)) !== null){
      if (m.index > last) p.append(document.createTextNode(part.slice(last, m.index)));
      const u = m[0].replace(/[.,;:!?]+$/, ''); // хвостовая пунктуация — не часть ссылки
      p.append(extLink(u, trunc(u, 80)));
      last = m.index + u.length;
    }
    if (last < part.length) p.append(document.createTextNode(part.slice(last)));
    frag.append(p);
  }
  return frag;
}

// какие секции уже «есть» (для оглавления)
export function sectionCounts(st){
  const r = respOf(st);
  const done = st.phase === 'done';
  const partial = (r.evidence_status === 'partial');
  return {
    ans: 1,
    conf: (r.contradictions || []).length,
    gaps: done ? (r.gaps || []).length : 0,
    hyp: done ? (r.hypotheses || []).length : 0,
    facts: (r.experiments || []).length,
    rel: partial ? (r.related_experiments || []).length : 0,
    notes: st.notes.length,
  };
}

export function paintToc(st){
  const toc = $('#toc');
  if (!toc) return;
  clear(toc);
  toc.append(el('div', 't', 'Содержание'));
  const counts = sectionCounts(st);
  for (const [key, title] of TOC_SECTIONS){
    const n = counts[key];
    // полный список секций виден всегда (и после финала): пустые — приглушённые .off без перехода
    const b = el('button', (n || key === 'ans') ? '' : 'off');
    b.dataset.key = key;
    b.append(document.createTextNode(title + ' '));
    if (n && key !== 'ans') b.append(el('i', null, String(n)));
    if (n || key === 'ans') b.addEventListener('click', () => tocNavigate(key));
    toc.append(b);
  }
  // перерисовка (evidence/final) сбрасывает и DOM оглавления, и позиции секций:
  // пересчитываем кэш и возвращаем маркер — либо цель клика, либо по текущему скроллу
  recalcSpy();
  if (SPY.navKey !== null){
    SPY.activeKey = SPY.navKey;
    applyTocActive();
  } else {
    SPY.activeKey = null; // DOM новый — маркер надо проставить безусловно
    updateTocSpy();
  }
}
