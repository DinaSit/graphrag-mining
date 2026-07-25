import { $, clear, el, wrapQuote } from './dom.js';
import { S, docInfo, docName } from './state.js';
import { sciWord } from './view_docs.js';
import { openSources } from './view_sources.js';

// ==================================================================
// Попап сноски
// ==================================================================
export let fnpopHideTimer = null;

export function showFnpop(refEl){
  const st = S.article;
  if (!st) return;
  const note = st.notes[Number(refEl.dataset.note)];
  if (!note) return;
  const pop = $('#fnpop');
  clear(pop);
  if (note.ref && note.ref.document_id){
    pop.append(el('div', 'd', docName(note.ref.document_id)));
    if (note.quote) pop.append(el('q', null, wrapQuote(note.quote, 300)));
    const ft = el('div', 'ft');
    // в подписи сноски — только признак научности источника
    const doc = docInfo(note.ref.document_id);
    const sci = doc && doc.is_scientific != null ? sciWord(doc.is_scientific) : '';
    ft.append(el('span', null, sci));
    const open = el('button', null, 'открыть источник →');
    open.addEventListener('click', () => { hideFnpop(); openSources(note.id); });
    ft.append(open);
    pop.append(ft);
  } else {
    pop.append(el('div', 'd', 'Источник не найден в списке ответа'));
    if (note.quote) pop.append(el('q', null, wrapQuote(note.quote, 300)));
  }
  pop.hidden = false;
  // позиционирование у сноски без выхода за края окна
  const r = refEl.getBoundingClientRect();
  const pw = pop.offsetWidth, ph = pop.offsetHeight;
  let left = r.left + window.scrollX - 20;
  left = Math.max(8 + window.scrollX, Math.min(left, window.scrollX + document.documentElement.clientWidth - pw - 8));
  let top = r.bottom + window.scrollY + 8;
  if (r.bottom + ph + 16 > window.innerHeight) top = r.top + window.scrollY - ph - 8;
  pop.style.left = left + 'px';
  pop.style.top = Math.max(window.scrollY + 8, top) + 'px';
}
export function hideFnpop(){ const p = $('#fnpop'); if (p) p.hidden = true; }
export function scheduleHideFnpop(){
  clearTimeout(fnpopHideTimer);
  fnpopHideTimer = setTimeout(hideFnpop, 250);
}
document.addEventListener('mouseover', e => {
  const ref = e.target.closest && e.target.closest('.ref[data-note]');
  if (ref){ clearTimeout(fnpopHideTimer); showFnpop(ref); }
  else if (e.target.closest && e.target.closest('#fnpop')) clearTimeout(fnpopHideTimer);
});
document.addEventListener('mouseout', e => {
  if ((e.target.closest && (e.target.closest('.ref[data-note]') || e.target.closest('#fnpop'))))
    scheduleHideFnpop();
});
document.addEventListener('click', e => {
  const ref = e.target.closest && e.target.closest('.ref[data-note]');
  if (ref){ e.preventDefault(); clearTimeout(fnpopHideTimer); showFnpop(ref); return; }
  if (!(e.target.closest && e.target.closest('#fnpop'))) hideFnpop();
});
document.addEventListener('focusin', e => {
  const ref = e.target.closest && e.target.closest('.ref[data-note]');
  if (ref) showFnpop(ref);
});
