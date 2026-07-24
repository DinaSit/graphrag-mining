import { viewArticle } from './article_toc.js';
import { ask } from './ask.js';
import { $, clear } from './dom.js';
import { hideFnpop } from './footnote_popup.js';
import { closeGraphOverlay } from './graph.js';
import { S } from './state.js';
import { viewDocs } from './view_docs.js';
import { viewHome } from './view_home.js';
import { viewReview } from './view_review.js';
import { viewSources } from './view_sources.js';

// ==================================================================
// Роутер
// ==================================================================
export function routeName(){
  const h = location.hash || '#/';
  if (h.startsWith('#/article')) return 'article';
  if (h.startsWith('#/sources')) return 'sources';
  if (h.startsWith('#/docs'))    return 'docs';
  if (h.startsWith('#/review'))  return 'review';
  return 'home';
}

// Переход на экран: если он уже открыт — перерисовать (hashchange не сработает)
export function gotoRoute(name){
  if (routeName() === name) render();
  else location.hash = '#/' + name;
}

export function render(){
  hideFnpop();
  closeGraphOverlay();
  const r = routeName();
  const view = $('#view');
  clear(view);
  // шапка: поиск везде, кроме главной (возврат к статье — кнопкой «назад» браузера)
  $('#hdr-search').hidden = (r === 'home');
  $('#hdr-center').hidden = (r === 'home'); // пустую центральную обёртку на главной убираем целиком
  // «Ответ» виден всегда, но до первого вопроса приглушён и инертен;
  // после первого вопроса — обычная ссылка на статью
  // из любого раздела (генерация при уходе не прерывается)
  const navAns = $('#nav-answer');
  navAns.classList.toggle('dimmed', !S.article);
  navAns.title = S.article ? '' : 'появится после первого вопроса';
  navAns.classList.toggle('cur', !!S.article && r === 'article');
  $('#nav-docs').classList.toggle('cur', r === 'docs');
  $('#nav-review').classList.toggle('cur', r === 'review');
  $('#hdr-q').value = ''; // после запуска поиска поле пустое: вопрос и так виден заголовком статьи
  if (r === 'home') viewHome(view);
  else if (r === 'article') viewArticle(view);
  else if (r === 'sources') viewSources(view);
  else if (r === 'docs') viewDocs(view);
  else if (r === 'review') viewReview(view);
}
// Уход со страницы статьи НЕ отменяет стрим: он продолжает копить состояние в S.article,
// и возврат на #/article перерисует накопленное. Отмена — только при новом вопросе (ask()).
window.addEventListener('hashchange', render);
// приглушённый «Ответ» инертен: до первого вопроса клик не ведёт на #/article
$('#nav-answer').addEventListener('click', e => { if (!S.article) e.preventDefault(); });
