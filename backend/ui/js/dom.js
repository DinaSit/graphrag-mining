// ---------- утилиты ----------
export const $ = s => document.querySelector(s);
export const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;

export function el(tag, cls, text){
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}
export function clear(node){ while (node.firstChild) node.removeChild(node.firstChild); }

// Лупа в правом краю строки поиска: символ без кнопочной обёртки, клик/Enter/Space = поиск
export function searchIcon(run){
  const ic = el('span', 'sicon');
  ic.setAttribute('role', 'button');
  ic.setAttribute('aria-label', 'Искать');
  ic.tabIndex = 0;
  ic.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/></svg>';
  ic.addEventListener('click', run);
  ic.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' '){ e.preventDefault(); run(); } });
  return ic;
}
export function pct(x){ return Math.round((Number(x) || 0) * 100) + ' %'; }
export function trunc(s, n){ s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
// «Ёлочки» вокруг цитаты ровно одной парой: фрагменты часто сами обёрнуты в «…» —
// внешнюю пару снимаем (только с краёв, внутренние кавычки не трогаем), потом оборачиваем
export function wrapQuote(s, n){
  s = String(s == null ? '' : s).trim();
  if (s.startsWith('«') && s.endsWith('»')) s = s.slice(1, -1).trim();
  return '«' + (n ? trunc(s, n) : s) + '»';
}
// Внешняя ссылка в новой вкладке: href/target/rel проставляются один раз здесь,
// содержимое — только textContent
export function extLink(href, text){
  const a = el('a', null, text);
  a.href = href;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  return a;
}

export async function apiJSON(path, opts){
  const res = await fetch(path, opts);
  if (!res.ok){
    const err = new Error('HTTP ' + res.status);
    err.status = res.status;
    try { err.detail = (await res.json()).detail; } catch (_) {}
    throw err;
  }
  return res.json();
}
