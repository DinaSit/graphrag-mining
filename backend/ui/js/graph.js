import { displayGraph } from './article_stream.js';
import { $, el, trunc } from './dom.js';

// ==================================================================
// Граф по контракту КГ: продольная раскладка слева-направо
// [Material/Process/Equipment/Condition] → [Claim] → [Document]
// ==================================================================
export function nodeColor(type){
  const t = String(type || '').toLowerCase();
  if (t.includes('material'))  return 'var(--w-link)';       // голубой — исходные материалы
  if (t.includes('process') || t.includes('equipment') || t.includes('condition'))
    return '#4a648c';                                        // тёмно-синий приглушённый — процесс/оборудование/условие
  if (t.includes('claim'))     return '#5b6672';             // серый — утверждение
  if (t.includes('document'))  return '#20272f';             // тёмный — документ-источник
  return '#5b6672';                                          // неизвестный тип трактуем как Claim (серый)
}
// колонка по типу узла (контракт КГ):
//   0 — вход: Material/Process/Equipment/Condition
//   2 — Document (правый край)
//   1 — Claim и любой неизвестный тип (в т.ч. старые Property/Effect payload'ы)
export const GRAPH_COL_LEFT  = ['material', 'process', 'equipment', 'condition'];
export function graphColumn(type){
  const t = String(type || '').toLowerCase();
  if (GRAPH_COL_LEFT.some(k => t.includes(k))) return 0;
  if (t.includes('document')) return 2;
  return 1; // Claim и неизвестные типы — в центральную колонку, рендеринг не прерывается
}
// mini=true — компакт для инфобокса (подпись под узлом, масштаб под ширину колонки);
// mini=false — оверлей (подпись справа, пиксельный размер: высокий граф скроллится в панели)
export function graphSVG(graph, maxN, w, mini){
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', 'граф окрестности ответа');
  const nodes = (graph.nodes || []).slice(0, maxN);
  if (!nodes.length){ svg.setAttribute('viewBox', '0 0 ' + w + ' 40'); return svg; }

  // раскладка по колонкам типа; пустые колонки схлопываются
  const byCol = [[], [], []];
  for (const n of nodes) byCol[graphColumn(n.type)].push(n);
  const cols = byCol.filter(c => c.length);

  const rowH = mini ? 30 : 46;
  const padY = mini ? 14 : 28;
  const maxRows = Math.max.apply(null, cols.map(c => c.length));
  const h = padY * 2 + maxRows * rowH;
  svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
  if (mini) svg.setAttribute('width', '100%');
  else { svg.setAttribute('width', String(w)); svg.setAttribute('height', String(h)); }

  const padX = mini ? 8 : 30;
  const band = (w - padX * 2) / cols.length;
  const pos = new Map(); // node.id -> [x, y]
  cols.forEach((col, ci) => {
    // в мини узел по центру полосы (подпись под ним), в оверлее — у левого края (подпись справа)
    const x = padX + band * ci + (mini ? band / 2 : 14);
    col.forEach((n, ri) => {
      pos.set(n.id, [x, h / 2 + (ri - (col.length - 1) / 2) * rowH]);
    });
  });

  const r = mini ? 4 : 6;
  for (const e of (graph.edges || [])){
    if (!pos.has(e.source) || !pos.has(e.target)) continue;
    let [x1, y1] = pos.get(e.source), [x2, y2] = pos.get(e.target);
    if (x2 < x1){ const t = [x1, y1]; [x1, y1] = [x2, y2]; [x2, y2] = t; } // рисуем всегда слева-направо
    const p = document.createElementNS(ns, 'path');
    const dx = Math.max((x2 - x1) / 2, mini ? 10 : 26); // внутри одной колонки — небольшая дуга
    p.setAttribute('d', 'M' + x1.toFixed(1) + ' ' + y1.toFixed(1)
      + ' C' + (x1 + dx).toFixed(1) + ' ' + y1.toFixed(1)
      + ' ' + (x2 - dx).toFixed(1) + ' ' + y2.toFixed(1)
      + ' ' + x2.toFixed(1) + ' ' + y2.toFixed(1));
    p.setAttribute('fill', 'none');
    p.style.stroke = 'var(--w-line2)';
    // ховер-подсветка (только в оверлее): по data-source/target находим инцидентные рёбра
    p.setAttribute('data-source', e.source);
    p.setAttribute('data-target', e.target);
    svg.append(p);
  }

  for (const col of cols){
    for (const n of col){
      const [x, y] = pos.get(n.id);
      const g = document.createElementNS(ns, 'g');
      g.setAttribute('data-id', n.id); // ключ для ховер-подсветки инцидентных рёбер и соседей
      const title = document.createElementNS(ns, 'title');
      title.textContent = (n.label || n.id) + (n.type ? ' · ' + n.type : '');
      g.append(title);
      const c = document.createElementNS(ns, 'circle');
      c.setAttribute('cx', x.toFixed(1)); c.setAttribute('cy', y.toFixed(1));
      c.setAttribute('r', String(String(n.type || '').toLowerCase().includes('material') ? r + 1 : r));
      c.style.fill = nodeColor(n.type);
      if (!mini) c.setAttribute('stroke-width', '2'); // видимая обводка при ховер-подсветке
      g.append(c);
      const t = document.createElementNS(ns, 'text');
      if (mini){
        t.setAttribute('x', x.toFixed(1));
        t.setAttribute('y', (y + r + 8.5).toFixed(1));
        t.setAttribute('text-anchor', 'middle');
        t.setAttribute('font-size', '6.5');
        t.textContent = trunc(n.label || n.id, 12);
      } else {
        t.setAttribute('x', (x + r + 7).toFixed(1));
        t.setAttribute('y', (y + 3.5).toFixed(1));
        t.setAttribute('font-size', '11');
        // в оверлее полоса колонки ~290px — подписи длиннее, чтобы не резать «Конвертерный шл…»
        t.textContent = trunc(n.label || n.id, 34);
      }
      t.style.fill = 'var(--w-dim)';
      g.append(t);
      svg.append(g);
    }
  }
  if (!mini) attachGraphHover(svg, graph.edges || []);
  return svg;
}

// Ховер/клик в развёрнутом графе. При наведении на вершину голубым (var(--w-link))
// подсвечивается её ПУТЬ по НАПРАВЛЕННЫМ рёбрам: объединение всех потомков (BFS вперёд
// по source→target) и всех предков (BFS назад по target→source) плюс ТОЛЬКО рёбра,
// пройденные этими обходами. Именно поэтому через общий узел (например, Document двух
// материалов) подсветка не распространяется на чужие пути: у Material подсвечиваются его Claims
// и их Documents, у Claim — входы слева и документ справа, у Document — все его Claims
// со входами. Остальной граф НЕ затемняется. Клик по вершине фиксирует подсветку
// её пути: она остаётся после увода курсора; ховер по другим вершинам поверх фиксации
// показывает временную подсветку, но при уходе возвращает зафиксированную. Клик по фону оверлея
// (или повторный клик по зафиксированной вершине) снимает фиксацию; клик по другой вершине —
// переключает фиксацию на неё.
export function attachGraphHover(svg, edges){
  const fwd = new Map(); // node.id -> Set целей его исходящих рёбер (source→target)
  const bwd = new Map(); // node.id -> Set источников его входящих рёбер (target→source)
  for (const e of edges){
    if (!fwd.has(e.source)) fwd.set(e.source, new Set());
    if (!bwd.has(e.target)) bwd.set(e.target, new Set());
    fwd.get(e.source).add(e.target);
    bwd.get(e.target).add(e.source);
  }
  const nodeEls = Array.from(svg.querySelectorAll('g[data-id]'));
  const edgeEls = Array.from(svg.querySelectorAll('path[data-source]'));

  const edgeKey = (source, target) => source + '→' + target;

  // Направленный BFS из id по карте adj: достижимые вершины + пройденные рёбра.
  // forward=true — обход вперёд (соседи — цели рёбер), false — назад (соседи —
  // источники); ключ ребра всегда в исходной ориентации source→target.
  const walk = (id, adj, forward, nodes, edgeKeys) => {
    const seen = new Set([id]);
    const queue = [id];
    while (queue.length){
      const cur = queue.shift();
      for (const nb of (adj.get(cur) || new Set())){
        edgeKeys.add(forward ? edgeKey(cur, nb) : edgeKey(nb, cur));
        if (!seen.has(nb)){ seen.add(nb); queue.push(nb); }
      }
    }
    for (const n of seen) nodes.add(n);
  };

  // путь вершины id: {nodes: Set вершин, edges: Set ключей рёбер} — потомки и предки
  const path = id => {
    const nodes = new Set(), edgeKeys = new Set();
    walk(id, fwd, true, nodes, edgeKeys);
    walk(id, bwd, false, nodes, edgeKeys);
    return { nodes, edges: edgeKeys };
  };

  // Красим путь голубым БЕЗ затемнения остального. set === null снимает всю подсветку.
  const paint = set => {
    for (const p of edgeEls){
      const inc = set && set.edges.has(edgeKey(p.getAttribute('data-source'), p.getAttribute('data-target')));
      p.style.stroke = inc ? 'var(--w-link)' : 'var(--w-line2)';
    }
    for (const g of nodeEls){
      const c = g.querySelector('circle');
      if (c) c.style.stroke = (set && set.nodes.has(g.getAttribute('data-id'))) ? 'var(--w-link)' : 'none';
    }
  };

  let pinnedNodeId = null; // зафиксированная вершина (её путь подсвечен постоянно)

  const showPinned = () => paint(pinnedNodeId ? path(pinnedNodeId) : null);

  for (const g of nodeEls){
    const id = g.getAttribute('data-id');
    g.style.cursor = 'pointer';
    // временная подсветка пути под курсором (поверх фиксации)
    g.addEventListener('mouseenter', () => paint(path(id)));
    // при уходе — вернуться к зафиксированному состоянию (или снять подсветку, если фиксации нет)
    g.addEventListener('mouseleave', showPinned);
    // клик по вершине: тот же узел — снять фиксацию, другой — зафиксировать его путь
    g.addEventListener('click', ev => {
      ev.stopPropagation(); // клик по фону оверлея не должен дополнительно снимать фиксацию
      pinnedNodeId = (pinnedNodeId === id) ? null : id;
      paint(path(id));
    });
  }

  // клик по фону svg (не по вершине) — снять фиксацию
  svg.addEventListener('click', () => { pinnedNodeId = null; paint(null); });
}

export function openGraphOverlay(st){
  closeGraphOverlay();
  const g = displayGraph(st);
  const ov = el('div', 'goverlay');
  ov.id = 'goverlay';
  const panel = el('div', 'panel');
  const cap = el('div', 'cap');
  cap.append(el('span', 't', 'Окрестность графа · ' + (g.nodes || []).length + ' узлов, '
    + (g.edges || []).length + ' связей'));
  const close = el('button', 'x');
  close.title = 'закрыть';
  close.setAttribute('aria-label', 'закрыть');
  close.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
  close.addEventListener('click', closeGraphOverlay);
  cap.append(close);
  panel.append(cap);
  if ((g.nodes || []).length) panel.append(graphSVG(g, 60, 880, false));
  else panel.append(el('p', 'p', 'Граф этого ответа пуст.'));
  ov.append(panel);
  ov.addEventListener('click', e => { if (e.target === ov) closeGraphOverlay(); });
  document.body.append(ov);
  close.focus();
}
export function closeGraphOverlay(){
  const ov = $('#goverlay');
  if (ov) ov.remove();
}
