import { showNotice } from './dialog.js';
import { S } from './state.js';

// ==================================================================
// Запрет действий, меняющих базу знаний, при резервной модели
// ==================================================================
// Загрузка, удаление и перезагрузка документа переразбирают корпус и заново
// извлекают факты. Извлечение идёт только через основную модель; на резервном
// провайдере оно либо откажет, либо выдаст разбор другого качества — и то и
// другое молча испортит базу. Пока состояние LLM «fallback», такие действия
// не выполняются: кнопка остаётся обычной, но вместо действия объясняет отказ.

const WORKING_STATUSES = new Set(['available', 'configured']);

export function llmBlockReason(){
  const health = S.health.data;
  if (S.health.failed || !health) return 'состояние системы неизвестно: backend не отвечает';
  if (health.answer_llm_provider === 'fallback') return 'состояние системы LLM: fallback';
  if (!WORKING_STATUSES.has(health.answer_llm_status)) {
    return 'состояние системы LLM: ' + (health.answer_llm_status || 'неизвестно');
  }
  return null;
}

export function showBlocked(reason){
  showNotice('Действие недоступно',
    'Невозможно выполнить действие, пока ' + reason + '. Загрузка, удаление и ' +
    'перезагрузка документов заново извлекают факты и требуют основной модели.');
}


/** Пропускает действие или показывает окно с причиной отказа. */
export function allowKnowledgeChange(){
  const reason = llmBlockReason();
  if (!reason) return true;
  showBlocked(reason);
  return false;
}
