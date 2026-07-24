import { respOf } from './article_stream.js';
import { resolveQuote } from './citations.js';
import { $, REDUCED, apiJSON, clear, el } from './dom.js';
import { gotoRoute, render } from './router.js';
import { PREVIEW_TYPES, S, docFragCache, docInfo, docName, docYear, fragLoc, loadDocs, sciLabel } from './state.js';

// ==================================================================
// Экран источников
// ==================================================================
export function buildSrcEntries(){
  const st = S.article;
  if (!st) return [];
  const entries = [];
  const seen = new Set();
  st.notes.forEach((note, i) => {
    if (!note.ref || !note.ref.document_id) return;
    const key = note.ref.document_id + '|' + (note.ref.fragment_id || '');
    if (seen.has(key)) return;
    seen.add(key);
    entries.push({ n: i + 1, ref: note.ref,
                   quote: note.quote || resolveQuote(st, '', note.ref) });
  });
  const r = respOf(st);
  for (const pool of [r.sources || [], r.related_sources || [], (r.search_hits || []).map(h => h.source)]){
    for (const s of pool){
      if (!s || !s.document_id) continue;
      const key = s.document_id + '|' + (s.fragment_id || '');
      if (seen.has(key)) continue;
      seen.add(key);
      entries.push({ n: null, ref: s, quote: s.quote || resolveQuote(st, '', s) });
    }
  }
  return entries;
}

// открыть источники текущего ответа; citeId — выделить конкретную сноску
export function openSources(citeId){
  const entries = buildSrcEntries();
  let sel = 0;
  if (citeId && S.article){
    const note = S.article.noteMap.get(citeId);
    if (note && note.ref){
      const i = entries.findIndex(e => e.ref === note.ref ||
        (e.ref.document_id === note.ref.document_id && e.ref.fragment_id === note.ref.fragment_id));
      if (i >= 0) sel = i;
    }
  }
  S.srcView = { entries, sel, docMode: false };
  gotoRoute('sources');
}
// открыть один документ (из базы знаний), без подсветки
export function openDocView(docId){
  S.srcView = {
    entries: [{ n: null, ref: { document_id: docId, fragment_id: null } }],
    sel: 0, docMode: true,
  };
  gotoRoute('sources');
}

export function viewSources(view){
  const sv = S.srcView;
  if (!sv || !sv.entries.length){ location.hash = '#/'; return; }
  const grid = el('div', 'viewer');
  const list = el('div', 'vlist');
  list.append(el('div', 't', sv.docMode ? 'Просмотр документа' : ('Источники · ' + sv.entries.length)));
  sv.entries.forEach((entry, i) => {
    const b = el('button', 'vsrc' + (i === sv.sel ? ' on' : ''));
    b.append(el('span', 'n', entry.n != null ? String(entry.n) : '·'));
    const body = el('div');
    const docId = entry.ref.document_id;
    // мета файла: имя · год · происхождение · тип/научность (для подсказки и резервного отображения)
    const info = docInfo(docId);
    const bits = [docName(docId)];
    const yr = docYear(docId);
    if (yr != null) bits.push(String(yr));
    if (info && info.origin === 'ru') bits.push('РФ');
    else if (info && info.origin === 'foreign') bits.push('зарубежн.');
    const sci = sciLabel(docId);
    if (sci) bits.push(sci);
    const quote = entry.quote ? String(entry.quote).replace(/\s+/g, ' ').trim() : '';
    if (quote){
      // основной текст — цитата (до 7 строк), под ней серым имя файла в одну строку
      body.append(el('div', 'q', quote));
      body.append(el('div', 'f', docName(docId)));
    } else {
      // цитаты нет (табличный источник и т.п.) — прежний вид: имя + мета
      body.append(el('span', 'nm', docName(docId)));
      body.append(el('span', 'loc', bits.slice(1).join(' · ')));
    }
    // подсказка при наведении: полная цитата + файл с годом/происхождением/научностью
    b.title = (quote ? quote + '\n\n' : '') + bits.join(' · ');
    b.append(body);
    b.addEventListener('click', () => { sv.sel = i; render(); });
    list.append(b);
  });
  const doc = el('div', 'vdoc');
  doc.id = 'vdoc';
  grid.append(list, doc);
  view.append(grid);
  // выбранный источник всегда в поле зрения: имени файла над превью нет,
  // подсвеченная строка слева — единственный указатель, что открыто
  const on = list.querySelector('.vsrc.on');
  if (on) on.scrollIntoView({ block: 'nearest' });
  paintSourceDoc(sv.entries[sv.sel].ref);
}

// Встроенный просмотрщик pdf.js (/ui/pdfjs): в отличие от нативного, поддерживает
// открытие на заданной странице И подсветку искомого текста прямо в документе.
// Начальный hash (#page/#search) вьюер стабильно теряет на гонке инициализации,
// поэтому страница и подсветка задаются явно из родителя после загрузки
export function pdfViewerUrl(fileUrl){
  return '/ui/pdfjs/web/viewer.html?file=' + encodeURIComponent(fileUrl);
}

// Фраза для поиска в PDF из текста цитаты: самый длинный ряд подряд идущих
// «словесных» слов (только буквы, ≥3 символов) — номера таблиц, цифры и тире
// текстовый слой PDF сопоставляет ненадёжно, слова из букв находятся стабильно
export function pdfSearchPhrase(text){
  const words = String(text || '').replace(/\s+/g, ' ').trim().split(' ');
  let best = [];
  let cur = [];
  for (const w of words){
    if (/^[а-яёa-z]{3,}$/i.test(w)){
      cur.push(w);
      if (cur.length >= 5){ best = cur; break; }
    } else {
      if (cur.length > best.length) best = cur;
      cur = [];
    }
  }
  if (cur.length > best.length) best = cur;
  return best.slice(0, 5).join(' ');
}

// Кандидаты подсветки для цитаты: сперва цитата целиком, затем первое
// предложение, затем словесная фраза — текстовый слой PDF не всегда сопоставляет
// длинный фрагмент (переносы, дефисы), резервные кандидаты гарантируют хотя бы частичную подсветку
export function citationQueries(text){
  const norm = String(text || '').replace(/\s+/g, ' ').trim();
  const out = [];
  if (norm.length >= 20) out.push(norm.slice(0, 400));
  const sentence = norm.match(/^.{30,300}?[.!?]/);
  if (sentence) out.push(sentence[0]);
  const run = pdfSearchPhrase(norm);
  if (run) out.push(run);
  return out;
}

// Управление уже открытым вьюером: прячет боковую панель, растягивает документ
// на ширину окна, переходит на страницу и подсвечивает первый найденный
// кандидат из queries. Сбой подавляется: документ остаётся открытым с начала
export async function pdfViewerDrive(iframe, page, queries){
  try {
    const win = iframe.contentWindow;
    const app = win && win.PDFViewerApplication;
    if (!app) return;
    await app.initializedPromise;
    // initializedPromise ≠ «документ загружен»: pdfDocument появляется позже,
    // ждём его опросом до ~10 с (100 × 100 мс), после чего опрос прекращается без сообщения
    for (let i = 0; i < 100 && !app.pdfDocument; i++) await new Promise(r => setTimeout(r, 100));
    if (!app.pdfDocument || !document.body.contains(iframe)) return;
    // боковая панель: закрыть сейчас, запретить автооткрытие настройкой
    // (sidebarViewOnLoad=0) и выполнить отложенное повторное закрытие — оглавление
    // загружается позже и может открыть панель повторно
    try { if (app.preferences) app.preferences.set('sidebarViewOnLoad', 0); } catch (_) {}
    try { if (app.pdfSidebar) app.pdfSidebar.close(); } catch (_) {}
    setTimeout(() => { try { app.pdfSidebar && app.pdfSidebar.close(); } catch (_) {} }, 1500);
    try { app.pdfViewer.currentScaleValue = 'page-width'; } catch (_) {}
    if (page != null) app.page = page;
    const total = () => (app.findController.pageMatches || [])
      .reduce((s, m) => s + (m ? m.length : 0), 0);
    for (const q of (queries || [])){
      const query = String(q || '').replace(/\s+/g, ' ').trim();
      if (query.length < 3) continue;
      // поиск pdf.js: подсвечивает все совпадения и переходит к ближайшему
      // от текущей страницы (page выше — страховка, если текст не будет найден)
      app.eventBus.dispatch('find', { source: null, type: '', query,
        caseSensitive: false, entireWord: false, highlightAll: true, findPrevious: false });
      // ждём результат поиска; есть совпадения — кандидат сработал
      for (const delay of [600, 900]){
        await new Promise(r => setTimeout(r, delay));
        if (total() > 0) return;
      }
    }
  } catch (_) {}
}

export function pdfViewerHook(iframe, page, queries){
  iframe.addEventListener('load', () => pdfViewerDrive(iframe, page, queries), { once: true });
}

// Единый выбор URL превью документа (досье и полностраничный просмотр):
// исходный PDF — оригинал, DOCX/PPTX с готовым превью — конвертированный PDF
// из MinIO, иначе null (резервный текстовый режим / заглушка)
export function docPreviewSrc(d){
  const t = String((d && d.document_type) || '').toLowerCase();
  if (t === 'pdf') return '/documents/' + encodeURIComponent(d.id) + '/original';
  if (PREVIEW_TYPES.includes(t) && d.preview_object)
    return '/documents/' + encodeURIComponent(d.id) + '/preview';
  return null;
}

// Фрагменты документа с кэшем (полный ответ /documents/{id} уже кэшируется)
export async function docFragments(documentId){
  let data = docFragCache.get(documentId);
  if (!data){
    data = await apiJSON('/documents/' + encodeURIComponent(documentId));
    docFragCache.set(documentId, data);
  }
  return data.fragments || data.source_fragments || [];
}

export async function paintSourceDoc(ref){
  const box = $('#vdoc');
  if (!box) return;
  clear(box);
  await loadDocs();
  const info = docInfo(ref.document_id);
  // шапки над превью нет: имя файла показывает выбранная строка слева,
  // скачивание и поиск — в тулбаре pdf.js; превью занимает всю колонку

  let fragments = null; // лениво: превью-режиму нужны только для цитаты
  const ensureFragments = async () => {
    if (fragments === null) fragments = await docFragments(ref.document_id);
    return fragments;
  };

  // URL превью (единые правила с досье — docPreviewSrc); нет превью —
  // текстовые фрагменты ниже
  const pvUrl = info ? docPreviewSrc(info) : null;

  if (pvUrl){
    // цитата/факт: pdf.js открывает документ на нужной странице и подсвечивает
    // цитату ПРЯМО в документе (по текстовому слою, кандидаты от целой цитаты
    // к словесной фразе); отдельного блока-напоминания над превью нет
    const iframe = document.createElement('iframe');
    iframe.title = info ? info.filename : '';
    let page = ref.page != null ? ref.page : null;
    let hitText = null;
    if (ref.fragment_id){
      try {
        const frs = await ensureFragments();
        const hit = frs.find(f => f.id === ref.fragment_id);
        if (hit){
          if (hit.page != null) page = hit.page;
          hitText = hit.text || null;
        }
      } catch (_) {} // фрагменты не загрузились — документ отображается без подсветки
      if (!document.body.contains(box)) return;
    }
    iframe.src = pdfViewerUrl(pvUrl);
    pdfViewerHook(iframe, page, hitText ? citationQueries(hitText) : null);
    box.append(iframe);
    return;
  }

  // текстовые фрагменты по порядку
  let frs;
  try {
    frs = await ensureFragments();
  } catch (e) {
    box.append(el('div', 'vempty', 'Не удалось загрузить документ: ' + (e.message || 'ошибка сети') + '. Оригинал доступен из досье в разделе «Документы».'));
    return;
  }
  if (!document.body.contains(box)) return;
  const sorted = frs.slice().sort((a, b) => (a.page || 0) - (b.page || 0));
  if (!sorted.length){
    box.append(el('div', 'vempty', 'Фрагменты документа недоступны — скачайте оригинал.'));
    return;
  }
  let target = null;
  for (const f of sorted){
    const isHit = ref.fragment_id && f.id === ref.fragment_id;
    const d = el('div', 'frag' + (isHit ? ' hit' : ''));
    d.append(el('span', 'fn', fragLoc(ref.document_id, f.page) + (isHit ? ' · цитируется в ответе' : '')));
    d.append(document.createTextNode(f.text || ''));
    box.append(d);
    if (isHit) target = d;
  }
  if (target) target.scrollIntoView({ behavior: REDUCED ? 'auto' : 'smooth', block: 'center' });
}
