import { $, REDUCED, apiJSON, clear, el } from './dom.js';
import { S } from './state.js';

// ==================================================================
// Статус систем
// ==================================================================
export function healthColor(h, failed){
  if (failed || !h) return 'bad';
  if (h.postgres === 'memory' || h.neo4j === 'memory') return 'bad';
  const errs = [h.postgres_last_error, h.neo4j_last_error, h.minio_last_error];
  if (errs.some(e => e) || h.answer_llm_status !== 'configured') return 'warn';
  return 'ok';
}

export async function pollHealth(){
  const prev = S.health.color;
  try {
    S.health.data = await apiJSON('/health');
    S.health.failed = false;
  } catch (_) {
    S.health.failed = true;
  }
  S.health.at = Date.now();
  S.health.color = healthColor(S.health.data, S.health.failed);
  const dot = $('#sysdot');
  dot.className = 'sysdot' + (S.health.color === 'warn' ? ' warn' : S.health.color === 'bad' ? ' bad' : '');
  if (prev !== S.health.color && !REDUCED){
    dot.classList.add('changed');
    setTimeout(() => dot.classList.remove('changed'), 2200);
  }
  if (!$('#syspop').hidden) paintSyspop();
}
setInterval(pollHealth, 15000);

export function paintSyspop(){
  const pop = $('#syspop');
  clear(pop);
  const h = S.health.data;
  pop.append(el('div', 't', 'Состояние системы'));
  const row = (name, val, cls) => {
    const r = el('div', 'row');
    r.append(el('i', cls || ''), document.createTextNode(name), el('span', null, val));
    pop.append(r);
  };
  if (S.health.failed || !h){
    row('Backend', 'нет ответа', 'bad');
  } else {
    row('Backend', h.status || '—', h.status === 'ok' ? '' : 'warn');
    row('Extraction', h.extraction || '—', '');
    row('PostgreSQL', h.postgres || '—', h.postgres === 'enabled' ? (h.postgres_last_error ? 'warn' : '') : 'bad');
    row('Neo4j', h.neo4j || '—', h.neo4j === 'enabled' ? (h.neo4j_last_error ? 'warn' : '') : 'bad');
    row('MinIO', h.minio || '—', h.minio === 'enabled' ? (h.minio_last_error ? 'warn' : '') : 'warn');
    const llmVal = [h.answer_llm_provider, h.answer_llm_model].filter(Boolean).join(' · ') || h.answer_llm_status || '—';
    row('LLM', llmVal, h.answer_llm_status === 'configured' ? '' : 'warn');
  }
}

$('#sysdot').addEventListener('mouseenter', () => { paintSyspop(); $('#syspop').hidden = false; });
$('#sysdot').addEventListener('mouseleave', () => { if (!S.health.pinned) $('#syspop').hidden = true; });
$('#sysdot').addEventListener('click', () => {
  S.health.pinned = !S.health.pinned;
  paintSyspop();
  if (S.health.pinned) $('#syspop').hidden = false;
});
document.addEventListener('click', e => {
  if (S.health.pinned && !e.target.closest('#syspop') && !e.target.closest('#sysdot')){
    S.health.pinned = false;
    $('#syspop').hidden = true;
  }
});
