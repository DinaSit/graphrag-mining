import { apiAction } from './auth.js';
import { $, apiJSON, clear, el, trunc } from './dom.js';
import { render, routeName } from './router.js';
import { S, loadDocs } from './state.js';
import { pollReviewCount } from './view_review.js';
import { allowKnowledgeChange } from './llm_guard.js';

// ==================================================================
// Загрузка документов (плашка в шапке)
// ==================================================================
export const uplEl = $('#upl');
export const fileInput = $('#file-input');
uplEl.addEventListener('click', e => {
  if (e.target.closest('#upl-pop')) return;
  if (!allowKnowledgeChange()) return;
  fileInput.click();
});
uplEl.addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  e.preventDefault();
  if (allowKnowledgeChange()) fileInput.click();
});
fileInput.addEventListener('change', () => { onFiles(fileInput.files); fileInput.value = ''; });
uplEl.addEventListener('dragover', e => { e.preventDefault(); uplEl.classList.add('dragging'); });
uplEl.addEventListener('dragleave', () => uplEl.classList.remove('dragging'));
uplEl.addEventListener('drop', e => {
  e.preventDefault();
  uplEl.classList.remove('dragging');
  if (!allowKnowledgeChange()) return;
  if (e.dataTransfer && e.dataTransfer.files) onFiles(e.dataTransfer.files);
});

export function onFiles(fileList){
  // uploadOne открывает плашку прогресса синхронно (до первого await)
  for (const f of Array.from(fileList || [])) uploadOne(f);
}

export async function uploadOne(file){
  const entry = { name: file.name, status: 'в очереди', kind: 'wait', error: null };
  S.uploads.unshift(entry);
  paintUplPop(true);
  try {
    const fd = new FormData();
    fd.append('file', file, file.name);
    const data = await apiAction('/ingest', { method: 'POST', body: fd });
    if (data.job_id){
      entry.status = 'в очереди';
      pollJob(entry, data.job_id);
    } else {
      // синхронный режим или дубликат — документ уже в базе
      entry.status = 'готово ✓';
      entry.kind = 'ok';
      onUploadDone();
    }
  } catch (e) {
    entry.status = 'ошибка ✗';
    entry.kind = 'err';
    entry.error = e.detail || e.message;
  }
  paintUplPop();
}

export const JOB_404_LIMIT = 5;  // подряд 404 — задача потеряна (перезапуск backend без очереди)
export const JOB_NET_LIMIT = 20; // подряд сетевых/прочих ошибок — опрос прекращается с тем же исходом

export function pollJob(entry, jobId){
  let miss404 = 0, missNet = 0;
  const lost = () => {
    entry.status = '✗ задача потеряна'; entry.kind = 'err';
    entry.error = 'перезагрузите файл';
    paintUplPop();
  };
  const tick = async () => {
    try {
      const job = await apiJSON('/jobs/' + encodeURIComponent(jobId));
      miss404 = missNet = 0;
      if (job.status === 'completed'){
        entry.status = 'готово ✓'; entry.kind = 'ok';
        paintUplPop();
        onUploadDone();
        return;
      }
      if (job.status === 'failed'){
        entry.status = 'ошибка ✗'; entry.kind = 'err';
        entry.error = job.error || 'обработка не удалась';
        paintUplPop();
        return;
      }
      entry.status = job.status === 'processing' ? 'обработка…' : 'в очереди';
      paintUplPop();
    } catch (e) {
      // 404 — задача неизвестна backend'у; всё остальное (сеть, 5xx) — свой счётчик
      if (e && e.status === 404){
        if (++miss404 >= JOB_404_LIMIT){ lost(); return; }
      } else if (++missNet >= JOB_NET_LIMIT){ lost(); return; }
    }
    setTimeout(tick, 4000);
  };
  setTimeout(tick, 4000);
}

export function onUploadDone(){
  // фоновое обновление: на маршруте docs render() перезагрузит список,
  // вне его обновляется только кэш документов
  if (routeName() === 'docs') render();
  else loadDocs(true);
  pollReviewCount();
}

export function paintUplPop(open){
  const pop = $('#upl-pop');
  if (open) pop.hidden = false;
  if (pop.hidden) return;
  clear(pop);
  pop.append(el('div', 't', 'Загрузки'));
  if (!S.uploads.length){
    pop.append(el('div', 'u-row', 'Перетащите файлы на плашку или кликните по ней.'));
    return;
  }
  for (const u of S.uploads.slice(0, 8)){
    const row = el('div', 'u-row');
    row.append(el('span', 'u-name', u.name));
    row.append(el('span', 'u-st' + (u.kind === 'ok' ? ' ok' : u.kind === 'err' ? ' err' : ''), u.status));
    pop.append(row);
    if (u.error) pop.append(el('div', 'u-err', trunc(u.error, 160)));
  }
}
document.addEventListener('click', e => {
  const pop = $('#upl-pop');
  if (!pop.hidden && !e.target.closest('#upl')) pop.hidden = true;
});
