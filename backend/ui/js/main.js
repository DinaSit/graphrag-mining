/* ==================================================================
   GraphRAG «Энциклопедия» — точка входа SPA (hash-роутинг, без сборки).
   Интерфейс собран из ES-модулей, которые браузер загружает сам: сборщик
   и внешние зависимости по-прежнему не нужны.
   Все данные из API вставляются ТОЛЬКО через textContent/createElement:
   цитаты содержат кавычки и угловые скобки.
   Порядок в файле: сначала модули, затем стартовые вызовы — они должны
   выполняться после того, как все модули вычислены.
   ================================================================== */

import { restoreArticle } from './article_store.js';
import { ask } from './ask.js';
import { pollHealth } from './health.js';
import { render, routeName } from './router.js';
import { S, loadDocs } from './state.js';
import { pollReviewCount } from './view_review.js';
import { loadWebSources } from './web_mode.js';
import './dom.js';
import './ask.js';
import './view_home.js';
import './article_stream.js';
import './citations.js';
import './article_toc.js';
import './view_article.js';
import './footnote_popup.js';
import './graph.js';
import './view_sources.js';
import './view_docs.js';
import './upload.js';
import './header.js';

// ==================================================================
// Старт
// ==================================================================
// Ответ восстанавливается до первой отрисовки: роутер по S.article решает,
// показывать ли экран статьи и подсвечивать ли пункт «Ответ». Прерванный
// перезагрузкой ответ продолжить нечем — тот же вопрос задаётся заново
const saved = restoreArticle();
if (saved && saved.unfinished) ask(saved.question);
else S.article = saved;

pollHealth();
pollReviewCount();
loadWebSources();
// Восстановленный ответ рисуется мгновенно, а список документов приходит
// отдельным запросом. Без перерисовки по его приходу сноски остаются с id
// вместо названий: при обычном ответе имена успевают загрузиться за время
// генерации, а после перезагрузки страницы — нет
loadDocs().then(() => { if (S.article && routeName() === 'article') render(); });
render();
