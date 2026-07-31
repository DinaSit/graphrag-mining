import { apiJSON, el } from './dom.js';

// ==================================================================
// Вход для действий, меняющих базу знаний
// ==================================================================
// Пара логин-пароль хранится в sessionStorage вкладки: у каждого своя,
// закрытие вкладки её стирает, на сервер она уходит только заголовком
// Authorization. Заранее интерфейс о необходимости входа не спрашивает —
// узнаёт по ответу 401, поэтому при выключенной на сервере проверке
// (ADMIN_USER/ADMIN_PASSWORD не заданы) окно не появляется никогда.

const KEY = 'graphrag-auth';

function saved(){
  try { return sessionStorage.getItem(KEY) || ''; } catch (_) { return ''; }
}

function remember(header){
  try { sessionStorage.setItem(KEY, header); } catch (_) {}
}

function forget(){
  try { sessionStorage.removeItem(KEY); } catch (_) {}
}

/** Параметры запроса с заголовком входа, если он уже получен. */
function withAuth(opts){
  const header = saved();
  if (!header) return opts;
  return { ...opts, headers: { ...(opts && opts.headers), Authorization: header } };
}

/**
 * Действие, меняющее базу знаний. Отличается от apiJSON одним: ответ 401
 * не считается ошибкой, а вызывает окно входа и повтор запроса. Повтор ровно
 * один — иначе неверная пара уводила бы в бесконечный круг.
 */
export async function apiAction(path, opts){
  try {
    return await apiJSON(path, withAuth(opts));
  } catch (error) {
    if (error.status !== 401) throw error;
    forget();                       // сохранённая пара больше не подходит
    if (!await askLogin()) throw error;
    return apiJSON(path, withAuth(opts));
  }
}

// ---------- окно входа ----------

let pending = null;   // одно окно на все действия: параллельные ждут его решения

/** Показывает окно и ждёт результата: true — вход выполнен, false — отменён. */
export function askLogin(){
  if (pending) return pending;
  pending = new Promise(resolve => paintLogin(result => {
    pending = null;
    resolve(result);
  }));
  return pending;
}

function paintLogin(done){
  const overlay = el('div', 'goverlay');
  overlay.id = 'login';
  const panel = el('form', 'panel narrow login');
  panel.setAttribute('novalidate', '');

  const cap = el('div', 'cap');
  cap.append(el('div', 't', 'Вход'));
  panel.append(cap);
  panel.append(el('p', 'blocktext',
    'Действие меняет базу знаний. Введите логин и пароль — до закрытия вкладки они больше не понадобятся.'));

  const field = (label, type, autocomplete) => {
    const wrap = el('label', 'lfield');
    wrap.append(el('span', 'k', label));
    const input = el('input');
    input.type = type;
    input.autocomplete = autocomplete;
    input.spellcheck = false;
    wrap.append(input);
    panel.append(wrap);
    return input;
  };
  const user = field('Логин', 'text', 'username');
  const password = field('Пароль', 'password', 'current-password');

  // показать пароль: набранный вслепую пароль — первая причина «не подходит»
  const eye = el('button', 'leye', 'показать');
  eye.type = 'button';
  eye.addEventListener('click', () => {
    const hidden = password.type === 'password';
    password.type = hidden ? 'text' : 'password';
    password.classList.toggle('shown', hidden);
    eye.textContent = hidden ? 'скрыть' : 'показать';
    password.focus();
  });
  password.parentElement.append(eye);

  const error = el('div', 'lerr');
  error.hidden = true;
  panel.append(error);

  const row = el('div', 'lrow');
  const cancel = el('button', 'lcancel', 'Отмена');
  cancel.type = 'button';
  const submit = el('button', 'lok', 'Войти');
  submit.type = 'submit';
  row.append(cancel, submit);
  panel.append(row);

  const close = result => {
    overlay.remove();
    document.removeEventListener('keydown', onKey);
    done(result);
  };
  const onKey = event => { if (event.key === 'Escape') close(false); };

  panel.addEventListener('submit', async event => {
    event.preventDefault();
    if (!user.value || !password.value){
      show('Заполните оба поля');
      (user.value ? password : user).focus();
      return;
    }
    submit.disabled = true;
    submit.textContent = 'Проверяем…';
    // Заголовок собирается здесь и проверяется лёгким запросом: неверную пару
    // лучше отклонить сразу, чем на самом действии
    const header = 'Basic ' + btoa(unescape(encodeURIComponent(user.value + ':' + password.value)));
    const ok = await verify(header);
    submit.disabled = false;
    submit.textContent = 'Войти';
    if (!ok){
      show('Логин или пароль не подходят');
      password.value = '';
      password.focus();
      return;
    }
    remember(header);
    close(true);
  });

  function show(message){
    error.textContent = message;
    error.hidden = false;
  }

  cancel.addEventListener('click', () => close(false));
  overlay.addEventListener('click', event => { if (event.target === overlay) close(false); });
  document.addEventListener('keydown', onKey);

  overlay.append(panel);
  document.body.append(overlay);
  user.focus();
}

/**
 * Проверка пары без побочных действий: PATCH кандидата с пустым набором полей
 * ничего не меняет, но проходит ту же проверку доступа, что и любое действие.
 * 404 (кандидата нет) тоже означает, что пара принята.
 */
async function verify(header){
  try {
    const res = await fetch('/review/facts/__probe__', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', Authorization: header },
      body: JSON.stringify({ fields: {} }),
    });
    return res.status !== 401;
  } catch (_) {
    return false;   // сеть недоступна — считать пару верной нельзя
  }
}
