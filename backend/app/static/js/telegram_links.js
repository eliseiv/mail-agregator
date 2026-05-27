/* =============================================================================
   telegram_links.js
   Telegram-привязки (ADR-0024 §4 / 04-api-contracts.md §4b) — секция на
   /my/integrations.

   Список TG-привязок текущего пользователя рендерится клиентски, потому что
   backend отдаёт их только JSON-эндпоинтом GET /api/telegram/links (нет
   server-rendered HTML-роута). Прогрессивное улучшение поверх csrf.js.

   Эндпоинты:
     GET    /api/telegram/links
       -> {links:[{telegram_user_id, created_at, dead}], max}
     POST   /api/telegram/links {init_data}
       -> {linked:true, telegram_user_id}
          | 409 tg_link_limit | 409 tg_link_owned_by_other
          | 401 invalid_init_data | init_data_expired
     DELETE /api/telegram/links/{telegram_user_id}
       -> {deleted: bool}  (идемпотентно — 200 даже если строки не было)

   Привязка нового TG требует Telegram WebApp initData
   (window.Telegram.WebApp.initData), доступного только при открытии сервиса
   внутри Telegram-бота. В обычном браузере initData нет — кнопка «Добавить»
   показывает инструкцию открыть бот в нужном Telegram-аккаунте.

   CSP-safe: без inline-обработчиков; всё навешивается по data-* атрибутам.
   Все state-changing запросы идут через window.MAS.csrfFetch (X-CSRF-Token).
   ========================================================================== */
(function () {
  'use strict';

  var section = document.querySelector('[data-tg-links-section]');
  if (!section) return;

  var body = section.querySelector('[data-tg-links-body]');
  var addBtn = section.querySelector('[data-tg-link-add]');
  var addHint = section.querySelector('[data-tg-link-add-hint]');

  /* ---- Утилиты ---------------------------------------------------------- */

  function flash(text, category) {
    if (window.MAS && typeof window.MAS.flash === 'function') {
      window.MAS.flash(text, category || 'info');
    }
  }

  function readError(response) {
    if (window.MAS && typeof window.MAS.readJsonError === 'function') {
      return window.MAS.readJsonError(response);
    }
    return Promise.resolve({
      code: 'http_' + response.status,
      message: 'Запрос не выполнен.'
    });
  }

  /** initData доступна только внутри Telegram WebApp. */
  function getTelegramInitData() {
    var tg = window.Telegram && window.Telegram.WebApp;
    if (!tg) return '';
    return typeof tg.initData === 'string' ? tg.initData : '';
  }

  /** Маскируем числовой telegram_user_id для отображения: 12•••789. */
  function maskTgId(id) {
    var s = String(id);
    if (s.length <= 5) return s;
    return s.slice(0, 2) + '•••' + s.slice(-3);
  }

  /** ISO-строка -> локальная дата/время; при ошибке вернуть исходное. */
  function formatDate(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    try {
      return d.toLocaleString('ru-RU', {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit'
      });
    } catch (_e) {
      return d.toISOString();
    }
  }

  function clear(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  /* ---- Рендер состояний ------------------------------------------------- */

  function renderLoading() {
    clear(body);
    var p = document.createElement('p');
    p.className = 'tg-links__loading';
    p.setAttribute('role', 'status');
    p.textContent = 'Загрузка списка привязок…';
    body.appendChild(p);
  }

  function renderError(message) {
    clear(body);
    var div = document.createElement('div');
    div.className = 'tg-links__error';
    div.setAttribute('role', 'alert');
    var strong = document.createElement('strong');
    strong.textContent = 'Не удалось загрузить привязки. ';
    div.appendChild(strong);
    div.appendChild(document.createTextNode(message || 'Попробуйте обновить страницу.'));
    var retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'btn btn--secondary btn--small tg-links__retry';
    retry.textContent = 'Повторить';
    retry.addEventListener('click', loadLinks);
    div.appendChild(document.createElement('br'));
    div.appendChild(retry);
    body.appendChild(div);
  }

  function renderEmpty() {
    clear(body);
    var div = document.createElement('div');
    div.className = 'tg-links__empty';
    div.setAttribute('role', 'status');
    var p = document.createElement('p');
    p.textContent = 'Нет привязанных Telegram-аккаунтов. ' +
      'Подключите Telegram, чтобы получать уведомления о новых письмах.';
    div.appendChild(p);
    body.appendChild(div);
  }

  /** Построить одну строку привязки. */
  function buildRow(link) {
    var li = document.createElement('li');
    li.className = 'tg-links__item' + (link.dead ? ' tg-links__item--dead' : '');
    li.setAttribute('data-tg-link-id', String(link.telegram_user_id));

    var main = document.createElement('div');
    main.className = 'tg-links__item-main';

    var idEl = document.createElement('span');
    idEl.className = 'tg-links__id';
    idEl.textContent = 'Telegram ID: ' + maskTgId(link.telegram_user_id);
    idEl.title = 'Telegram user id: ' + link.telegram_user_id;
    main.appendChild(idEl);

    var dateEl = document.createElement('span');
    dateEl.className = 'tg-links__date';
    dateEl.textContent = 'Привязан: ' + formatDate(link.created_at);
    main.appendChild(dateEl);

    li.appendChild(main);

    var statusWrap = document.createElement('div');
    statusWrap.className = 'tg-links__status';
    var badge = document.createElement('span');
    if (link.dead) {
      badge.className = 'integrations-badge integrations-badge--dead';
      badge.textContent = 'Неактивна';
      badge.title = 'Бот не смог доставить уведомление — переподключите этот Telegram';
      statusWrap.appendChild(badge);
      var hint = document.createElement('p');
      hint.className = 'tg-links__dead-hint';
      hint.textContent = 'Бот не смог доставить уведомление. Переподключите этот аккаунт через бота.';
      statusWrap.appendChild(hint);
    } else {
      badge.className = 'integrations-badge integrations-badge--ok';
      badge.textContent = 'Активна';
      statusWrap.appendChild(badge);
    }
    li.appendChild(statusWrap);

    var actions = document.createElement('div');
    actions.className = 'tg-links__item-actions';
    var unlink = document.createElement('button');
    unlink.type = 'button';
    unlink.className = 'btn btn--danger btn--small';
    unlink.textContent = 'Отвязать';
    unlink.setAttribute('aria-label', 'Отвязать Telegram ' + maskTgId(link.telegram_user_id));
    unlink.addEventListener('click', function () {
      onUnlink(link.telegram_user_id, unlink);
    });
    actions.appendChild(unlink);
    li.appendChild(actions);

    return li;
  }

  function renderList(data) {
    var links = (data && data.links) || [];
    if (links.length === 0) {
      renderEmpty();
      return;
    }
    clear(body);
    var ul = document.createElement('ul');
    ul.className = 'tg-links__list';
    links.forEach(function (link) {
      ul.appendChild(buildRow(link));
    });
    body.appendChild(ul);

    // Лимит: если достигнут — поясняем на кнопке/подсказке.
    var max = data && typeof data.max === 'number' ? data.max : null;
    var liveCount = links.filter(function (l) { return !l.dead; }).length;
    if (max !== null && liveCount >= max) {
      var note = document.createElement('p');
      note.className = 'field__hint tg-links__limit-note';
      note.textContent = 'Достигнут лимит привязок (' + max + '). ' +
        'Чтобы добавить новый — сначала отвяжите один из существующих.';
      body.appendChild(note);
    }
  }

  /* ---- Кнопка «Добавить» ------------------------------------------------ */

  function setupAddButton() {
    if (!addBtn) return;
    var initData = getTelegramInitData();
    if (initData) {
      // Внутри Telegram WebApp — можем привязать текущий TG напрямую.
      addBtn.hidden = false;
      if (addHint) addHint.hidden = true;
      addBtn.addEventListener('click', function () {
        onAddViaWebApp(initData);
      });
    } else {
      // Обычный браузер — initData нет. Показываем инструкцию, кнопку прячем
      // (нажимать нечем). Подсказка объясняет, как привязать через бота.
      addBtn.hidden = true;
      if (addHint) addHint.hidden = false;
    }
  }

  function onAddViaWebApp(initData) {
    addBtn.disabled = true;
    addBtn.setAttribute('aria-busy', 'true');
    var originalLabel = addBtn.textContent;
    addBtn.textContent = 'Привязываем…';

    window.MAS.csrfFetch('/api/telegram/links', {
      method: 'POST',
      body: { init_data: initData }
    }).then(function (response) {
      if (response.ok) {
        flash('Telegram-аккаунт привязан.', 'success');
        loadLinks();
        return;
      }
      return readError(response).then(function (err) {
        var message;
        if (err.code === 'tg_link_limit') {
          message = err.message || 'Достигнут лимит привязок.';
        } else if (err.code === 'tg_link_owned_by_other') {
          message = 'Этот Telegram уже привязан к другому аккаунту системы.';
        } else if (err.code === 'invalid_init_data' || err.code === 'init_data_expired') {
          message = 'Не удалось подтвердить Telegram. Откройте бот заново и повторите.';
        } else {
          message = err.message || 'Не удалось привязать Telegram.';
        }
        flash(message, 'error');
      });
    }).catch(function () {
      flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
    }).then(function () {
      addBtn.disabled = false;
      addBtn.removeAttribute('aria-busy');
      addBtn.textContent = originalLabel;
    });
  }

  /* ---- Отвязка ---------------------------------------------------------- */

  function onUnlink(telegramUserId, btn) {
    // eslint-disable-next-line no-alert
    if (!window.confirm('Отвязать этот Telegram-аккаунт? Уведомления в него приходить перестанут.')) {
      return;
    }
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    var original = btn.textContent;
    btn.textContent = 'Отвязываем…';

    window.MAS.csrfFetch('/api/telegram/links/' + encodeURIComponent(telegramUserId), {
      method: 'DELETE'
    }).then(function (response) {
      if (response.ok) {
        return response.json().then(function (data) {
          if (data && data.deleted === true) {
            flash('Telegram-аккаунт отвязан.', 'success');
          } else {
            // {deleted:false} — строки уже не было (чужой/несуществующий).
            flash('Привязка не найдена — список обновлён.', 'info');
          }
          loadLinks();
        }).catch(function () {
          // 200 без тела — всё равно обновим список.
          flash('Telegram-аккаунт отвязан.', 'success');
          loadLinks();
        });
      }
      return readError(response).then(function (err) {
        flash(err.message || 'Не удалось отвязать Telegram.', 'error');
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
        btn.textContent = original;
      });
    }).catch(function () {
      flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
      btn.disabled = false;
      btn.removeAttribute('aria-busy');
      btn.textContent = original;
    });
  }

  /* ---- Загрузка списка -------------------------------------------------- */

  function loadLinks() {
    renderLoading();
    window.MAS.csrfFetch('/api/telegram/links', { method: 'GET' })
      .then(function (response) {
        if (response.ok) {
          return response.json().then(function (data) {
            renderList(data);
          }).catch(function () {
            renderError('Некорректный ответ сервера.');
          });
        }
        return readError(response).then(function (err) {
          renderError(err.message);
        });
      })
      .catch(function () {
        renderError('Сетевая ошибка.');
      });
  }

  /* ---- Старт ------------------------------------------------------------ */

  setupAddButton();
  loadLinks();
})();
