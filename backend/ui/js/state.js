import { newArticle } from './article_stream.js';
import { apiJSON, trunc } from './dom.js';
import { sciWord } from './view_docs.js';

// ---------- кэш документов (имена, научность, тип) ----------
export let docsCache = null;           // массив документов из GET /documents
export let docsById = Object.create(null);
export const docFragCache = new Map(); // document_id -> ответ GET /documents/{id}
// типы, для которых backend формирует PDF-превью (LibreOffice-конвертация исходника);
// дублирует серверный PREVIEW_SOURCE_EXTENSIONS (storage.py): backend строит
// превью только для этих форматов — лишний тип здесь = гарантированный 404
export const PREVIEW_TYPES = ['docx', 'docm', 'pptx'];

export async function loadDocs(force){
  if (docsCache && !force) return docsCache;
  try {
    const list = await apiJSON('/documents');
    docsCache = Array.isArray(list) ? list : [];
    docsById = Object.create(null);
    for (const d of docsCache) docsById[d.id] = d;
  } catch (_) {
    docsCache = docsCache || [];
  }
  return docsCache;
}
export function docInfo(id){ return docsById[id] || null; }

// Адрес фрагмента: для PDF page — страница, для PPTX — слайд, иначе сквозной блок
export function fragLoc(documentId, page){
  const d = docInfo(documentId);
  const t = (d && d.document_type || '').toLowerCase();
  const n = (page == null ? '?' : page);
  if (t === 'pdf') return 'стр. ' + n;
  if (t === 'pptx' || t === 'ppt') return 'слайд ' + n;
  return 'блок ' + n;
}
export function sciLabel(id){
  // Подпись источника: тип (доклад/отчёт/…) и научность — независимые поля,
  // «доклад · научный»; непосчитанные части опускаются
  const d = docInfo(id);
  if (!d) return null;
  const parts = [];
  if (d.doc_type) parts.push(d.doc_type);
  if (d.is_scientific != null) parts.push(sciWord(d.is_scientific));
  return parts.length ? parts.join(' · ') : null;
}
export function docName(id){
  const d = docInfo(id);
  return d ? d.filename : ('документ ' + trunc(id, 12));
}
// Год издания документа (контракт «год статьи»); null, если года нет / документ не в кэше
export function docYear(id){
  const d = docInfo(id);
  return d && d.year != null ? d.year : null;
}

// ---------- недавние вопросы ----------
export const RECENT_KEY = 'graphrag-recent-questions';
export function recentList(){
  try { const v = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]'); return Array.isArray(v) ? v : []; }
  catch (_) { return []; }
}
export function pushRecent(q){
  const list = [q].concat(recentList().filter(x => x !== q)).slice(0, 5);
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch (_) {}
}

// три самых показательных запроса — на главной и в подсказках экрана вопроса вне предметной области
export const POPULAR = [
  'Обзор способов удаления SO₂ из отходящих газов металлургии',
  'Какие технические решения циркуляции католита при электроэкстракции никеля описаны в практике?',
  'Какие методы обессоливания подходят при сульфатах 200–300 мг/л?',
];

// ==================================================================
// Состояние приложения
// ==================================================================
export const S = {
  article: null,   // состояние текущей «статьи» (см. newArticle)
  srcView: null,   // состояние экрана источников
  health: { data: null, failed: false, at: 0, color: 'ok', pinned: false },
  uploads: [],     // [{name, status, kind, error}]; kind: wait | ok | err — цвет статуса в плашке
};

// ---------- скрытые документы ----------
// Выбор читателя, а не состояние системы: набор живёт в sessionStorage вкладки
// и уезжает с каждым вопросом. У другого человека и в другой вкладке те же
// документы остаются видимыми; закрытие вкладки возвращает базу целиком.
const HIDDEN_KEY = 'graphrag-hidden-docs';

function readHidden(){
  try { return new Set(JSON.parse(sessionStorage.getItem(HIDDEN_KEY) || '[]')); }
  catch (_) { return new Set(); }
}

let hiddenDocs = readHidden();

export function hiddenDocIds(){ return [...hiddenDocs]; }
export function isDocHidden(id){ return hiddenDocs.has(id); }

export function toggleDocHidden(id){
  if (hiddenDocs.has(id)) hiddenDocs.delete(id); else hiddenDocs.add(id);
  try { sessionStorage.setItem(HIDDEN_KEY, JSON.stringify([...hiddenDocs])); } catch (_) {}
  return hiddenDocs.has(id);
}
