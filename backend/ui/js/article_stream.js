import { ask } from './ask.js';
import { render, routeName } from './router.js';
import { S, loadDocs } from './state.js';
import { paintAnswer, paintChips, paintInfobox, recalcSpy, updateTocSpy } from './view_article.js';
import { paintToc } from './web_mode.js';

// ==================================================================
// Статья: состояние + SSE-стрим
// ==================================================================
export function newArticle(question){
  return {
    question,
    phase: 'streaming',   // streaming | done | offtopic | error | aborted
    ctrl: new AbortController(), // отмена fetch при новом вопросе
    evidence: null,       // payload события evidence
    final: null,          // QueryResponse из события final
    answerRaw: '',        // сырой текст дельт (с id-шными сносками)
    error: null,
    noteMap: new Map(),   // citeId -> note
    notes: [],            // [{id, ref, quote}] в порядке первого появления
    // независимый веб-контур (К1): запрос отправляется сразу вместе с /ask/stream,
    // результат ждёт в состоянии, пока пользователь не включит веб-режим глобусом
    web: { phase: 'loading', data: null, error: null }, // loading | done | error
    webMode: false,       // что показывает центральная колонка: база или веб-ответ
  };
}

// Штатная отмена незавершённого стрима: не ошибка, «final» поверх уже не придёт
export function abortStream(st){
  if (!st || st.phase !== 'streaming') return;
  st.phase = 'aborted';
  st.ctrl.abort();
}

export async function startStream(st){
  loadDocs(); // имена документов пригодятся сноскам
  try {
    const res = await fetch('/ask/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: st.question }),
      signal: st.ctrl.signal,
    });
    if (!res.ok) throw new Error('backend ответил HTTP ' + res.status);
    const reader = res.body.getReader();
    const dec = new TextDecoder('utf-8');
    let buf = '';
    try {
      for (;;){
        const { done, value } = await reader.read();
        if (done || st.phase === 'aborted') break;
        buf += dec.decode(value, { stream: true });
        // SSE-события разделены пустой строкой: обрабатываем только целые блоки,
        // незавершённый хвост копится в buf до следующего чанка
        let i;
        while ((i = buf.indexOf('\n\n')) !== -1){
          handleSSE(st, buf.slice(0, i));
          buf = buf.slice(i + 2);
        }
      }
    } finally {
      reader.cancel().catch(() => {});
    }
    if (st.phase === 'streaming'){ // поток оборвался без final
      st.phase = 'error';
      st.error = 'поток оборвался до финального события';
      syncArticle(st);
    }
  } catch (e) {
    if (st.phase === 'aborted' || (e && e.name === 'AbortError')) return; // штатная отмена
    if (st.phase === 'streaming' || st.phase === 'error'){
      st.phase = 'error';
      st.error = e && e.message ? e.message : 'сеть недоступна';
      syncArticle(st);
    }
  }
}

// Разбор одного SSE-блока (строки event:/data:). Несколько data:-строк склеиваются
// без \n: backend по контракту шлёт JSON одной строкой (main.py, _sse_event),
// спековая склейка через перевод строки здесь не нужна
export function handleSSE(st, block){
  if (st.phase === 'aborted') return; // после отмены события (включая final) не применяем
  let ev = 'message', data = '';
  for (const line of block.split('\n')){
    if (line.startsWith('event:')) ev = line.slice(6).trim();
    else if (line.startsWith('data:')) data += line.slice(5).trim();
  }
  if (!data) return;
  let payload;
  try { payload = JSON.parse(data); } catch (_) { return; }
  if (ev === 'evidence'){
    st.evidence = payload;
    syncArticle(st, 'evidence');
  } else if (ev === 'delta'){
    st.answerRaw += (payload.text || '');
    syncArticle(st, 'delta');
  } else if (ev === 'final'){
    st.final = payload;
    st.phase = payload.offtopic ? 'offtopic' : 'done';
    syncArticle(st, 'final');
  }
}

// Точечная синхронизация DOM, если открыт экран статьи с этим же состоянием
export function syncArticle(st, kind){
  if (routeName() !== 'article' || S.article !== st) return;
  if (st.phase === 'offtopic' || st.phase === 'error'){ render(); return; }
  if (kind === 'evidence'){ paintToc(st); paintInfobox(st); paintChips(st); }
  else if (kind === 'delta'){ scheduleAnswerPaint(st); }
  else render(); // 'final'; вызовы без kind до сюда не доходят (offtopic/error выше)
}

// Коалесинг дельт: полный перерендер ответа не чаще раза за кадр (rAF),
// иначе на каждой SSE-дельте весь summary повторно обрабатывается (сноски и DOM) — O(n²)
export let answerPaintRAF = 0;
export function scheduleAnswerPaint(st){
  if (answerPaintRAF) return;
  answerPaintRAF = requestAnimationFrame(() => {
    answerPaintRAF = 0;
    // к моменту кадра вопрос могли сменить или финализировать — тогда рисовать нечего
    if (routeName() === 'article' && S.article === st && st.phase === 'streaming'){
      paintAnswer(st);
      recalcSpy();    // контент увеличился — позиции секций и высота скролла изменились
      updateTocSpy(); // и активный пункт мог смениться без единого scroll-события
    }
  });
}

// ---------- активные данные ответа ----------
export function respOf(st){ return st.final || st.evidence || {}; }
// Граф прямого ответа — r.graph; у смежного ответа (sufficient=false, статус
// partial) backend отдаёт материал в related_graph — его показываем С ПОДПИСЬЮ
// «Граф смежных данных»: смежные данные не выдаются за прямые, но и не
// скрываются. displayGraph отдаёт {graph, related} — related управляет
// подписью мини-графа и заголовком оверлея.
export function displayGraph(st){
  const r = respOf(st);
  const main = r.graph;
  if (main && Array.isArray(main.nodes) && main.nodes.length){
    return { graph: main, related: false };
  }
  const rel = r.related_graph;
  if (rel && Array.isArray(rel.nodes) && rel.nodes.length && r.evidence_status === 'partial'){
    return { graph: rel, related: true };
  }
  return { graph: { nodes: [], edges: [] }, related: false };
}
export function allSources(st){
  const r = respOf(st);
  return [].concat(r.sources || [], r.related_sources || []);
}
// Число ДОКУМЕНТОВ, давших материал ответу — единое правило подсчёта для чипа
// и карточки «О статье». Считаются уникальные документы источников ответа и
// смежных данных; если источников ещё нет — документы поисковых фрагментов
export function usedDocsCount(st, r){
  const ids = new Set();
  for (const s of allSources(st)) if (s && s.document_id) ids.add(s.document_id);
  if (!ids.size){
    for (const h of r.search_hits || []) if (h.source && h.source.document_id) ids.add(h.source.document_id);
  }
  return ids.size;
}
