// ==================================================================
// Открытый ответ переживает перезагрузку страницы
// ==================================================================
// Хранилище — sessionStorage вкладки. Состояние принадлежит одному человеку и
// одной вкладке: на сервер не уходит, другим пользователям не видно, две
// вкладки держат независимые ответы и не затирают друг друга. Закрытие вкладки
// состояние стирает — накапливаться нечему.

const KEY = 'graphrag-article';

const FINISHED = new Set(['done', 'offtopic', 'error']);

export function saveArticle(st){
  if (!st) return;
  try {
    // Готовая статья сохраняется целиком: ctrl (AbortController) не
    // сериализуется, noteMap выводится из notes — это один и тот же объект
    // заметки. От незавершённой хранится только вопрос: соединение после
    // перезагрузки мертво, продолжить поток нечем, поэтому статья строится заново
    const { ctrl, noteMap, ...rest } = st;
    sessionStorage.setItem(KEY, JSON.stringify(
      FINISHED.has(st.phase) ? rest : { question: st.question, phase: st.phase }));
  } catch (_) {
    // Переполнение квоты не должно ронять уже показанный ответ: тихо
    // отказываемся от сохранения, экран продолжает работать
    clearArticle();
  }
}

export function restoreArticle(){
  let saved = null;
  try {
    saved = JSON.parse(sessionStorage.getItem(KEY) || 'null');
  } catch (_) {
    clearArticle();
    return null;
  }
  if (!saved || !saved.question) return null;
  // Незавершённая: восстанавливать нечего, вызывающий задаёт вопрос заново
  if (!FINISHED.has(saved.phase)) return { question: saved.question, unfinished: true };
  return {
    ...saved,
    ctrl: new AbortController(),      // прежний контроллер вместе со страницей умер
    noteMap: new Map((saved.notes || []).map(note => [note.id, note])),
    notes: saved.notes || [],
    web: saved.web || { phase: 'done', data: null, error: null },
  };
}

function clearArticle(){
  try { sessionStorage.removeItem(KEY); } catch (_) {}
}
