import { abortStream, newArticle, startStream } from './article_stream.js';
import { saveArticle } from './article_store.js';
import { apiJSON } from './dom.js';
import { gotoRoute, routeName } from './router.js';
import { S, pushRecent } from './state.js';
import { paintChips, paintInfobox } from './view_article.js';
import { paintWebMode, webActiveSources } from './web_mode.js';

// ==================================================================
// Запуск вопроса
// ==================================================================
export function ask(q){
  q = String(q || '').trim();
  if (!q) return;
  pushRecent(q);
  abortStream(S.article); // старый стрим не должен дописывать поверх нового вопроса
  S.article = newArticle(q);
  saveArticle(S.article); // перезагрузка во время ответа перезапустит именно этот вопрос
  startStream(S.article);
  startWebAnswer(S.article); // веб-контур стартует сразу, параллельно и независимо
  gotoRoute('article');
}

// К1: POST /web/answer — независимый веб-поиск; результат сохраняется в состояние
// статьи и не блокирует остальные операции. Отменяется тем же AbortController'ом, что и стрим.
export async function startWebAnswer(st){
  try {
    // Вместе с вопросом передаётся реестр источников: поиск на сервере идёт ровно
    // по списку из карточки «О статье» (ru — русским запросом, en — переводом)
    const data = await apiJSON('/web/answer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: st.question, sources: webActiveSources() }),
      signal: st.ctrl.signal,
    });
    st.web = { phase: 'done', data, error: null };
  } catch (e) {
    if (e && e.name === 'AbortError') return; // штатная отмена при новом вопросе
    st.web = { phase: 'error', data: null, error: e.detail || e.message || 'сеть недоступна' };
  }
  saveArticle(st);   // веб-ответ — часть состояния статьи
  // если пользователь уже смотрит веб-режим этой статьи — скелет заменяем ответом,
  // а инфобокс и чипы получают свежий список веб-источников
  if (routeName() === 'article' && S.article === st && st.webMode){
    paintWebMode(st);
    paintInfobox(st);
    paintChips(st);
  }
}
