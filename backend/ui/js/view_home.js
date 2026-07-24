import { ask } from './ask.js';
import { apiJSON, el, searchIcon } from './dom.js';
import { POPULAR, docName, loadDocs, recentList } from './state.js';

// ==================================================================
// Главная
// ==================================================================
export function viewHome(view){
  const home = el('div', 'home');
  home.append(el('div', 'sp sp-t')); // фиксированный отступ над заголовком (96px)
  const big = el('div', 'big');
  big.append('Graph');
  big.append(el('span', null, 'RAG'));
  big.append(' — энциклопедия R&D');
  home.append(big);

  const search = el('div', 'search');
  const inp = el('input');
  inp.id = 'home-q';
  inp.type = 'text';
  inp.placeholder = 'Искать в Графе…';
  inp.autocomplete = 'off';
  const run = () => { const v = inp.value; inp.value = ''; ask(v); };
  inp.addEventListener('keydown', e => { if (e.key === 'Enter') run(); });
  search.append(inp, searchIcon(run)); // сочетание клавиш ⌘K работает без визуального бейджа
  home.append(search);
  home.append(el('div', 'sp sp-m')); // гибкий спейсер между поиском и карточками (вместо margin-top:124px)

  const cols = el('div', 'cols');
  // «Знаете ли вы?»: место зарезервировано с первого кадра пустым плейсхолдером
  // (dyk-empty — без рамки), приход /facts/random лишь наполняет его, не смещая карточки
  const dyk = el('div', 'card dyk dyk-empty');
  cols.append(dyk);

  const pop = el('div', 'card');
  pop.append(el('div', 't', 'Популярные статьи'));
  for (const q of POPULAR){
    const b = el('button', 'lnk', q + ' →');
    b.addEventListener('click', () => ask(q));
    pop.append(b);
  }
  cols.append(pop);

  const rec = el('div', 'card');
  rec.append(el('div', 't', 'Недавнее'));
  const recent = recentList().slice(0, 3); // не более трёх строк — карточка ниже
  if (recent.length){
    for (const q of recent){
      const b = el('button', 'lnk recent-q', q);
      b.addEventListener('click', () => ask(q));
      rec.append(b);
    }
  } else {
    const empty = el('div', null, 'Пока пусто — задайте первый вопрос.');
    empty.style.cssText = 'color:var(--w-dim);font-size:13.5px';
    rec.append(empty);
  }
  cols.append(rec);
  home.append(cols);

  const corp = el('div', 'corp', 'Создано в рамках хакатона для Норникель');
  home.append(corp);
  home.append(el('div', 'sp sp-b')); // фиксированный отступ под подписью (28px)
  view.append(home);

  inp.focus();
  loadDocs().then(() => {
    fillDyk(dyk); // имена документов уже в кэше — можно подписать источник факта
  });
}

export function plural(n, one, few, many){
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

// «Знаете ли вы?»: на экран не попадает ни «не указано», ни пустые поля.
// Факты с незаполненными полями пропускаем и пробуем следующий (по массиву или повторным запросом).
export const DYK_EMPTY = new Set(['', 'не указано', 'неизвестно', 'нет данных', 'unknown', 'unknown property',
  'unknown material', 'unknown process', 'n/a', 'na', 'none', 'null', '-', '—']);
export function dykField(v){
  const s = String(v == null ? '' : v).trim();
  return DYK_EMPTY.has(s.toLowerCase()) ? null : s;
}
// Разбор факта в готовые куски фразы; null — факт для карточки не годится
export function dykPhrase(fact){
  if (!fact) return null;
  const material = dykField(fact.material);
  const property = dykField(fact.property);
  if (!material || !property) return null;
  const d = String(fact.effect_direction || '').toLowerCase();
  let dir = null;
  if (d === 'increase' || d === 'рост') dir = 'рост';
  else if (d === 'decrease' || d === 'снижение') dir = 'снижение';
  else if (d === 'neutral' || d === 'без изменений') dir = 'без изменений';
  if (!dir) return null;
  let val = '';
  if (dir !== 'без изменений' && fact.effect_value != null)
    val = ' на ' + fact.effect_value + (dykField(fact.effect_unit) || '');
  return { material, property, dir, val, process: dykField(fact.process) };
}

export async function fillDyk(dyk){
  let fact = null, ph = null, teaser = null;
  // до 4 попыток: эндпоинт может отдавать один факт или массив — перебираем всё
  for (let attempt = 0; attempt < 4 && !fact; attempt++){
    let data = null;
    try { data = await apiJSON('/facts/random'); }
    catch (_) { break; } // 404 или эндпоинт ещё не готов — карточку не показываем
    const pool = Array.isArray(data && data.facts) ? data.facts : [data && data.fact];
    for (const f of pool){
      // готовая LLM-формулировка (teaser из контракта) имеет приоритет над механической сборкой
      const t = dykField(f && f.teaser);
      const p = dykPhrase(f);
      if (t || p){ fact = f; teaser = t; ph = p; break; }
    }
  }
  if (!fact || !document.body.contains(dyk)) return;
  dyk.classList.remove('dyk-empty'); // плейсхолдер наполняется — проявляем рамку факта
  dyk.append(el('div', 't', 'Знаете ли вы?'));
  const f = el('div', 'fact');
  if (teaser){
    f.append(teaser); // «…что …?» уже сформулировано LLM целиком
  } else {
    // механическая сборка: «…что <материал>: «<свойство>» — <рост|снижение>[ на N%][ при «<процесс>»]?»
    f.append('…что ' + ph.material + ': «' + ph.property + '» — ');
    f.append(el('b', null, ph.dir + ph.val));
    if (ph.process) f.append(' при «' + ph.process + '»');
    f.append('?');
  }
  dyk.append(f);
  const src = el('div', 'src');
  const parts = [];
  if (fact.source && fact.source.document_id) parts.push(docName(fact.source.document_id));
  if (fact.confidence != null) parts.push('уверенность ' + Number(fact.confidence).toFixed(2));
  src.append(parts.join(' · ') + (parts.length ? ' · ' : ''));
  // человекочитаемый question из контракта; без него — прежняя механическая фраза
  const q = dykField(fact.question) ||
    (ph ? 'что известно про ' + ph.material + (ph.process ? ' при ' + ph.process : '') + '?' : null);
  if (q){
    const more = el('button', 'lnk', 'спросить подробнее →');
    more.addEventListener('click', () => ask(q));
    src.append(more);
  }
  dyk.append(src);
}
