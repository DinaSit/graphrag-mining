import { ask } from './ask.js';
import { $, clear, el, searchIcon } from './dom.js';
import { hideFnpop } from './footnote_popup.js';
import { closeGraphOverlay } from './graph.js';
import { routeName } from './router.js';
import { S } from './state.js';
import { dockDocId } from './view_docs.js';

// ==================================================================
// Клавиатура и поиск в шапке
// ==================================================================
export const hdrQ = $('#hdr-q');
export const runHdrSearch = () => { const v = hdrQ.value; hdrQ.value = ''; ask(v); };
$('#hdr-search').append(searchIcon(runHdrSearch));
hdrQ.addEventListener('keydown', e => { if (e.key === 'Enter') runHdrSearch(); });

// актуальная высота шапки — в --hdr-h (sticky-панели, липкая шапка review, центрирование экрана вопроса вне предметной области)
export function syncHdrH(){
  // getBoundingClientRect: субпиксельная точность, чтобы под липкими блоками не было щели
  document.documentElement.style.setProperty('--hdr-h', $('#hdr').getBoundingClientRect().height + 'px');
}
new ResizeObserver(syncHdrH).observe($('#hdr'));
syncHdrH();

document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && String(e.key).toLowerCase() === 'k'){
    e.preventDefault();
    const target = $('#home-q') || $('#hdr-q');
    if (target){ target.focus(); target.select(); }
    return;
  }
  if (e.key === 'Escape'){
    hideFnpop();
    closeGraphOverlay();
    $('#upl-pop').hidden = true;
    S.health.pinned = false;
    $('#syspop').hidden = true;
    closeBurger();
  }
  // «Документы»: ↑/↓ листают список, досье обновляется; из полей ввода не перехватываем
  if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && routeName() === 'docs'){
    const t = e.target;
    if (t && /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName)) return;
    const rows = [...document.querySelectorAll('.docrow')];
    if (!rows.length) return;
    e.preventDefault();
    const i = rows.findIndex(r => r.dataset.id === dockDocId);
    const j = i < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)));
    if (rows[j].dataset.id !== dockDocId){
      rows[j].click();
      rows[j].scrollIntoView({ block: 'nearest' });
    }
  }
});

// ---------- бургер-меню тесной шапки (≤980px) ----------
// Пункты строятся заново при каждом открытии в соответствии с текущим состоянием
// nav-item'ов (dimmed/cur/красная точка Review) — состояние не дублируется
export const burgerBtn = $('#burger');
export const bmenu = $('#bmenu');
export function closeBurger(){
  if (bmenu.hidden) return;
  bmenu.hidden = true;
  burgerBtn.setAttribute('aria-expanded', 'false');
}
export function buildBurgerMenu(){
  clear(bmenu);
  for (const src of document.querySelectorAll('.hright > .nav-item')){
    const active = src.tagName === 'A' && !src.classList.contains('dimmed');
    const item = el(active ? 'a' : 'span',
      'bm-item' + (src.classList.contains('dimmed') ? ' dimmed' : '') + (src.classList.contains('cur') ? ' cur' : ''));
    item.textContent = src.textContent;
    if (src.id === 'nav-review' && !$('#review-dot').hidden) item.append(el('span', 'bdot'));
    if (active){
      item.href = src.getAttribute('href');
      item.addEventListener('click', closeBurger);
    }
    bmenu.append(item);
  }
}
burgerBtn.addEventListener('click', e => {
  e.stopPropagation();
  if (bmenu.hidden){
    buildBurgerMenu();
    bmenu.hidden = false;
    burgerBtn.setAttribute('aria-expanded', 'true');
  } else closeBurger();
});
document.addEventListener('click', e => {
  if (!bmenu.hidden && !bmenu.contains(e.target) && !burgerBtn.contains(e.target)) closeBurger();
});
