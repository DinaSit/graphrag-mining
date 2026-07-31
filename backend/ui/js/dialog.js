import { $, el } from './dom.js';

// ==================================================================
// Всплывающие окна: сообщение и подтверждение
// ==================================================================
// Замена родным confirm() и alert() браузера. Те не поддаются стилизации:
// на странице появляется системная панель со своими шрифтами и кнопками,
// выпадающая из оформления. Здесь та же панель, что у остальных окон.

/** Каркас окна: затемнение, панель, закрытие по Escape и клику по фону. */
function openPanel(title, done){
  if ($('#dlg')) return null;      // одно окно за раз: второе поверх первого не строим
  const overlay = el('div', 'goverlay');
  overlay.id = 'dlg';
  const panel = el('div', 'panel narrow');
  const cap = el('div', 'cap');
  cap.append(el('div', 't', title));
  panel.append(cap);

  const close = result => {
    overlay.remove();
    document.removeEventListener('keydown', onKey);
    done(result);
  };
  const onKey = event => { if (event.key === 'Escape') close(false); };
  overlay.addEventListener('click', event => { if (event.target === overlay) close(false); });
  document.addEventListener('keydown', onKey);

  overlay.append(panel);
  document.body.append(overlay);
  return { panel, close };
}

/** Сообщение с единственной кнопкой. Промис завершается закрытием окна. */
export function showNotice(title, text){
  return new Promise(resolve => {
    const dlg = openPanel(title, resolve);
    if (!dlg) return resolve(false);
    dlg.panel.append(el('p', 'blocktext', text));
    const ok = el('button', 'blockok', 'Понятно');
    ok.addEventListener('click', () => dlg.close(true));
    dlg.panel.append(ok);
    ok.focus();
  });
}

/**
 * Подтверждение действия: true — согласие, false — отказ или закрытие.
 * У необратимого действия (danger) фокус стоит на «Отмене»: случайный Enter
 * тогда отменяет, а не выполняет.
 */
export function askConfirm({ title, text, confirmLabel = 'Подтвердить', danger = false }){
  return new Promise(resolve => {
    const dlg = openPanel(title, resolve);
    if (!dlg) return resolve(false);
    dlg.panel.append(el('p', 'blocktext', text));
    const row = el('div', 'lrow');
    const cancel = el('button', 'lcancel', 'Отмена');
    const confirm = el('button', 'lok' + (danger ? ' ldanger' : ''), confirmLabel);
    cancel.addEventListener('click', () => dlg.close(false));
    confirm.addEventListener('click', () => dlg.close(true));
    row.append(cancel, confirm);
    dlg.panel.append(row);
    (danger ? cancel : confirm).focus();
  });
}
