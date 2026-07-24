import { respOf } from './article_stream.js';
import { $, el, trunc } from './dom.js';

// ==================================================================
// Живые сноски: [fragment-…] / [claim-…] / (fragment-…) -> [n]
// ==================================================================
export const CITE_RE = /[\[(]\s*((?:fragment|claim)-[\w./-]+(?:\s*,\s*(?:fragment|claim)-[\w./-]+)*)\s*[\])]/g;

// Хвост буфера с незакрытой скобкой, которая ещё может стать сноской, удерживается и не выводится
export function splitPending(buf){
  const from = Math.max(0, buf.length - 200); // сноска короче ~200 символов — дальше назад буфер не просматривается
  for (let i = buf.length - 1; i >= from; i--){
    const ch = buf[i];
    if (ch === ']' || ch === ')') break;
    if (ch === '[' || ch === '('){
      const inner = buf.slice(i + 1);
      if (isCiteTail(inner, ch === '[')) return [buf.slice(0, i), buf.slice(i)];
      break;
    }
  }
  return [buf, ''];
}
// Может ли текст после незакрытой скобки быть дополнен до сноски: уже начатый список
// id либо недобранный префикс слова «fragment-»/«claim-» (проверка через
// startsWith наоборот). allowEmpty — для «[»: пустой хвост после квадратной
// скобки придерживаем, после круглой — нет
export function isCiteTail(inner, allowEmpty){
  const s = inner.replace(/^\s+/, '');
  if (!s) return allowEmpty;
  if (/^(?:fragment|claim)-[\w.,;:\s/-]*$/i.test(s)) return true;
  return 'fragment-'.startsWith(s.toLowerCase()) || 'claim-'.startsWith(s.toLowerCase());
}

// note для id: номер по первому появлению; ссылка резолвится по данным ответа
export function getNote(st, id){
  if (st.noteMap.has(id)) return st.noteMap.get(id);
  const note = { id, ref: resolveRef(st, id), quote: null };
  note.quote = resolveQuote(st, id, note.ref);
  st.noteMap.set(id, note);
  st.notes.push(note);
  return note;
}
export function noteForSource(st, source){
  if (!source) return null;
  const id = source.fragment_id || (source.document_id + '#');
  if (st.noteMap.has(id)) return st.noteMap.get(id);
  const note = { id, ref: source, quote: source.quote || null };
  st.noteMap.set(id, note);
  st.notes.push(note);
  return note;
}
export function resolveRef(st, id){
  const r = respOf(st);
  const pools = [].concat(
    r.sources || [], r.related_sources || [],
    (r.search_hits || []).map(h => h.source),
    (r.experiments || []).map(x => x.source),
    (r.related_experiments || []).map(x => x.source)
  ).filter(Boolean);
  if (id.startsWith('fragment-')){
    const hit = pools.find(s => s.fragment_id === id);
    if (hit) return hit;
    // фрагмент не в списках ответа — даже document_id из самого id надёжно не извлечь
    return null;
  }
  // claim-…: пробуем найти узел графа и его фрагмент
  for (const g of [r.graph, r.related_graph]){
    const node = g && (g.nodes || []).find(n => n.id === id);
    if (node && node.data){
      const fid = node.data.fragment_id || node.data.source_fragment_id;
      if (fid){
        const hit = pools.find(s => s.fragment_id === fid);
        if (hit) return hit;
      }
    }
  }
  return null;
}
export function resolveQuote(st, id, ref){
  if (ref && ref.quote) return ref.quote;
  const r = respOf(st);
  const fid = ref ? ref.fragment_id : (id.startsWith('fragment-') ? id : null);
  if (fid){
    const hit = (r.search_hits || []).find(h => h.fragment_id === fid);
    if (hit && hit.text) return trunc(hit.text, 280);
  }
  return null;
}

export function supFor(st, note){
  const n = st.notes.indexOf(note) + 1;
  const a = el('a', 'ref');
  a.tabIndex = 0;
  a.dataset.note = String(n - 1);
  a.append(el('sup', null, '[' + n + ']'));
  return a;
}

// Текст с id-сносками -> DOM с <sup>[n]</sup>; всё содержимое — textContent
export function appendRich(st, container, text){
  CITE_RE.lastIndex = 0;
  let last = 0, m;
  while ((m = CITE_RE.exec(text)) !== null){
    if (m.index > last) container.append(document.createTextNode(text.slice(last, m.index)));
    const ids = m[1].split(/\s*,\s*/).filter(x => /^(?:fragment|claim)-/.test(x));
    for (const id of ids) container.append(supFor(st, getNote(st, id)));
    last = m.index + m[0].length;
  }
  if (last < text.length) container.append(document.createTextNode(text.slice(last)));
}
export function richParagraphs(st, text, cls){
  const frag = document.createDocumentFragment();
  const parts = String(text || '').split(/\n{2,}/).filter(p => p.trim());
  for (const part of parts.length ? parts : ['']){
    const p = el('p', cls || 'p');
    appendRich(st, p, part);
    frag.append(p);
  }
  return frag;
}
