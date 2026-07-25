import { ask } from './ask.js';
import { el } from './dom.js';
import { hideFnpop } from './footnote_popup.js';
import { S } from './state.js';
import { paintAnswer, paintAskError, paintChips, paintFinalSections, paintInfobox, paintOfftopic, scheduleSpy } from './view_article.js';
import { applyWebMode, globeBtn, paintToc } from './web_mode.js';

// ==================================================================
// Экран статьи
// ==================================================================
// «Внешних источников» в базовом ответе нет: веб-ответ доступен через переключатель-глобус
export const TOC_SECTIONS = [
  ['ans',   'Ответ'],
  ['conf',  'Противоречия'],
  ['gaps',  'Пробелы'],
  ['hyp',   'Гипотезы'],
  ['facts', 'Прямые факты и эксперименты'],
  ['rel',   'Смежные данные'],
  ['notes', 'Источники'],
];

export function viewArticle(view){
  const st = S.article;
  if (!st){ location.hash = '#/'; return; }
  if (st.phase === 'offtopic'){ paintOfftopic(view); return; }
  if (st.phase === 'error'){ paintAskError(view, st); return; }
  // phase 'aborted' сюда не попадает: отмена бывает только при новом вопросе,
  // и ask() тут же заменяет S.article свежим состоянием

  const cols = el('div', 'cols3');
  const toc = el('nav', 'toc'); toc.id = 'toc';
  // скролл-обёртка «текст + инфобокс»: единственный скролл-контейнер экрана статьи
  const wrap = el('div', 'artwrap'); wrap.id = 'artscroll';
  const art = el('article'); art.id = 'artbody'; art.style.position = 'relative';
  const box = el('aside', 'infobox'); box.id = 'infobox';
  // попап сноски позиционируется по viewport-координатам — при скролле обёртки
  // он отрывается от своей сноски, поэтому прячем его; заодно обновляем скролл-spy
  // (слушатель зарегистрирован на самой обёртке и удаляется вместе с ней при перерисовке view)
  wrap.addEventListener('scroll', () => { hideFnpop(); scheduleSpy(); }, { passive: true });
  wrap.append(art, box);
  cols.append(toc, wrap);
  view.append(cols);

  const trow = el('div', 'trow');
  trow.append(el('h1', 'art', st.question));
  trow.append(globeBtn(st)); // переключатель веб-ответа — всегда на странице статьи
  art.append(trow);
  // узкий экран (≤980, содержание скрыто): вместо мета-строки и карточки
  // «О статье» — строка чипов с метриками; переключение выполняется только средствами CSS
  const chips = el('div', 'chips'); chips.id = 'art-chips';
  art.append(chips);
  const ans = el('div'); ans.id = 'sec-ans';
  const ansBody = el('div'); ansBody.id = 'ans-body';
  ans.append(ansBody);
  art.append(ans);
  const rest = el('div'); rest.id = 'sec-rest';
  art.append(rest);

  paintToc(st);
  paintInfobox(st);
  paintChips(st);
  paintAnswer(st);
  if (st.phase === 'done') paintFinalSections(st);
  else {
    const sk = el('div'); sk.id = 'skels';
    const s1 = el('div', 'skel'); s1.style.width = '88%';
    const s2 = el('div', 'skel'); s2.style.width = '64%';
    sk.append(s1, s2);
    art.append(sk);
  }
  // контейнер веб-режима размещён рядом с базовыми секциями и переключается глобусом
  const web = el('div'); web.id = 'webmode'; web.hidden = true;
  art.append(web);
  if (st.webMode) applyWebMode(st); // перерисовка (например, final) сохраняет веб-режим
}
